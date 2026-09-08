"""Scheduler: ready-set computation and deadlock detection (Section 5.3).

Join semantics (all/any/quorum) live inside the upstream_satisfied gate
condition (gates.py) rather than here, since "is this node's join
satisfied" and "is this node's entry gate satisfied" are the same
question once join_policy is taken into account. The scheduler's job is
purely to decide which PENDING/BLOCKED/INVALIDATED/AWAITING_APPROVAL
nodes currently have a passing entry gate.

Bounded concurrency (the semaphore) lives on the Engine, since it bounds
concurrent *execution* (node_executors / future LLM calls), not
ready-set computation, which is synchronous and cheap.
"""

from __future__ import annotations

from agentic.core.context import ContextStore
from agentic.core.gates import GateContext, GateEvaluator, aggregate
from agentic.core.graph import WorkflowGraph
from agentic.core.models import GateVerdict, RunState
from agentic.core.states import NodeStatus

_CANDIDATE_STATUSES = (
    NodeStatus.PENDING,
    NodeStatus.BLOCKED,
    NodeStatus.INVALIDATED,
    NodeStatus.AWAITING_APPROVAL,
)

_TERMINAL_STATUSES = (NodeStatus.SUCCEEDED, NodeStatus.SKIPPED, NodeStatus.HALTED)

_IN_FLIGHT_STATUSES = (
    NodeStatus.RUNNING,
    NodeStatus.RETRYING,
    NodeStatus.FALLBACK,
    NodeStatus.ROLLING_BACK,
)


class Scheduler:
    def __init__(self, graph: WorkflowGraph, context: ContextStore, gates: GateEvaluator) -> None:
        self.graph = graph
        self.context = context
        self.gates = gates

    def ready_set(self, state: RunState) -> list[str]:
        """Nodes whose current status could plausibly run and whose entry
        gate (join policy included) currently passes."""
        ready: list[str] = []
        for node_id in self.graph.node_ids:
            node_run = state.nodes.get(node_id)
            status = node_run.status if node_run else NodeStatus.PENDING
            if status not in _CANDIDATE_STATUSES:
                continue
            ctx = GateContext(
                node=self.graph[node_id],
                graph=self.graph,
                context=self.context,
                run_state=state,
                node_run=node_run,
            )
            if aggregate(self.gates.evaluate_entry(ctx)) == GateVerdict.FAIL:
                continue
            ready.append(node_id)
        return ready

    def is_deadlocked(self, state: RunState) -> bool:
        """True when nothing is in flight, nothing is ready, and at least
        one node has not reached a terminal status — no external event
        (approval, retry, replan) is currently pending that could ever
        unblock it."""
        statuses = [nr.status for nr in state.nodes.values()]
        if any(s in _IN_FLIGHT_STATUSES for s in statuses):
            return False
        if any(s == NodeStatus.AWAITING_APPROVAL for s in statuses):
            return False
        if self.ready_set(state):
            return False
        return any(s not in _TERMINAL_STATUSES for s in statuses)

    def all_settled(self, state: RunState) -> bool:
        terminal = (NodeStatus.SUCCEEDED, NodeStatus.SKIPPED)
        return all(nr.status in terminal for nr in state.nodes.values())
