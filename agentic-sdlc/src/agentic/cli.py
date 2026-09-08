"""Typer entrypoint for the `agentic` command-line control plane
(Section 14). Command bodies delegate to runtime.py for engine
construction so this module stays argument-parsing and presentation.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import typer
from rich.console import Console
from rich.table import Table

from agentic import runtime
from agentic.core.models import Artifact, compute_content_hash
from agentic.governance.approvals import ApprovalManager
from agentic.observability.audit import render_audit_report, verify_run_audit
from agentic.observability.metrics import MetricsCollector
from agentic.observability.reporting import (
    ReportData,
    reconstruct_approvals_from_events,
    render_html,
    render_markdown,
)
from agentic.workflows import ambiguous

console = Console()

app = typer.Typer(
    name="agentic",
    help="Agentic SDLC Orchestration System — control plane.",
    no_args_is_help=True,
)

approvals_app = typer.Typer(help="Inspect and manage human approval checkpoints.")
app.add_typer(approvals_app, name="approvals")

_KNOWN_WORKFLOWS = ("greenfield", "brownfield", "ambiguous")


def _new_run_id(workflow: str) -> str:
    return f"{workflow}-{uuid4().hex[:8]}"


def _print_node_statuses(state) -> None:  # noqa: ANN001
    table = Table(title=f"Run {state.run_id} — {state.status.value}")
    table.add_column("Node")
    table.add_column("Status")
    for node_id, node_run in state.nodes.items():
        table.add_row(node_id, node_run.status.value)
    console.print(table)

    pending_approvals = [nid for nid, nr in state.nodes.items() if nr.status.value == "AWAITING_APPROVAL"]
    if pending_approvals:
        console.print(f"[yellow]Awaiting approval:[/yellow] {', '.join(pending_approvals)}")
        console.print(f"  agentic approvals show {state.run_id} <node>")
        console.print(f"  agentic approve {state.run_id} <node>")
        console.print(f"  agentic resume {state.run_id}")


@app.command()
def run(
    workflow: str = typer.Argument(..., help="Workflow name: greenfield | brownfield | ambiguous"),
    mode: str = typer.Option("replay", "--mode", help="LLM mode: live | replay"),
    clarification_answer: str = typer.Option(
        ambiguous.DEFAULT_CLARIFICATION_ANSWER, "--clarification-answer",
        help="Answer req.clarify will use once approved (ambiguous workflow only).",
    ),
) -> None:
    """Start a new orchestration run for the given workflow.

    Each workflow's raw requirement text is fixed (see
    workflows/<name>.py's RAW_REQUIREMENT) because --mode replay's
    cassette is keyed to prompts built from it; there is no --input
    override here that would still hit the cassette."""
    if workflow not in _KNOWN_WORKFLOWS:
        console.print(f"[red]unknown workflow: {workflow} (known: {', '.join(_KNOWN_WORKFLOWS)})[/red]")
        raise typer.Exit(code=1)

    run_id = _new_run_id(workflow)
    try:
        engine = runtime.new_engine(workflow, run_id, mode=mode, clarification_answer=clarification_answer)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    engine.start(scenario=workflow, workflow=workflow)
    state = asyncio.run(engine.run())
    _print_node_statuses(state)


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="Run id to resume."),
    mode: str = typer.Option("replay", "--mode", help="LLM mode: live | replay"),
) -> None:
    """Resume a run that is persisted (e.g. paused at an approval checkpoint)."""
    try:
        engine = runtime.load_engine(run_id, mode=mode)
    except runtime.UnknownRun as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    assert engine.state is not None
    state = asyncio.run(engine.run())
    _print_node_statuses(state)


@app.command()
def approve(
    run_id: str = typer.Argument(...),
    node_id: str = typer.Argument(...),
    note: str = typer.Option("", "--note"),
) -> None:
    """Grant approval for a node awaiting a human checkpoint."""
    try:
        engine = runtime.load_engine(run_id)
    except runtime.UnknownRun as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    assert engine.state is not None
    engine.grant_approval(node_id, note=note)
    engine.store.save(engine.state)
    console.print(f"[green]approved[/green] {node_id} on run {run_id}")
    console.print(f"  agentic resume {run_id}")


@app.command()
def reject(
    run_id: str = typer.Argument(...),
    node_id: str = typer.Argument(...),
    note: str = typer.Option("", "--note"),
) -> None:
    """Reject a node awaiting a human checkpoint."""
    try:
        engine = runtime.load_engine(run_id)
    except runtime.UnknownRun as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    assert engine.state is not None
    engine.reject_approval(node_id, note=note)
    engine.store.save(engine.state)
    console.print(f"[yellow]rejected[/yellow] {node_id} on run {run_id}")


@app.command()
def report(
    run_id: str = typer.Argument(...),
    format: str = typer.Option("md", "--format", help="md | html | json"),
) -> None:
    """Render the run report."""
    try:
        engine = runtime.load_engine(run_id)
    except runtime.UnknownRun as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    assert engine.state is not None
    events = engine.events.read()
    metrics = MetricsCollector().compute(events)
    audit = verify_run_audit(run_id, engine.events)
    approvals = reconstruct_approvals_from_events(events)
    try:
        summary_payload = engine.context.get("engineering_summary").payload
        engineering_summary = summary_payload.get("summary", "") if isinstance(summary_payload, dict) else ""
    except KeyError:
        engineering_summary = ""
    data = ReportData(
        run_state=engine.state, graph=engine.graph, events=events, metrics=metrics,
        context=engine.context, approvals=approvals, audit=audit,
        engineering_summary=engineering_summary,
    )

    rdir = runtime.run_dir(run_id)
    if format == "md":
        out_path = rdir / "report.md"
        out_path.write_text(render_markdown(data), encoding="utf-8")
    elif format == "html":
        out_path = rdir / "report.html"
        out_path.write_text(render_html(data), encoding="utf-8")
    elif format == "json":
        out_path = rdir / "report.json"
        out_path.write_text(engine.state.model_dump_json(indent=2), encoding="utf-8")
    else:
        console.print(f"[red]unknown format: {format} (use md | html | json)[/red]")
        raise typer.Exit(code=1)

    console.print(f"wrote {out_path}")


@app.command()
def lineage(
    run_id: str = typer.Argument(...),
    artifact: str = typer.Option(..., "--artifact", help="Artifact name to trace."),
) -> None:
    """Show full decision/artifact lineage for a named artifact."""
    try:
        engine = runtime.load_engine(run_id)
    except runtime.UnknownRun as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    try:
        target = engine.context.get(artifact)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    graph = engine.context.lineage(target.artifact_id)
    console.print(f"[bold]lineage for '{artifact}'[/bold] (run {run_id})")
    console.print(f"  artifacts: {[a.name + ':v' + str(a.version) for a in graph.artifacts]}")
    console.print(f"  decisions: {[d.statement for d in graph.decisions]}")
    console.print(f"  nodes: {graph.nodes}")
    console.print(f"  agents: {graph.agents}")


@app.command()
def replan(
    run_id: str = typer.Argument(...),
    node: str = typer.Option(..., "--node", help="Node id whose changed output triggers the re-plan."),
    input: str = typer.Option(..., "--input", help="Revised guidance / clarification answer."),
) -> None:
    """Trigger an explicit dynamic re-plan of a run."""
    try:
        engine = runtime.load_engine(run_id)
    except runtime.UnknownRun as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    assert engine.state is not None
    artifact_name = "clarification_answer" if node == "req.clarify" else f"{node}_replan_input"
    payload = {"text": input}
    engine.context.put(Artifact(
        artifact_id=str(uuid4()), name=artifact_name, kind="spec",
        content_hash=compute_content_hash(payload), payload=payload,
        produced_by_node=node, produced_by_agent="human", run_id=run_id, created_at=datetime.now(UTC),
    ))
    try:
        engine.replan_now(node)
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator, not a crash
        console.print(f"[red]replan failed: {exc}[/red]")
        raise typer.Exit(code=1) from None

    console.print(f"[green]re-plan applied[/green] from {node} on run {run_id}")
    console.print(f"  agentic resume {run_id}")


@app.command()
def halt(run_id: str = typer.Argument(...)) -> None:
    """Safe-stop a running or paused run."""
    try:
        engine = runtime.load_engine(run_id)
    except runtime.UnknownRun as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    assert engine.state is not None
    engine.halt("operator halt via CLI")
    console.print(f"[yellow]halted[/yellow] run {run_id}")


@app.command(name="verify-audit")
def verify_audit(run_id: str = typer.Argument(...)) -> None:
    """Walk the hash-chained event log and report any break."""
    try:
        engine = runtime.load_engine(run_id)
    except runtime.UnknownRun as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    assert engine.state is not None
    result = verify_run_audit(run_id, engine.events)
    rendered = render_audit_report(result)
    if result.ok:
        console.print(f"[green]{rendered}[/green]")
    else:
        console.print(f"[red]{rendered}[/red]")
        raise typer.Exit(code=1)


@approvals_app.command(name="list")
def approvals_list(run_id: str = typer.Argument(...)) -> None:
    """List pending and resolved approval checkpoints for a run."""
    try:
        engine = runtime.load_engine(run_id)
    except runtime.UnknownRun as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    assert engine.state is not None
    records = reconstruct_approvals_from_events(engine.events.read())
    if not records:
        console.print("no approval checkpoints recorded yet")
        return
    table = Table(title=f"Approvals — run {run_id}")
    table.add_column("Node")
    table.add_column("Status")
    table.add_column("Approver")
    table.add_column("Note")
    for record in records:
        table.add_row(record.node_id, record.status, record.approver or "-", record.note or "")
    console.print(table)


@approvals_app.command(name="show")
def approvals_show(run_id: str = typer.Argument(...), node_id: str = typer.Argument(...)) -> None:
    """Show the rendered decision package for one approval checkpoint."""
    try:
        engine = runtime.load_engine(run_id)
    except runtime.UnknownRun as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    assert engine.state is not None
    node_run = engine.state.nodes.get(node_id)
    if node_run is None:
        console.print(f"[red]no such node: {node_id}[/red]")
        raise typer.Exit(code=1)

    manager = ApprovalManager()
    package = manager.render_package(engine.graph[node_id], node_run, engine.context)
    console.print(f"[bold]Decision package: {node_id}[/bold] (run {run_id})")
    console.print(f"  stage: {package.stage}")
    console.print(f"  input_hash: {package.input_hash}")
    console.print(f"  artifacts: {[a.name for a in package.artifacts]}")
    for decision in package.decisions:
        console.print(f"  decision: {decision.statement} — {decision.rationale}")
    console.print(f"  consequence of rejection: {package.consequence_of_rejection}")


if __name__ == "__main__":
    app()
