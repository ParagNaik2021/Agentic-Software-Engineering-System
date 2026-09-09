"""Enhancement 1 (agentic watch): WatchController's state polling and
approve/reject dispatch, exercised without Tkinter (watch.py is
presentation-only and untested here on purpose — no display in CI).

Uses the ambiguous workflow because it reaches its first approval
checkpoint (req.clarify) in a handful of nodes, offline, against the
committed cassette.
"""

from pathlib import Path

import pytest

from agentic.core.context import ContextStore
from agentic.core.engine import Engine
from agentic.core.events import EventLog, EventType
from agentic.core.states import NodeStatus
from agentic.core.store import RunStore
from agentic.governance.rendering import render_text
from agentic.gui.watch_controller import WatchController
from agentic.llm.cassette import Cassette
from agentic.llm.replay import ReplayProvider
from agentic.workflows import ambiguous

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "src" / "agentic" / "llm" / "prompts"
CASSETTE_PATH = REPO_ROOT / "cassettes" / "ambiguous.json"
CLARIFICATION_ANSWER = ambiguous.DEFAULT_CLARIFICATION_ANSWER


def _make_controller(tmp_path: Path, run_id: str) -> WatchController:
    graph = ambiguous.build_graph()
    context = ContextStore(graph, persist_dir=tmp_path / "artifacts")
    events = EventLog(path=tmp_path / "events.jsonl", run_id=run_id)
    store = RunStore(tmp_path / "state.db")
    provider = ReplayProvider(Cassette(CASSETTE_PATH), on_miss="error")
    node_executors = ambiguous.build_node_executors(provider, PROMPTS_DIR, run_id, CLARIFICATION_ANSWER)
    engine = Engine(
        run_id=run_id, graph=graph, context=context, event_log=events, store=store,
        node_executors=node_executors,
    )
    engine.start(scenario="ambiguous", workflow="ambiguous")
    return WatchController(engine)


def test_poll_before_advance_shows_no_awaiting_and_not_terminal(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path, "watch-1")
    snapshot = controller.poll()
    assert snapshot.awaiting == []
    assert snapshot.is_terminal is False
    assert all(nr.status == NodeStatus.PENDING for nr in snapshot.state.nodes.values())


@pytest.mark.asyncio
async def test_advance_reaches_first_approval_checkpoint(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path, "watch-2")
    await controller.advance()

    snapshot = controller.poll()
    assert snapshot.awaiting == ["req.clarify"]
    assert snapshot.is_terminal is False
    assert snapshot.state.nodes["design.arch"].status == NodeStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_decision_package_for_awaiting_node_reflects_upstream_artifacts(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path, "watch-3")
    await controller.advance()

    package = controller.decision_package("req.clarify")
    assert package.node_id == "req.clarify"
    names = {a.name for a in package.artifacts}
    assert {"normalized_spec", "ambiguity_assessment", "clarification_questions"} <= names

    rendered = render_text(package)
    assert "req.clarify" in rendered
    assert "Normalized Requirement" in rendered


@pytest.mark.asyncio
async def test_approve_goes_through_runtime_helper_and_reaches_succeeded(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path, "watch-4")
    await controller.advance()

    controller.approve("req.clarify", note="looks right")
    await controller.advance()

    snapshot = controller.poll()
    assert snapshot.is_terminal is True
    assert snapshot.state.status.value == "SUCCEEDED"

    granted = [e for e in controller.engine.events.read() if e.type == EventType.APPROVAL_GRANTED]
    assert len(granted) == 1
    assert granted[0].payload["note"] == "looks right"


@pytest.mark.asyncio
async def test_reject_goes_through_runtime_helper_and_marks_node_rejected(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path, "watch-5")
    await controller.advance()

    controller.reject("req.clarify", note="need a different answer")
    await controller.advance()

    snapshot = controller.poll()
    assert snapshot.state.nodes["req.clarify"].status == NodeStatus.REJECTED
    assert snapshot.is_terminal is True

    rejected = [e for e in controller.engine.events.read() if e.type == EventType.APPROVAL_REJECTED]
    assert len(rejected) == 1
    assert rejected[0].payload["note"] == "need a different answer"


@pytest.mark.asyncio
async def test_poll_reflects_state_persisted_by_a_second_engine_instance(tmp_path: Path) -> None:
    """The polling contract watch.py relies on: poll() re-reads
    RunStore.load(), so a run advanced by a *different* Engine object
    pointed at the same state.db (e.g. a concurrent `agentic resume` in
    another process) is visible without reconstructing WatchController."""
    controller = _make_controller(tmp_path, "watch-6")
    await controller.advance()
    assert controller.poll().awaiting == ["req.clarify"]

    # simulate a second, independent process approving + resuming
    other_context = ContextStore(ambiguous.build_graph(), persist_dir=tmp_path / "artifacts")
    other_context.load_from_disk()
    other_events = EventLog(path=tmp_path / "events.jsonl", run_id="watch-6")
    other_store = RunStore(tmp_path / "state.db")
    other_provider = ReplayProvider(Cassette(CASSETTE_PATH), on_miss="error")
    other_executors = ambiguous.build_node_executors(
        other_provider, PROMPTS_DIR, "watch-6", CLARIFICATION_ANSWER
    )
    other_engine = Engine.resume(
        run_id="watch-6", graph=ambiguous.build_graph(), context=other_context,
        event_log=other_events, store=other_store, node_executors=other_executors,
    )
    other_engine.grant_approval("req.clarify", note="approved elsewhere")
    assert other_engine.state is not None
    other_engine.store.save(other_engine.state)
    await other_engine.run()

    snapshot = controller.poll()
    assert snapshot.state.status.value == "SUCCEEDED"
    assert snapshot.awaiting == []
