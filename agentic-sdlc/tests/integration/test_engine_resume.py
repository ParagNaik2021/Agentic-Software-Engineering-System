"""P2 acceptance: a paused run resumes to identical state in a fresh
process."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentic.core.context import ContextStore
from agentic.core.engine import Engine, NodeExecutionResult
from agentic.core.events import EventLog
from agentic.core.graph import WorkflowGraph
from agentic.core.models import Artifact, NodeSpec, RunStatus, SDLCStage, compute_content_hash
from agentic.core.states import NodeStatus
from agentic.core.store import RunStore


def _build_graph() -> WorkflowGraph:
    """intake -> design (requires_approval) -> impl"""
    graph = WorkflowGraph(root="intake")
    graph.add_node(NodeSpec(node_id="intake", stage=SDLCStage.INTAKE))
    graph.add_node(
        NodeSpec(
            node_id="design", stage=SDLCStage.DESIGN_REVIEW, agent="architect",
            depends_on=["intake"], requires_approval=True,
        )
    )
    graph.add_node(
        NodeSpec(node_id="impl", stage=SDLCStage.IMPLEMENTATION, agent="implementer", depends_on=["design"])
    )
    graph.validate()
    return graph


async def _stub_executor(node, view) -> NodeExecutionResult:
    payload = {"produced_by": node.node_id}
    artifact = Artifact(
        artifact_id=f"{node.node_id}-artifact", name=f"artifact_{node.node_id}", kind="design",
        content_hash=compute_content_hash(payload), payload=payload,
        produced_by_node=node.node_id, produced_by_agent=node.agent or "system",
        run_id="run-1", created_at=datetime.now(UTC),
    )
    return NodeExecutionResult(artifacts=[artifact])


def _make_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return tmp_path / "events.jsonl", tmp_path / "state.db", tmp_path / "artifacts"


@pytest.mark.asyncio
async def test_paused_run_resumes_to_identical_state_in_a_fresh_process(tmp_path: Path) -> None:
    events_path, db_path, artifacts_dir = _make_paths(tmp_path)
    node_executors = {"intake": _stub_executor, "design": _stub_executor, "impl": _stub_executor}

    # --- "process 1": run until it pauses for the design approval ---
    graph1 = _build_graph()
    context1 = ContextStore(graph1, persist_dir=artifacts_dir)
    events1 = EventLog(path=events_path, run_id="run-1")
    store1 = RunStore(db_path)

    engine1 = Engine(
        run_id="run-1", graph=graph1, context=context1, event_log=events1, store=store1,
        node_executors=node_executors,
    )
    engine1.start(scenario="greenfield", workflow="greenfield")
    paused_state = await engine1.run()

    assert paused_state.status == RunStatus.AWAITING_APPROVAL
    assert paused_state.nodes["intake"].status == NodeStatus.SUCCEEDED
    assert paused_state.nodes["design"].status == NodeStatus.AWAITING_APPROVAL
    assert paused_state.nodes["impl"].status == NodeStatus.PENDING

    # --- "process 2": brand new graph/context/event log/store objects
    # pointed at the same files, simulating a fresh process ---
    graph2 = _build_graph()
    context2 = ContextStore(graph2, persist_dir=artifacts_dir)
    events2 = EventLog(path=events_path, run_id="run-1")
    store2 = RunStore(db_path)

    engine2 = Engine.resume(
        run_id="run-1", graph=graph2, context=context2, event_log=events2, store=store2,
        node_executors=node_executors,
    )

    assert engine2.state == paused_state  # identical projected state after resume
    # artifact content, not just its id, survived the process boundary
    assert context2.get("artifact_intake").payload == {"produced_by": "intake"}

    # --- grant approval and continue the same (resumed) run to completion ---
    engine2.grant_approval("design")
    final_state = await engine2.run()

    assert final_state.status == RunStatus.SUCCEEDED
    for node_id in ("intake", "design", "impl"):
        assert final_state.nodes[node_id].status == NodeStatus.SUCCEEDED

    # the event log is still a valid, unbroken hash chain end to end
    assert events2.verify_chain().ok is True


@pytest.mark.asyncio
async def test_resume_without_granting_approval_pauses_again_identically(tmp_path: Path) -> None:
    events_path, db_path, artifacts_dir = _make_paths(tmp_path)
    node_executors = {"intake": _stub_executor, "design": _stub_executor, "impl": _stub_executor}

    graph1 = _build_graph()
    context1 = ContextStore(graph1, persist_dir=artifacts_dir)
    engine1 = Engine(
        run_id="run-1", graph=graph1, context=context1,
        event_log=EventLog(path=events_path, run_id="run-1"), store=RunStore(db_path),
        node_executors=node_executors,
    )
    engine1.start(scenario="greenfield", workflow="greenfield")
    await engine1.run()

    graph2 = _build_graph()
    context2 = ContextStore(graph2, persist_dir=artifacts_dir)
    engine2 = Engine.resume(
        run_id="run-1", graph=graph2, context=context2,
        event_log=EventLog(path=events_path, run_id="run-1"), store=RunStore(db_path),
        node_executors=node_executors,
    )
    # no grant_approval() call this time
    state_after_second_run = await engine2.run()

    assert state_after_second_run.status == RunStatus.AWAITING_APPROVAL
    assert state_after_second_run.nodes["design"].status == NodeStatus.AWAITING_APPROVAL
