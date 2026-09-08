"""P2 acceptance: changing an upstream artifact invalidates exactly the
transitive downstream set and no more."""

from datetime import UTC, datetime

from agentic.core.context import ContextStore
from agentic.core.graph import WorkflowGraph
from agentic.core.models import (
    Artifact,
    NodeRun,
    NodeSpec,
    RunState,
    RunStatus,
    SDLCStage,
    compute_content_hash,
)
from agentic.core.replan import ReplanBudgetExceeded, ReplanController, compute_invalidation_set
from agentic.core.states import NodeStatus

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _graph() -> WorkflowGraph:
    """intake -> a -> b -> c
                intake -> sibling (unrelated branch)"""
    graph = WorkflowGraph(root="intake")
    graph.add_node(NodeSpec(node_id="intake", stage=SDLCStage.INTAKE))
    graph.add_node(NodeSpec(node_id="a", stage=SDLCStage.REQUIREMENTS, depends_on=["intake"]))
    graph.add_node(NodeSpec(node_id="b", stage=SDLCStage.ARCHITECTURE, depends_on=["a"]))
    graph.add_node(NodeSpec(node_id="c", stage=SDLCStage.IMPLEMENTATION, depends_on=["b"]))
    graph.add_node(
        NodeSpec(node_id="sibling", stage=SDLCStage.DATA_DESIGN, depends_on=["intake"])
    )
    graph.validate()
    return graph


def _artifact(name: str, payload: dict, node: str, version: int = 1) -> Artifact:
    return Artifact(
        artifact_id=f"{name}-v{version}", name=name, kind="spec",
        content_hash=compute_content_hash(payload), payload=payload,
        produced_by_node=node, produced_by_agent="stub", run_id="run-1",
        created_at=NOW, version=version,
    )


def _succeed_all(graph: WorkflowGraph, context: ContextStore, state: RunState) -> None:
    """Simulate every node having already run successfully, each
    producing one artifact named after itself, with input_hash recorded
    at the moment it ran."""
    for node_id in graph.node_ids:
        # record input_hash as of "now" (before this node's own artifact exists)
        state.nodes[node_id].input_hash = context.input_hash(node_id)
        context.put(_artifact(f"artifact_{node_id}", {"v": 1}, node=node_id))
        state.nodes[node_id].status = NodeStatus.SUCCEEDED


def _run_state(graph: WorkflowGraph) -> RunState:
    nodes = {
        nid: NodeRun(node_id=nid, run_id="run-1", status=NodeStatus.PENDING)
        for nid in graph.node_ids
    }
    return RunState(
        run_id="run-1", scenario="s", workflow="w", status=RunStatus.RUNNING,
        nodes=nodes, created_at=NOW,
    )


def test_invalidation_is_exactly_the_transitive_downstream_set() -> None:
    graph = _graph()
    context = ContextStore(graph)
    state = _run_state(graph)
    _succeed_all(graph, context, state)

    # Simulate an external change to the artifact produced by "a": a new
    # version with different content changes a's input_hash footprint
    # for anything downstream that reads it.
    context.put(_artifact("artifact_a", {"v": 2}, node="a", version=2))

    invalidated = compute_invalidation_set(state, graph, context, changed_node_id="a")

    assert invalidated == {"b", "c"}
    # nodes not downstream of "a" must be untouched
    assert "sibling" not in invalidated
    assert "intake" not in invalidated
    assert "a" not in invalidated  # the changed node itself is not invalidated


def test_invalidation_is_empty_when_nothing_actually_changed() -> None:
    graph = _graph()
    context = ContextStore(graph)
    state = _run_state(graph)
    _succeed_all(graph, context, state)

    invalidated = compute_invalidation_set(state, graph, context, changed_node_id="a")

    assert invalidated == set()


def test_invalidation_ignores_nodes_that_never_ran() -> None:
    graph = _graph()
    context = ContextStore(graph)
    state = _run_state(graph)
    # Only intake and a have run; b and c are still PENDING.
    state.nodes["intake"].input_hash = context.input_hash("intake")
    context.put(_artifact("artifact_intake", {"v": 1}, node="intake"))
    state.nodes["intake"].status = NodeStatus.SUCCEEDED

    state.nodes["a"].input_hash = context.input_hash("a")
    context.put(_artifact("artifact_a", {"v": 1}, node="a"))
    state.nodes["a"].status = NodeStatus.SUCCEEDED

    context.put(_artifact("artifact_a", {"v": 2}, node="a", version=2))

    invalidated = compute_invalidation_set(state, graph, context, changed_node_id="a")

    assert invalidated == set()  # b and c never ran, nothing to invalidate


def test_replan_controller_apply_returns_changed_node_and_invalidated_set() -> None:
    graph = _graph()
    context = ContextStore(graph)
    state = _run_state(graph)
    _succeed_all(graph, context, state)
    context.put(_artifact("artifact_a", {"v": 2}, node="a", version=2))

    controller = ReplanController(budget=3)
    controller.request("a")
    assert controller.pending() is True

    changed_node_id, invalidated = controller.apply(state, graph, context)

    assert changed_node_id == "a"
    assert invalidated == {"b", "c"}
    assert controller.pending() is False


def test_replan_controller_enforces_budget() -> None:
    graph = _graph()
    context = ContextStore(graph)
    state = _run_state(graph)
    state.replan_count = 3

    controller = ReplanController(budget=3)
    controller.request("a")

    try:
        controller.apply(state, graph, context)
    except ReplanBudgetExceeded as exc:
        assert exc.replan_count == 3
    else:
        raise AssertionError("expected ReplanBudgetExceeded")
