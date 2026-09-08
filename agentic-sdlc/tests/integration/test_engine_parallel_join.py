"""P2 acceptance: a graph with a 3-way parallel group and a join executes
in the correct order with observable concurrency."""

import asyncio
import time
from pathlib import Path

import pytest

from agentic.core.context import ContextStore
from agentic.core.engine import Engine, NodeExecutionResult
from agentic.core.events import EventLog
from agentic.core.graph import WorkflowGraph
from agentic.core.models import Artifact, NodeSpec, SDLCStage, compute_content_hash
from agentic.core.states import NodeStatus
from agentic.core.store import RunStore


def _build_graph() -> WorkflowGraph:
    """intake -> {design.arch, design.data, design.api} -> design.review (join, all)"""
    graph = WorkflowGraph(root="intake")
    graph.add_node(NodeSpec(node_id="intake", stage=SDLCStage.INTAKE))
    for name, stage in (
        ("design.arch", SDLCStage.ARCHITECTURE),
        ("design.data", SDLCStage.DATA_DESIGN),
        ("design.api", SDLCStage.API_DESIGN),
    ):
        graph.add_node(
            NodeSpec(node_id=name, stage=stage, agent=name, depends_on=["intake"], parallel_group="design")
        )
    graph.add_node(
        NodeSpec(
            node_id="design.review", stage=SDLCStage.DESIGN_REVIEW,
            depends_on=["design.arch", "design.data", "design.api"], join_policy="all",
        )
    )
    graph.validate()
    return graph


@pytest.fixture
def engine(tmp_path: Path) -> tuple[Engine, dict]:
    graph = _build_graph()
    context = ContextStore(graph)
    events = EventLog(path=tmp_path / "events.jsonl", run_id="run-1")
    store = RunStore(tmp_path / "state.db")

    timeline: dict[str, tuple[float, float]] = {}

    def make_parallel_executor(node_id: str, delay: float):
        async def executor(node, view) -> NodeExecutionResult:
            start = time.monotonic()
            await asyncio.sleep(delay)
            timeline[node_id] = (start, time.monotonic())
            payload = {"produced_by": node_id}
            artifact = Artifact(
                artifact_id=f"{node_id}-artifact", name=f"artifact_{node_id}", kind="design",
                content_hash=compute_content_hash(payload), payload=payload,
                produced_by_node=node_id, produced_by_agent=node.agent or "system",
                run_id="run-1", created_at=events.clock.now(),
            )
            return NodeExecutionResult(artifacts=[artifact])
        return executor

    node_executors = {
        "design.arch": make_parallel_executor("design.arch", 0.2),
        "design.data": make_parallel_executor("design.data", 0.2),
        "design.api": make_parallel_executor("design.api", 0.2),
    }

    eng = Engine(
        run_id="run-1", graph=graph, context=context, event_log=events, store=store,
        node_executors=node_executors, concurrency=4,
    )
    eng.start(scenario="greenfield", workflow="greenfield")
    return eng, timeline


@pytest.mark.asyncio
async def test_three_way_parallel_group_runs_concurrently(engine) -> None:
    eng, timeline = engine
    final_state = await eng.run()

    assert final_state.status.value == "SUCCEEDED"
    for node_id in ("intake", "design.arch", "design.data", "design.api", "design.review"):
        assert final_state.nodes[node_id].status == NodeStatus.SUCCEEDED

    # observable concurrency: all three parallel nodes' execution windows overlap.
    # A serial run of three 0.2s sleeps would take >=0.6s wall clock; a
    # concurrent run takes ~0.2s. That gap is what's actually being tested,
    # since exact start/end timestamps are subject to scheduler jitter.
    starts = [timeline[n][0] for n in ("design.arch", "design.data", "design.api")]
    ends = [timeline[n][1] for n in ("design.arch", "design.data", "design.api")]
    wall_clock = max(ends) - min(starts)
    assert wall_clock < 0.4, (
        f"parallel nodes took {wall_clock:.3f}s wall clock; "
        "expected ~0.2s if truly concurrent, not ~0.6s if serial"
    )


@pytest.mark.asyncio
async def test_join_only_runs_after_all_three_branches_succeed(engine) -> None:
    eng, timeline = engine
    final_state = await eng.run()

    join_start = final_state.nodes["design.review"].started_at
    for node_id in ("design.arch", "design.data", "design.api"):
        branch_end = final_state.nodes[node_id].ended_at
        assert branch_end is not None and join_start is not None
        assert branch_end <= join_start


@pytest.mark.asyncio
async def test_design_review_context_view_includes_all_three_branch_artifacts(engine) -> None:
    eng, _ = engine
    await eng.run()

    view = eng.context.view_for("design.review")
    assert "artifact_design.arch" in view
    assert "artifact_design.data" in view
    assert "artifact_design.api" in view
