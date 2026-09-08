"""Workflow registry: name -> graph builder (Section 4.1). `agentic run
<name>` and the test suite both resolve a workflow through this single
mapping."""

from __future__ import annotations

from collections.abc import Callable

from agentic.core.graph import WorkflowGraph
from agentic.workflows.ambiguous import build_graph as build_ambiguous_graph
from agentic.workflows.brownfield import build_graph as build_brownfield_graph
from agentic.workflows.greenfield import build_graph as build_greenfield_graph

GraphBuilder = Callable[[], WorkflowGraph]

WORKFLOWS: dict[str, GraphBuilder] = {
    "greenfield": build_greenfield_graph,
    "brownfield": build_brownfield_graph,
    "ambiguous": build_ambiguous_graph,
}


def get_builder(name: str) -> GraphBuilder:
    if name not in WORKFLOWS:
        raise KeyError(f"unknown workflow: {name} (known: {sorted(WORKFLOWS)})")
    return WORKFLOWS[name]
