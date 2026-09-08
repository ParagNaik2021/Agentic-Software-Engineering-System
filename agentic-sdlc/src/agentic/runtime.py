"""CLI runtime glue: constructing a provider/Engine for a named workflow
and reconstructing an Engine for an existing run_id from disk. Kept
separate from cli.py so command bodies stay thin argument-parsing/
presentation code, and so this can be unit-tested without invoking Typer.
"""

from __future__ import annotations

from pathlib import Path

from agentic.config import Settings, get_settings
from agentic.core.context import ContextStore
from agentic.core.engine import Engine, NodeExecutor
from agentic.core.events import EventLog, EventType
from agentic.core.store import RunStore
from agentic.governance.recovery import RecoveryManager
from agentic.llm.cassette import Cassette
from agentic.llm.live import LiveProvider
from agentic.llm.provider import LLMProvider
from agentic.llm.replay import ReplayProvider
from agentic.workflows import ambiguous, brownfield, greenfield
from agentic.workflows.registry import get_builder


class UnknownRun(Exception):
    pass


def prompts_dir(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).prompts_dir


def cassette_path(workflow: str, settings: Settings | None = None) -> Path:
    return (settings or get_settings()).cassettes_dir / f"{workflow}.json"


def workspace_root(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).workspace_dir


def run_dir(run_id: str, settings: Settings | None = None) -> Path:
    return (settings or get_settings()).run_dir(run_id)


def build_provider(mode: str, workflow: str, settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    cassette = Cassette(cassette_path(workflow, settings))
    if mode == "replay":
        return ReplayProvider(cassette, on_miss=settings.replay_on_miss)
    if mode == "live":
        if not settings.anthropic_api_key:
            raise ValueError("--mode live requires ANTHROPIC_API_KEY to be set (see .env.example)")
        return LiveProvider(api_key=settings.anthropic_api_key, cassette=cassette)
    raise ValueError(f"unsupported --mode for the CLI: {mode!r} (use 'replay' or 'live')")


def _executors_for(
    workflow: str, provider: LLMProvider, run_id: str, settings: Settings, clarification_answer: str
) -> tuple[dict[str, NodeExecutor], dict[str, NodeExecutor], dict]:
    pdir = prompts_dir(settings)
    if workflow == "greenfield":
        node_executors, executors_by_agent = greenfield.build_node_executors(
            provider, pdir, workspace_root(settings), run_id
        )
        return node_executors, executors_by_agent, {"plan.decompose": greenfield.expand_impl_tasks}
    if workflow == "brownfield":
        node_executors, executors_by_agent = brownfield.build_node_executors(
            provider, pdir, workspace_root(settings), run_id
        )
        return node_executors, executors_by_agent, {"plan.decompose": brownfield.expand_impl_tasks}
    if workflow == "ambiguous":
        node_executors = ambiguous.build_node_executors(provider, pdir, run_id, clarification_answer)
        return node_executors, {}, {}
    raise ValueError(f"unknown workflow: {workflow!r}")


def _build_engine(
    workflow: str, run_id: str, mode: str, settings: Settings, clarification_answer: str
) -> Engine:
    graph = get_builder(workflow)()
    rdir = run_dir(run_id, settings)
    context = ContextStore(graph, persist_dir=rdir / "artifacts")
    events = EventLog(path=rdir / "events.jsonl", run_id=run_id)
    store = RunStore(rdir / "state.db")
    provider = build_provider(mode, workflow, settings)
    node_executors, executors_by_agent, expanders = _executors_for(
        workflow, provider, run_id, settings, clarification_answer
    )
    return Engine(
        run_id=run_id, graph=graph, context=context, event_log=events, store=store,
        node_executors=node_executors, executors_by_agent=executors_by_agent,
        graph_expanders=expanders, recovery=RecoveryManager(),
        concurrency=settings.max_concurrent_nodes, replan_budget=settings.replan_budget,
    )


def new_engine(
    workflow: str,
    run_id: str,
    mode: str = "replay",
    settings: Settings | None = None,
    clarification_answer: str = ambiguous.DEFAULT_CLARIFICATION_ANSWER,
) -> Engine:
    return _build_engine(workflow, run_id, mode, settings or get_settings(), clarification_answer)


def _read_workflow_name(rdir: Path, run_id: str) -> str:
    events_path = rdir / "events.jsonl"
    if not events_path.exists():
        raise UnknownRun(f"no run found at {rdir}")
    for event in EventLog(path=events_path, run_id=run_id).read():
        if event.type == EventType.RUN_STARTED:
            return str(event.payload["workflow"])
    raise UnknownRun(f"run {run_id!r} has no RUN_STARTED event")


def load_engine(
    run_id: str,
    mode: str = "replay",
    settings: Settings | None = None,
    clarification_answer: str = ambiguous.DEFAULT_CLARIFICATION_ANSWER,
) -> Engine:
    """Reconstruct an Engine for an existing run_id — used by resume/
    approve/reject/replan/halt, all of which act on a run some earlier
    `agentic run` invocation started (possibly in a different process)."""
    settings = settings or get_settings()
    rdir = run_dir(run_id, settings)
    workflow = _read_workflow_name(rdir, run_id)
    graph = get_builder(workflow)()
    context = ContextStore(graph, persist_dir=rdir / "artifacts")
    events = EventLog(path=rdir / "events.jsonl", run_id=run_id)
    store = RunStore(rdir / "state.db")
    provider = build_provider(mode, workflow, settings)
    node_executors, executors_by_agent, expanders = _executors_for(
        workflow, provider, run_id, settings, clarification_answer
    )
    return Engine.resume(
        run_id=run_id, graph=graph, context=context, event_log=events, store=store,
        node_executors=node_executors, executors_by_agent=executors_by_agent,
        graph_expanders=expanders, recovery=RecoveryManager(),
        concurrency=settings.max_concurrent_nodes, replan_budget=settings.replan_budget,
    )
