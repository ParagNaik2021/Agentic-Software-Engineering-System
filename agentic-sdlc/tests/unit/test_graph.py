"""P1 acceptance: cyclic graphs rejected; correct topological layering."""

import pytest

from agentic.core.graph import GraphValidationError, WorkflowGraph
from agentic.core.models import NodeSpec, SDLCStage


def make_node(node_id: str, depends_on: list[str] | None = None) -> NodeSpec:
    return NodeSpec(node_id=node_id, stage=SDLCStage.IMPLEMENTATION, depends_on=depends_on or [])


def test_cyclic_graph_is_rejected() -> None:
    graph = WorkflowGraph(root="a")
    graph.add_node(make_node("a"))
    graph.add_node(make_node("b", ["a"]))
    graph.add_node(make_node("c", ["b"]))
    # close the cycle: a now (also) depends on c
    graph.add_node(make_node("d", ["c"]))
    graph._nodes["a"].depends_on.append("d")

    with pytest.raises(GraphValidationError, match="cycle"):
        graph.validate()


def test_dangling_dependency_is_rejected() -> None:
    graph = WorkflowGraph(root="a")
    graph.add_node(make_node("a"))
    graph.add_node(make_node("b", ["nonexistent"]))

    with pytest.raises(GraphValidationError, match="undefined node"):
        graph.validate()


def test_orphan_node_unreachable_from_root_is_rejected() -> None:
    graph = WorkflowGraph(root="a")
    graph.add_node(make_node("a"))
    graph.add_node(make_node("orphan"))  # no depends_on, not the root

    with pytest.raises(GraphValidationError, match="orphan"):
        graph.validate()


def test_missing_root_is_rejected() -> None:
    graph = WorkflowGraph(root="missing")
    graph.add_node(make_node("a"))

    with pytest.raises(GraphValidationError, match="root"):
        graph.validate()


def test_valid_graph_passes_validation() -> None:
    graph = WorkflowGraph(root="a")
    graph.add_node(make_node("a"))
    graph.add_node(make_node("b", ["a"]))
    graph.add_node(make_node("c", ["a"]))
    graph.add_node(make_node("join", ["b", "c"]))

    graph.validate()  # must not raise


def _build_layered_graph(n_layers: int, width: int) -> tuple[WorkflowGraph, list[list[str]]]:
    """A deterministic n_layers x width grid: every node in layer i depends
    on every node in layer i-1. Used to exercise topological_layers on a
    graph with >= 20 nodes."""
    graph = WorkflowGraph(root="root")
    graph.add_node(make_node("root"))
    expected: list[list[str]] = [["root"]]

    prev_layer = ["root"]
    for layer_idx in range(n_layers):
        layer_ids = [f"n{layer_idx}_{i}" for i in range(width)]
        for nid in layer_ids:
            graph.add_node(make_node(nid, depends_on=list(prev_layer)))
        expected.append(sorted(layer_ids))
        prev_layer = layer_ids

    return graph, expected


def test_topological_layers_on_20_plus_node_graph() -> None:
    graph, expected = _build_layered_graph(n_layers=4, width=5)  # 1 + 4*5 = 21 nodes
    assert len(graph) == 21
    graph.validate()

    layers = graph.topological_layers()
    assert layers == expected

    # every node in a layer has all its dependencies in strictly earlier layers
    seen: set[str] = set()
    for layer in layers:
        for nid in layer:
            deps = set(graph[nid].depends_on)
            assert deps.issubset(seen), f"{nid} has a dependency not yet processed"
        seen.update(layer)
    assert seen == set(graph.node_ids)


def test_upstream_and_downstream_of() -> None:
    graph = WorkflowGraph(root="a")
    graph.add_node(make_node("a"))
    graph.add_node(make_node("b", ["a"]))
    graph.add_node(make_node("c", ["a"]))
    graph.add_node(make_node("join", ["b", "c"]))
    graph.validate()

    assert graph.upstream_of("join") == {"a", "b", "c"}
    assert graph.upstream_of("b") == {"a"}
    assert graph.downstream_of("a") == {"b", "c", "join"}
    assert graph.downstream_of("join") == set()
