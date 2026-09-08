"""Section 5.1: impl.<task_id> nodes are dynamically expanded from
task_graph. This exercises Engine.graph_expanders end to end: a
plan.decompose-like node produces a task list, and new impl.* nodes
(plus a patched impl.join) appear in the graph mid-run."""

from datetime import UTC, datetime
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
    """intake -> plan.decompose -> impl.join (placeholder, no tasks yet)"""
    graph = WorkflowGraph(root="intake")
    graph.add_node(NodeSpec(node_id="intake", stage=SDLCStage.INTAKE))
    graph.add_node(
        NodeSpec(node_id="plan.decompose", stage=SDLCStage.DECOMPOSITION, agent="planner", depends_on=["intake"])
    )
    # placeholder: no real deps yet, patched by the expander once tasks exist
    graph.add_node(NodeSpec(node_id="impl.join", stage=SDLCStage.IMPLEMENTATION, depends_on=["plan.decompose"]))
    graph.validate()
    return graph


def _expand_impl_tasks(graph: WorkflowGraph, view) -> list[str]:
    task_graph = view.get("task_graph")
    tasks = task_graph.payload.get("tasks", []) if task_graph else []
    new_ids = []
    for task in tasks:
        node_id = f"impl.{task['task_id']}"
        graph.add_node(
            NodeSpec(node_id=node_id, stage=SDLCStage.IMPLEMENTATION, agent="implementer", depends_on=["plan.decompose"])
        )
        new_ids.append(node_id)
    graph["impl.join"].depends_on = list(new_ids)
    return new_ids


async def _planner_executor(node, view) -> NodeExecutionResult:
    payload = {"tasks": [{"task_id": "T1"}, {"task_id": "T2"}, {"task_id": "T3"}]}
    artifact = Artifact(
        artifact_id="task_graph-1", name="task_graph", kind="spec",
        content_hash=compute_content_hash(payload), payload=payload,
        produced_by_node=node.node_id, produced_by_agent="planner", run_id="run-1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return NodeExecutionResult(artifacts=[artifact])


async def _noop_executor(node, view) -> NodeExecutionResult:
    return NodeExecutionResult()


@pytest.mark.asyncio
async def test_dynamic_expansion_adds_impl_nodes_and_patches_join(tmp_path: Path) -> None:
    graph = _build_graph()
    context = ContextStore(graph)
    events = EventLog(path=tmp_path / "events.jsonl", run_id="run-1")
    store = RunStore(tmp_path / "state.db")

    engine = Engine(
        run_id="run-1", graph=graph, context=context, event_log=events, store=store,
        node_executors={"intake": _noop_executor, "plan.decompose": _planner_executor},
        graph_expanders={"plan.decompose": _expand_impl_tasks},
    )
    engine.start(scenario="greenfield", workflow="greenfield")
    final_state = await engine.run()

    assert final_state.status.value == "SUCCEEDED"
    for task_id in ("T1", "T2", "T3"):
        node_id = f"impl.{task_id}"
        assert node_id in graph
        assert final_state.nodes[node_id].status == NodeStatus.SUCCEEDED

    assert set(graph["impl.join"].depends_on) == {"impl.T1", "impl.T2", "impl.T3"}
    assert final_state.nodes["impl.join"].status == NodeStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_dynamic_expansion_survives_resume_in_a_fresh_process(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    db_path = tmp_path / "state.db"
    executors = {"intake": _noop_executor, "plan.decompose": _planner_executor}
    expanders = {"plan.decompose": _expand_impl_tasks}

    graph1 = _build_graph()
    context1 = ContextStore(graph1, persist_dir=tmp_path / "artifacts")
    engine1 = Engine(
        run_id="run-1", graph=graph1, context=context1,
        event_log=EventLog(path=events_path, run_id="run-1"), store=RunStore(db_path),
        node_executors=executors, graph_expanders=expanders,
    )
    engine1.start(scenario="greenfield", workflow="greenfield")
    await engine1.run()

    # fresh process: rebuild the SAME base graph (expander re-applies once
    # plan.decompose's success is replayed) and resume
    graph2 = _build_graph()
    context2 = ContextStore(graph2, persist_dir=tmp_path / "artifacts")
    engine2 = Engine.resume(
        run_id="run-1", graph=graph2, context=context2,
        event_log=EventLog(path=events_path, run_id="run-1"), store=RunStore(db_path),
        node_executors=executors, graph_expanders=expanders,
    )

    for task_id in ("T1", "T2", "T3"):
        assert engine2.state.nodes[f"impl.{task_id}"].status == NodeStatus.SUCCEEDED
    assert engine2.state.status.value == "SUCCEEDED"
