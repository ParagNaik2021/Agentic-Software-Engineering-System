"""Typer entrypoint for the `agentic` command-line control plane
(Section 14). Command bodies delegate to runtime.py for engine
construction so this module stays argument-parsing and presentation.
"""

from __future__ import annotations

import asyncio
import contextlib
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import typer
from rich.console import Console
from rich.table import Table

from agentic import runtime, service
from agentic.core.models import Artifact, RunStatus, compute_content_hash
from agentic.core.states import NodeStatus
from agentic.governance.approvals import ApprovalManager, ApprovalNotPermitted
from agentic.governance.rendering import render_docx, render_text
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


# Workflows whose graph actually reaches an implementation stage and so
# regenerates the app under workspace/urlshortener. `ambiguous` joined
# these when it was moved onto common.build_canonical_graph(): it runs the
# same implementation and verification fan-out as greenfield, so the app
# it serves is one it produced itself.
_GENERATING_WORKFLOWS = ("greenfield", "brownfield", "ambiguous")


def _maybe_autostart_service(state, port: int, no_serve: bool) -> None:  # noqa: ANN001
    """Post-success convenience step: if this run just reached SUCCEEDED
    with its summary node SUCCEEDED, and it's a workflow that generates a
    runnable app, launch that app (with the tester UI mounted) as a
    detached background process. Entirely separate from orchestration —
    never touches engine/event-log state, so --mode replay determinism
    and the existing test suite are unaffected."""
    if no_serve:
        return
    if state.status != RunStatus.SUCCEEDED:
        return
    summary_node = state.nodes.get("summary")
    if summary_node is None or summary_node.status != NodeStatus.SUCCEEDED:
        return

    settings = runtime.get_settings()
    workspace_dir = settings.workspace_dir
    if not service.app_is_built(workspace_dir):
        # Only reachable for a non-generating workflow with no prior app
        # on disk; a generating workflow that succeeded has written one.
        console.print(
            f"[yellow]nothing to serve: the '{state.workflow}' workflow generates no code and "
            f"{workspace_dir} holds no app yet — run `agentic run greenfield` first.[/yellow]"
        )
        return

    info = service.start_service(workspace_dir, port)
    if state.workflow not in _GENERATING_WORKFLOWS:
        console.print(
            f"[cyan]note:[/cyan] the '{state.workflow}' workflow produces no code — "
            "serving the app left by the most recent greenfield/brownfield run."
        )

    if info.note:
        console.print(f"[yellow]{info.note}[/yellow]")
    console.print(f"[green]Service started:[/green] http://localhost:{info.port}")
    console.print(f"[green]Tester UI:[/green]        http://localhost:{info.port}/tester")
    console.print(f"  pid {info.pid} — to stop: `agentic stop` (or {service.stop_hint(info.pid)})")

    if not info.healthy:
        console.print(
            "[red]warning: /healthz did not respond within the startup timeout "
            "— the service may still be starting or may have failed; check the pid above.[/red]"
        )
        return

    opened = False
    with contextlib.suppress(Exception):
        opened = webbrowser.open(f"http://localhost:{info.port}/tester")
    if not opened:
        console.print(
            "[yellow]couldn't auto-open a browser here — open the Tester UI link above manually.[/yellow]"
        )


@app.command()
def run(
    workflow: str = typer.Argument(..., help="Workflow name: greenfield | brownfield | ambiguous"),
    mode: str = typer.Option("replay", "--mode", help="LLM mode: live | replay"),
    clarification_answer: str = typer.Option(
        ambiguous.DEFAULT_CLARIFICATION_ANSWER, "--clarification-answer",
        help="Answer req.clarify will use once approved (ambiguous workflow only).",
    ),
    watch: bool = typer.Option(
        False, "--watch", help="Open the watch GUI immediately instead of running headless."
    ),
    port: int = typer.Option(
        service.DEFAULT_PORT, "--port",
        help="Port for the auto-started generated app once the run reaches SUCCEEDED.",
    ),
    no_serve: bool = typer.Option(
        False, "--no-serve",
        help="Skip auto-starting the generated app after a successful run (for CI/automated testing).",
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

    if watch:
        from agentic.gui.watch import launch_watch_window

        console.print(f"opened watch GUI for run {run_id}")
        launch_watch_window(engine)
        return

    state = asyncio.run(engine.run())
    _print_node_statuses(state)
    _maybe_autostart_service(state, port, no_serve)


@app.command()
def watch(
    run_id: str = typer.Argument(..., help="Run id to watch."),
    mode: str = typer.Option("replay", "--mode", help="LLM mode: live | replay"),
) -> None:
    """Open a GUI window that advances the run and pops up the right
    control whenever a node pauses for a human: Approve/Reject for an
    approval gate, or the ambiguity agent's questions plus an answer box
    and Submit for a CLARIFICATION-stage node (ambiguous's req.clarify).
    Replaces manually running `approvals show` / `approve` / `resume`,
    and for clarification replaces having to pass
    `--clarification-answer` before the run even starts.
    """
    from agentic.gui.watch import run_watch_gui  # tkinter import stays optional until used

    try:
        run_watch_gui(run_id, mode=mode)
    except runtime.UnknownRun as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="Run id to resume."),
    mode: str = typer.Option("replay", "--mode", help="LLM mode: live | replay"),
    port: int = typer.Option(
        service.DEFAULT_PORT, "--port",
        help="Port for the auto-started generated app if this resume brings the run to SUCCEEDED.",
    ),
    no_serve: bool = typer.Option(
        False, "--no-serve",
        help="Skip auto-starting the generated app after a successful run (for CI/automated testing).",
    ),
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
    _maybe_autostart_service(state, port, no_serve)


@app.command()
def stop() -> None:
    """Stop the auto-started app server, so a CLI session doesn't leave
    an orphaned uvicorn process running after you're done with it."""
    stopped = service.stop_service(runtime.get_settings().workspace_dir)
    if stopped is None:
        console.print("no agentic-started service is running")
        return
    pid, port = stopped
    console.print(f"[yellow]stopped[/yellow] service on http://localhost:{port} (pid {pid})")


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

    try:
        runtime.approve_and_save(engine, node_id, note=note)
    except ApprovalNotPermitted as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
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

    try:
        runtime.reject_and_save(engine, node_id, note=note)
    except ApprovalNotPermitted as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
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
    input: str = typer.Option(
        "", "--input", help="Revised guidance / clarification answer. Required unless --from-rejection."
    ),
    from_rejection: bool = typer.Option(
        False, "--from-rejection",
        help=(
            "Recover a REJECTED approval gate: re-opens the nodes it depends_on with its stored "
            "rejection note attached, then returns it to PENDING. Distinct from the --input path "
            "(input-hash-mismatch replan) in the audit trail."
        ),
    ),
) -> None:
    """Trigger an explicit re-plan of a run — either from revised
    upstream guidance (--input) or from an operator recovering a
    REJECTED approval gate (--from-rejection)."""
    try:
        engine = runtime.load_engine(run_id)
    except runtime.UnknownRun as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    assert engine.state is not None

    if from_rejection:
        try:
            asyncio.run(engine.replan_from_rejection(node))
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator, not a crash
            console.print(f"[red]replan failed: {exc}[/red]")
            raise typer.Exit(code=1) from None
        console.print(f"[green]recovered[/green] {node} from rejection on run {run_id}")
        console.print(f"  agentic resume {run_id}")
        return

    if not input:
        console.print("[red]--input is required unless --from-rejection is set[/red]")
        raise typer.Exit(code=1)

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
    package = manager.render_package(
        engine.graph[node_id], node_run, engine.context, policy_verdicts=node_run.policy_verdicts
    )
    console.print(render_text(package), markup=False, highlight=False)


@approvals_app.command(name="export")
def approvals_export(
    run_id: str = typer.Argument(...),
    node_id: str = typer.Argument(...),
    format: str = typer.Option("docx", "--format", help="docx (only format currently supported)"),
    output: str = typer.Option("", "--output", help="Output path (default: <run_id>-<node_id>.docx)"),
) -> None:
    """Export the same decision package `approvals show` prints as a
    formatted Word document, for review outside the terminal."""
    if format != "docx":
        console.print(f"[red]unknown format: {format} (use 'docx')[/red]")
        raise typer.Exit(code=1)

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
    package = manager.render_package(
        engine.graph[node_id], node_run, engine.context, policy_verdicts=node_run.policy_verdicts
    )
    out_path = Path(output) if output else Path(f"{run_id}-{node_id}.docx")
    render_docx(package, out_path)
    console.print(f"wrote {out_path}")


if __name__ == "__main__":
    app()
