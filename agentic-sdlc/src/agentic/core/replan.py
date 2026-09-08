"""Dynamic re-planning (Section 5.5).

detect_stale / compute_invalidation_set are pure functions: they read
RunState and ContextStore but never mutate them. The Engine is the only
thing that actually flips a node's status, because every status change
must be paired with a NODE_STATE_CHANGED event for the run to replay
correctly — see Engine._transition in engine.py, which is what
run()'s replan step calls once ReplanController.apply() tells it which
node_ids are affected.

The "reshape" step (ask the planner agent whether the task graph itself
must change) has no agent to call until P6; reshape() is a documented
no-op hook that later phases override.
"""

from __future__ import annotations

from agentic.core.context import ContextStore
from agentic.core.graph import WorkflowGraph
from agentic.core.models import RunState
from agentic.core.states import NodeStatus


class ReplanBudgetExceeded(Exception):
    def __init__(self, replan_count: int) -> None:
        self.replan_count = replan_count
        super().__init__(f"replan budget exhausted after {replan_count} re-plan(s)")


def detect_stale(
    state: RunState, graph: WorkflowGraph, context: ContextStore, changed_node_id: str
) -> set[str]:
    """Step 1: among SUCCEEDED nodes downstream of changed_node_id, which
    ones now have a different input_hash than the one they actually ran
    with."""
    stale: set[str] = set()
    for node_id in graph.downstream_of(changed_node_id):
        node_run = state.nodes.get(node_id)
        if node_run is None or node_run.status != NodeStatus.SUCCEEDED:
            continue
        if node_run.input_hash is None:
            continue
        if context.input_hash(node_id) != node_run.input_hash:
            stale.add(node_id)
    return stale


def compute_invalidation_set(
    state: RunState, graph: WorkflowGraph, context: ContextStore, changed_node_id: str
) -> set[str]:
    """Step 2 (pure): transitively close the stale set — invalidating a
    node also invalidates everything downstream of it, since
    regenerating it will itself change output. Only SUCCEEDED nodes are
    eligible: INVALIDATED is a legal successor of SUCCEEDED alone, so a
    node that never ran (still PENDING) needs no invalidation — it will
    simply pick up the fresh input the first time it runs."""
    stale = detect_stale(state, graph, context, changed_node_id)
    to_invalidate: set[str] = set()
    stack = list(stale)
    while stack:
        node_id = stack.pop()
        if node_id in to_invalidate:
            continue
        node_run = state.nodes.get(node_id)
        if node_run is None or node_run.status != NodeStatus.SUCCEEDED:
            continue
        to_invalidate.add(node_id)
        stack.extend(graph.downstream_of(node_id))
    return to_invalidate


def reshape(state: RunState, graph: WorkflowGraph, context: ContextStore) -> None:
    """Step 3: ask the planner whether the task graph itself must change
    (new tasks, dropped tasks, a newly required impact analysis). No
    planner agent exists until P6; workflows/*.py wires a real callback
    in once it does."""
    return None


class ReplanController:
    """Owns the replan queue and budget. Computation only — apply()
    returns which node_ids must be invalidated; it never mutates
    RunState itself (see module docstring)."""

    def __init__(self, budget: int = 3) -> None:
        self.budget = budget
        self._queue: list[str] = []

    def request(self, changed_node_id: str) -> None:
        self._queue.append(changed_node_id)

    def pending(self) -> bool:
        return bool(self._queue)

    def apply(
        self, state: RunState, graph: WorkflowGraph, context: ContextStore
    ) -> tuple[str, set[str]]:
        if state.replan_count >= self.budget:
            raise ReplanBudgetExceeded(state.replan_count)
        changed_node_id = self._queue.pop(0)
        invalidated = compute_invalidation_set(state, graph, context, changed_node_id)
        reshape(state, graph, context)
        return changed_node_id, invalidated
