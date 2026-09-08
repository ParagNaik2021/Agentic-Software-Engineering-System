"""Engine <-> RecoveryManager wiring (Section 6.4): retry, fallback,
rollback and safe-stop actually happen, not just get classified."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentic.core.context import ContextStore
from agentic.core.engine import Engine, NodeExecutionResult
from agentic.core.events import EventLog, EventType
from agentic.core.graph import WorkflowGraph
from agentic.core.models import (
    Artifact,
    ErrorClass,
    GateConditionSpec,
    GateSpec,
    NodeSpec,
    RetryPolicy,
    RunStatus,
    SDLCStage,
    compute_content_hash,
)
from agentic.core.states import NodeStatus
from agentic.core.store import RunStore
from agentic.governance.recovery import RecoveryManager
from agentic.llm.provider import TransientProviderError


def _graph(retry: RetryPolicy | None = None) -> WorkflowGraph:
    graph = WorkflowGraph(root="intake")
    graph.add_node(NodeSpec(node_id="intake", stage=SDLCStage.INTAKE))
    graph.add_node(
        NodeSpec(
            node_id="flaky", stage=SDLCStage.IMPLEMENTATION, agent="implementer",
            depends_on=["intake"], retry=retry or RetryPolicy(max_attempts=3, base_seconds=0.0, cap_seconds=0.0),
        )
    )
    graph.add_node(NodeSpec(node_id="downstream", stage=SDLCStage.UNIT_TEST, depends_on=["flaky"]))
    graph.validate()
    return graph


async def _noop(node, view) -> NodeExecutionResult:
    return NodeExecutionResult()


def _engine(tmp_path: Path, executor, graph=None, recovery: RecoveryManager | None = None, rollback_handlers=None) -> Engine:
    graph = graph or _graph()
    context = ContextStore(graph)
    events = EventLog(path=tmp_path / "events.jsonl", run_id="run-1")
    store = RunStore(tmp_path / "state.db")
    engine = Engine(
        run_id="run-1", graph=graph, context=context, event_log=events, store=store,
        node_executors={"intake": _noop, "flaky": executor, "downstream": _noop},
        recovery=recovery or RecoveryManager(),
        rollback_handlers=rollback_handlers,
    )
    engine.start(scenario="s", workflow="w")
    return engine


@pytest.mark.asyncio
async def test_transient_failure_retries_then_succeeds(tmp_path: Path) -> None:
    calls = {"n": 0}

    async def executor(node, view) -> NodeExecutionResult:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientProviderError("simulated transient failure")
        return NodeExecutionResult()

    engine = _engine(tmp_path, executor)
    final_state = await engine.run()

    assert final_state.status == RunStatus.SUCCEEDED
    assert calls["n"] == 3
    assert final_state.nodes["flaky"].attempt == 3
    retry_events = [e for e in engine.events.read() if e.type == EventType.RETRY_SCHEDULED]
    assert len(retry_events) == 2


@pytest.mark.asyncio
async def test_transient_failure_exhausts_retries_and_routes_to_rollback(tmp_path: Path) -> None:
    async def always_fails(node, view) -> NodeExecutionResult:
        raise TransientProviderError("always fails")

    rolled_back = {"called": False}

    def rollback_handler():
        rolled_back["called"] = True

    engine = _engine(
        tmp_path, always_fails,
        graph=_graph(RetryPolicy(max_attempts=2, base_seconds=0.0, cap_seconds=0.0)),
        rollback_handlers={"flaky": rollback_handler},
    )
    final_state = await engine.run()

    assert final_state.nodes["flaky"].status == NodeStatus.ROLLED_BACK
    assert rolled_back["called"] is True
    rollback_started = [e for e in engine.events.read() if e.type == EventType.ROLLBACK_STARTED]
    rollback_completed = [e for e in engine.events.read() if e.type == EventType.ROLLBACK_COMPLETED]
    assert len(rollback_started) == 1
    assert len(rollback_completed) == 1
    # downstream never runs; nothing else can become ready -> safe-stop
    assert final_state.status == RunStatus.HALTED
    assert final_state.nodes["downstream"].status == NodeStatus.PENDING


@pytest.mark.asyncio
async def test_quality_failure_falls_back_then_rolls_back(tmp_path: Path) -> None:
    """A quality (exit-gate) failure that persists across the fallback
    attempt is exactly Section 9.3's demo: fallback, then rollback."""
    graph = WorkflowGraph(root="intake")
    graph.add_node(NodeSpec(node_id="intake", stage=SDLCStage.INTAKE))
    graph.add_node(
        NodeSpec(
            node_id="verify.unit", stage=SDLCStage.UNIT_TEST, depends_on=["intake"],
            exit_gate=GateSpec(conditions=[GateConditionSpec(type="tests_pass")]),
        )
    )
    graph.validate()
    context = ContextStore(graph)
    events = EventLog(path=tmp_path / "events.jsonl", run_id="run-1")
    store = RunStore(tmp_path / "state.db")

    call_count = {"n": 0}

    async def broken_tests(node, view) -> NodeExecutionResult:
        call_count["n"] += 1
        payload = {"exit_code": 1, "coverage": 0.5}
        artifact = Artifact(
            artifact_id=f"tr-{call_count['n']}", name="test_results", kind="report",
            content_hash=compute_content_hash(payload), payload=payload,
            produced_by_node=node.node_id, produced_by_agent="system", run_id="run-1",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        return NodeExecutionResult(artifacts=[artifact])

    rolled_back = {"called": False}
    engine = Engine(
        run_id="run-1", graph=graph, context=context, event_log=events, store=store,
        node_executors={"intake": _noop, "verify.unit": broken_tests},
        recovery=RecoveryManager(),
        rollback_handlers={"verify.unit": lambda: rolled_back.__setitem__("called", True)},
    )
    engine.start(scenario="s", workflow="w")
    final_state = await engine.run()

    assert final_state.nodes["verify.unit"].error is not None
    assert final_state.nodes["verify.unit"].error.error_class == ErrorClass.QUALITY_FAILURE
    fallback_events = [e for e in engine.events.read() if e.type == EventType.FALLBACK_ENGAGED]
    assert len(fallback_events) == 1
    assert rolled_back["called"] is True
    assert final_state.nodes["verify.unit"].status == NodeStatus.ROLLED_BACK


@pytest.mark.asyncio
async def test_without_a_recovery_manager_failed_nodes_stay_failed(tmp_path: Path) -> None:
    """Backward compatible with every earlier phase's tests: no
    recovery configured means the P2 behaviour (stay FAILED) holds."""
    async def always_fails(node, view) -> NodeExecutionResult:
        raise RuntimeError("boom")

    graph = _graph()
    context = ContextStore(graph)
    events = EventLog(path=tmp_path / "events.jsonl", run_id="run-1")
    store = RunStore(tmp_path / "state.db")
    engine = Engine(
        run_id="run-1", graph=graph, context=context, event_log=events, store=store,
        node_executors={"intake": _noop, "flaky": always_fails, "downstream": _noop},
        recovery=None,
    )
    engine.start(scenario="s", workflow="w")
    final_state = await engine.run()

    assert final_state.nodes["flaky"].status == NodeStatus.FAILED
    assert final_state.status == RunStatus.HALTED
