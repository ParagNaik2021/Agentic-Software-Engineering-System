"""Engine.replan_from_rejection() (the `agentic replan --from-rejection`
command): explicit, human-initiated recovery for a REJECTED approval
gate — distinct from trigger_replan()'s input-hash-mismatch path.

Graph shape mirrors design.arch/design.data -> design.review:
    intake -> [design_a, design_b] -> gate (requires_approval, no agent) -> downstream
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentic.core.context import ContextStore
from agentic.core.engine import Engine, NodeExecutionResult
from agentic.core.events import EventLog, EventType
from agentic.core.graph import WorkflowGraph
from agentic.core.models import NodeSpec, SDLCStage
from agentic.core.states import NodeStatus
from agentic.core.store import RunStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _graph() -> WorkflowGraph:
    graph = WorkflowGraph(root="intake")
    graph.add_node(NodeSpec(node_id="intake", stage=SDLCStage.INTAKE))
    graph.add_node(NodeSpec(node_id="design_a", stage=SDLCStage.ARCHITECTURE, depends_on=["intake"]))
    graph.add_node(NodeSpec(node_id="design_b", stage=SDLCStage.DATA_DESIGN, depends_on=["intake"]))
    graph.add_node(
        NodeSpec(
            node_id="gate", stage=SDLCStage.DESIGN_REVIEW, depends_on=["design_a", "design_b"],
            join_policy="all", requires_approval=True,
        )
    )
    graph.add_node(NodeSpec(node_id="downstream", stage=SDLCStage.IMPLEMENTATION, depends_on=["gate"]))
    graph.validate()
    return graph


async def _noop(node, view) -> NodeExecutionResult:  # noqa: ANN001
    return NodeExecutionResult()


def _make_engine(tmp_path: Path) -> Engine:
    graph = _graph()
    context = ContextStore(graph, persist_dir=tmp_path / "artifacts")
    events = EventLog(path=tmp_path / "events.jsonl", run_id="run-1")
    store = RunStore(tmp_path / "state.db")
    engine = Engine(
        run_id="run-1", graph=graph, context=context, event_log=events, store=store,
        node_executors={"intake": _noop, "design_a": _noop, "design_b": _noop, "downstream": _noop},
    )
    engine.start(scenario="test", workflow="test")
    return engine


@pytest.mark.asyncio
async def test_rejection_alone_never_calls_replan_from_rejection(tmp_path: Path) -> None:
    """The explicit design intent: rejecting halts, and stays halted,
    until a human deliberately runs the recovery command — it must never
    fire on its own."""
    engine = _make_engine(tmp_path)
    await engine.run()  # design_a, design_b succeed; gate -> AWAITING_APPROVAL

    engine.reject_approval("gate", note="needs rework")
    state = await engine.run()

    assert state.nodes["gate"].status == NodeStatus.REJECTED
    assert state.status.value == "HALTED"
    assert state.nodes["design_a"].status == NodeStatus.SUCCEEDED  # untouched
    assert state.nodes["design_b"].status == NodeStatus.SUCCEEDED  # untouched
    replans = [e for e in engine.events.read() if e.type == EventType.REPLAN_TRIGGERED]
    assert replans == []  # nothing replanned itself


@pytest.mark.asyncio
async def test_replan_from_rejection_reopens_depends_on_and_the_gate_itself(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    await engine.run()
    engine.reject_approval("gate", note="Add analytics support and SQLite persistence before proceeding")
    await engine.run()

    await engine.replan_from_rejection("gate")

    assert engine.state is not None
    assert engine.state.nodes["design_a"].status == NodeStatus.PENDING
    assert engine.state.nodes["design_b"].status == NodeStatus.PENDING
    assert engine.state.nodes["gate"].status == NodeStatus.PENDING
    assert engine.state.replan_count == 1
    assert engine.state.metrics.replans == 1


@pytest.mark.asyncio
async def test_replan_from_rejection_note_is_injected_into_context(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    await engine.run()
    note = "Add analytics support and SQLite persistence before proceeding"
    engine.reject_approval("gate", note=note)
    await engine.run()

    await engine.replan_from_rejection("gate")

    feedback = engine.context.get("gate_rejection_feedback")
    assert feedback.payload == {"text": note}
    assert feedback.produced_by_agent == "human"


@pytest.mark.asyncio
async def test_replan_from_rejection_emits_a_distinct_trigger_from_input_hash_mismatch(tmp_path: Path) -> None:
    """The audit-trail requirement: rejection-triggered replans must not
    be conflated with the existing upstream-artifact-change path."""
    engine = _make_engine(tmp_path)
    await engine.run()
    note = "Add analytics support and SQLite persistence before proceeding"
    engine.reject_approval("gate", note=note)
    await engine.run()

    await engine.replan_from_rejection("gate")

    replan_events = [e for e in engine.events.read() if e.type == EventType.REPLAN_TRIGGERED]
    assert len(replan_events) == 1
    payload = replan_events[0].payload
    assert payload["trigger"] == "rejection"
    assert payload["changed_node"] == "gate"
    assert set(payload["invalidated"]) == {"design_a", "design_b"}
    assert payload["note"] == note

    invalidated_events = [e for e in engine.events.read() if e.type == EventType.NODE_INVALIDATED]
    assert {e.node_id for e in invalidated_events} == {"design_a", "design_b"}
    for e in invalidated_events:
        assert e.payload["reason"] == "rejection_feedback"
        assert e.payload["source_node"] == "gate"

    rollback_events = [e.type for e in engine.events.read() if "ROLLBACK" in e.type.value]
    assert len(rollback_events) == 2  # ROLLBACK_STARTED + ROLLBACK_COMPLETED for the gate itself


@pytest.mark.asyncio
async def test_replan_from_rejection_only_reopens_succeeded_dependencies(tmp_path: Path) -> None:
    """If a dependency never actually succeeded (edge case, shouldn't
    normally happen once the gate itself reached AWAITING_APPROVAL, but
    the implementation must not blow up on it), it's left alone rather
    than illegally transitioned."""
    engine = _make_engine(tmp_path)
    await engine.run()
    engine.reject_approval("gate", note="rework")
    await engine.run()

    assert engine.state is not None
    engine.state.nodes["design_b"].status = NodeStatus.PENDING  # simulate "never actually ran"

    await engine.replan_from_rejection("gate")

    assert engine.state.nodes["design_a"].status == NodeStatus.PENDING  # invalidated then reset
    assert engine.state.nodes["design_b"].status == NodeStatus.PENDING  # left alone, not touched
    replan_events = [e for e in engine.events.read() if e.type == EventType.REPLAN_TRIGGERED]
    assert set(replan_events[-1].payload["invalidated"]) == {"design_a"}


@pytest.mark.asyncio
async def test_replan_from_rejection_raises_if_node_is_not_rejected(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    await engine.run()  # gate is AWAITING_APPROVAL, not REJECTED

    with pytest.raises(ValueError, match="not REJECTED"):
        await engine.replan_from_rejection("gate")


@pytest.mark.asyncio
async def test_full_sequence_reaches_a_new_awaiting_approval_after_dependencies_resucceed(tmp_path: Path) -> None:
    """The end-to-end shape the CLI demo exercises: reject -> halt ->
    --from-rejection -> dependencies re-run -> gate re-evaluates."""
    engine = _make_engine(tmp_path)
    await engine.run()
    engine.reject_approval("gate", note="Add analytics support and SQLite persistence before proceeding")
    first_halt = await engine.run()
    assert first_halt.status.value == "HALTED"

    await engine.replan_from_rejection("gate")
    final_state = await engine.run()  # design_a/design_b re-run (stub executors, always succeed)

    assert final_state.nodes["design_a"].status == NodeStatus.SUCCEEDED
    assert final_state.nodes["design_b"].status == NodeStatus.SUCCEEDED
    assert final_state.nodes["design_a"].attempt == 2  # genuinely re-executed, not just reset
    assert final_state.nodes["gate"].status == NodeStatus.AWAITING_APPROVAL
    assert final_state.status.value == "AWAITING_APPROVAL"
