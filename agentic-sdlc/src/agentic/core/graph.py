"""WorkflowGraph: build, validate, traverse (Section 4.1 / 5.1).

The graph is purely structural — NodeSpec objects and depends_on edges.
It knows nothing about runtime status; the scheduler (P2) layers its
ready-set computation on top of upstream_of/downstream_of, and gates
consult entry/exit specs stored on each NodeSpec.
"""

from __future__ import annotations

from agentic.core.models import NodeSpec


class GraphValidationError(Exception):
    """Raised by WorkflowGraph.validate() for cycles, dangling
    dependencies, a missing root, or nodes unreachable from the root."""


class WorkflowGraph:
    def __init__(self, root: str) -> None:
        self.root = root
        self._nodes: dict[str, NodeSpec] = {}

    def add_node(self, spec: NodeSpec) -> None:
        if spec.node_id in self._nodes:
            raise GraphValidationError(f"duplicate node_id: {spec.node_id}")
        self._nodes[spec.node_id] = spec

    def __contains__(self, node_id: str) -> bool:
        return node_id in self._nodes

    def __getitem__(self, node_id: str) -> NodeSpec:
        return self._nodes[node_id]

    def __iter__(self):
        return iter(self._nodes.values())

    def __len__(self) -> int:
        return len(self._nodes)

    @property
    def node_ids(self) -> list[str]:
        return list(self._nodes.keys())

    def remove_node(self, node_id: str) -> None:
        """Used by replan.py's reshape step to drop dropped tasks."""
        self._nodes.pop(node_id, None)
        for node in self._nodes.values():
            if node_id in node.depends_on:
                node.depends_on.remove(node_id)

    def _dependents(self, node_id: str) -> list[str]:
        """Nodes that directly depend on node_id."""
        return [n.node_id for n in self._nodes.values() if node_id in n.depends_on]

    def _reachable_from(self, node_id: str, seen: set[str] | None = None) -> set[str]:
        seen = seen if seen is not None else set()
        seen.add(node_id)
        for dependent in self._dependents(node_id):
            if dependent not in seen:
                self._reachable_from(dependent, seen)
        return seen

    def validate(self) -> None:
        if self.root not in self._nodes:
            raise GraphValidationError(f"root node '{self.root}' not present in graph")

        for node in self._nodes.values():
            for dep in node.depends_on:
                if dep not in self._nodes:
                    raise GraphValidationError(
                        f"node '{node.node_id}' depends on undefined node '{dep}'"
                    )

        self._check_cycles()

        reachable = self._reachable_from(self.root)
        unreachable = set(self._nodes) - reachable
        if unreachable:
            raise GraphValidationError(
                f"orphan nodes unreachable from root '{self.root}': {sorted(unreachable)}"
            )

    def _check_cycles(self) -> None:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = dict.fromkeys(self._nodes, WHITE)

        def visit(node_id: str, path: list[str]) -> None:
            color[node_id] = GRAY
            for dependent in self._dependents(node_id):
                if color[dependent] == GRAY:
                    cycle = " -> ".join([*path, dependent])
                    raise GraphValidationError(f"cycle detected: {cycle}")
                if color[dependent] == WHITE:
                    visit(dependent, [*path, dependent])
            color[node_id] = BLACK

        for node_id in self._nodes:
            if color[node_id] == WHITE:
                visit(node_id, [node_id])

    def upstream_of(self, node_id: str) -> set[str]:
        """All transitive ancestors of node_id (not including node_id)."""
        seen: set[str] = set()
        stack = list(self._nodes[node_id].depends_on)
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(self._nodes[n].depends_on)
        return seen

    def downstream_of(self, node_id: str) -> set[str]:
        """All transitive descendants of node_id (not including node_id)."""
        return self._reachable_from(node_id) - {node_id}

    def topological_layers(self) -> list[list[str]]:
        """Kahn's algorithm, grouped into layers. All nodes in a layer have
        every dependency satisfied by an earlier layer, so a layer is
        exactly the set of nodes that *could* run concurrently if nothing
        else gated them — this is the structural precursor to the
        scheduler's runtime ready-set."""
        remaining = {nid: len(n.depends_on) for nid, n in self._nodes.items()}
        dependents = {nid: self._dependents(nid) for nid in self._nodes}

        layers: list[list[str]] = []
        processed: set[str] = set()
        current = sorted(nid for nid, degree in remaining.items() if degree == 0)

        while current:
            layers.append(current)
            processed.update(current)
            next_layer: set[str] = set()
            for nid in current:
                for dep in dependents[nid]:
                    remaining[dep] -= 1
                    if remaining[dep] == 0:
                        next_layer.add(dep)
            current = sorted(next_layer - processed)

        if len(processed) != len(self._nodes):
            raise GraphValidationError(
                "graph has a cycle or disconnected component; "
                "topological_layers requires validate() to have passed"
            )
        return layers
