"""Typer entrypoint for the `agentic` command-line control plane.

Command bodies are filled in as the phases that back them land
(engine in P2, governance in P3, observability in P4, ...). P0 only
guarantees that every command the spec names exists, is discoverable
via --help, and fails loudly rather than silently if invoked early.
"""

from __future__ import annotations

import typer
from rich.console import Console

console = Console()

app = typer.Typer(
    name="agentic",
    help="Agentic SDLC Orchestration System — control plane.",
    no_args_is_help=True,
)

approvals_app = typer.Typer(help="Inspect and manage human approval checkpoints.")
app.add_typer(approvals_app, name="approvals")


def _not_yet_implemented(feature: str) -> None:
    console.print(f"[yellow]{feature} is not yet implemented (scaffolding phase P0).[/yellow]")
    raise typer.Exit(code=0)


@app.command()
def run(
    workflow: str = typer.Argument(..., help="Workflow name: greenfield | brownfield | ambiguous"),
    mode: str = typer.Option("replay", "--mode", help="LLM mode: live | replay | mock"),
    input: str | None = typer.Option(None, "--input", help="Raw requirement text."),
    inject_fault: str | None = typer.Option(
        None, "--inject-fault", help="Fault profile, e.g. 'node=impl.core_service,kind=transient,count=2'"
    ),
    record: bool = typer.Option(False, "--record", help="Record LLM interactions to cassettes/."),
) -> None:
    """Start a new orchestration run for the given workflow."""
    _not_yet_implemented("run")


@app.command()
def resume(run_id: str = typer.Argument(..., help="Run id to resume.")) -> None:
    """Resume a run that is persisted (e.g. paused at an approval checkpoint)."""
    _not_yet_implemented("resume")


@app.command()
def approve(
    run_id: str = typer.Argument(...),
    node_id: str = typer.Argument(...),
    note: str | None = typer.Option(None, "--note"),
) -> None:
    """Grant approval for a node awaiting a human checkpoint."""
    _not_yet_implemented("approve")


@app.command()
def reject(
    run_id: str = typer.Argument(...),
    node_id: str = typer.Argument(...),
    note: str | None = typer.Option(None, "--note"),
) -> None:
    """Reject a node awaiting a human checkpoint."""
    _not_yet_implemented("reject")


@app.command()
def report(
    run_id: str = typer.Argument(...),
    format: str = typer.Option("md", "--format", help="md | html | json"),
) -> None:
    """Render the run report."""
    _not_yet_implemented("report")


@app.command()
def lineage(
    run_id: str = typer.Argument(...),
    artifact: str = typer.Option(..., "--artifact", help="Artifact name to trace."),
) -> None:
    """Show full decision/artifact lineage for a named artifact."""
    _not_yet_implemented("lineage")


@app.command()
def replan(
    run_id: str = typer.Argument(...),
    node: str = typer.Option(..., "--node", help="Node id to re-plan from."),
    input: str = typer.Option(..., "--input", help="Revised guidance / clarification answer."),
) -> None:
    """Trigger an explicit dynamic re-plan of a run."""
    _not_yet_implemented("replan")


@app.command()
def halt(run_id: str = typer.Argument(...)) -> None:
    """Safe-stop a running or paused run."""
    _not_yet_implemented("halt")


@app.command(name="verify-audit")
def verify_audit(run_id: str = typer.Argument(...)) -> None:
    """Walk the hash-chained event log and report any break."""
    _not_yet_implemented("verify-audit")


@approvals_app.command(name="list")
def approvals_list(run_id: str = typer.Argument(...)) -> None:
    """List pending and resolved approval checkpoints for a run."""
    _not_yet_implemented("approvals list")


@approvals_app.command(name="show")
def approvals_show(run_id: str = typer.Argument(...), node_id: str = typer.Argument(...)) -> None:
    """Show the rendered decision package for one approval checkpoint."""
    _not_yet_implemented("approvals show")


if __name__ == "__main__":
    app()
