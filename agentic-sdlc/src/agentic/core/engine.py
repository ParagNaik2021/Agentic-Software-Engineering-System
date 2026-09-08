"""The orchestration run loop (Section 5.3), node execution and exit-gate
settlement, and pause/resume across human approval checkpoints
(Section 6.2).

Node execution is injected via `node_executors`: a mapping from node_id
to an async callable. Real agents (P6) populate this from the agent
registry; until then, and in every test, small deterministic stub
executors stand in for them.

Every status change goes through _transition(), which emits the
NODE_STATE_CHANGED event *before* mutating in-memory state, and derives
started_at/ended_at/duration_ms from the event's own timestamp — the
same derivation events.replay_to_state() performs — so a live run and a
replayed run agree exactly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from agentic.core.context import ContextStore, ContextView
from agentic.core.events import Actor, EventLog, EventType
from agentic.core.gates import GateContext, GateEvaluator, aggregate
from agentic.core.graph import WorkflowGraph
from agentic.core.models import (
    Artifact,
    Decision,
    ErrorClass,
    ErrorRecord,
    GateVerdict,
    NodeRun,
    NodeSpec,
    RunMetrics,
    RunState,
    RunStatus,
)
from agentic.core.replan import ReplanBudgetExceeded, ReplanController
from agentic.core.states import NodeStatus, assert_transition
from agentic.core.store import RunStore

SYSTEM = Actor(kind="system", id="engine")
APPROVER = Actor(kind="human", id="approver")

_CANDIDATE_STATUSES = (
    NodeStatus.PENDING,
    NodeStatus.BLOCKED,
    NodeStatus.INVALIDATED,
    NodeStatus.AWAITING_APPROVAL,
)
_ENDED_STATUSES = (
    NodeStatus.SUCCEEDED,
    NodeStatus.FAILED,
    NodeStatus.ROLLED_BACK,
    NodeStatus.HALTED,
)


@dataclass
class NodeExecutionResult:
    artifacts: list[Artifact] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)


NodeExecutor = Callable[[NodeSpec, ContextView], Awaitable[NodeExecutionResult]]


async def _default_control_executor(node: NodeSpec, view: ContextView) -> NodeExecutionResult:
    """Control nodes (agent is None: intake, joins, ...) do no work of
    their own — downstream nodes consume merged upstream artifacts
    through ContextStore.view_for regardless of what a join "produces"."""
    return NodeExecutionResult()


class Engine:
    def __init__(
        self,
        run_id: str,
        graph: WorkflowGraph,
        context: ContextStore,
        event_log: EventLog,
        store: RunStore,
        node_executors: dict[str, NodeExecutor] | None = None,
        gates: GateEvaluator | None = None,
        concurrency: int = 4,
        replan_budget: int = 3,
    ) -> None:
        self.run_id = run_id
        self.graph = graph
        self.context = context
        self.events = event_log
        self.store = store
        self.node_executors = node_executors or {}
        self.gates = gates or GateEvaluator()
        self.semaphore = asyncio.Semaphore(concurrency)
        self.replan = ReplanController(budget=replan_budget)
        self.granted_approvals: dict[str, str] = {}
        self.state: RunState | None = None

    @property
    def _state(self) -> RunState:
        """Non-None view of self.state for internal use, once a run has
        been started or resumed. Keeps every other method free of
        RunState | None narrowing noise."""
        if self.state is None:
            raise RuntimeError("engine has no active run state; call start() or resume() first")
        return self.state

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self, scenario: str, workflow: str) -> RunState:
        event = self.events.append(
            EventType.RUN_STARTED,
            SYSTEM,
            payload={
                "scenario": scenario,
                "workflow": workflow,
                "node_ids": self.graph.node_ids,
            },
        )
        nodes = {
            nid: NodeRun(node_id=nid, run_id=self.run_id, status=NodeStatus.PENDING)
            for nid in self.graph.node_ids
        }
        self.state = RunState(
            run_id=self.run_id,
            scenario=scenario,
            workflow=workflow,
            status=RunStatus.RUNNING,
            nodes=nodes,
            created_at=event.ts,
            replan_count=0,
            metrics=RunMetrics(),
        )
        self.store.save(self._state)
        return self._state

    @classmethod
    def resume(
        cls,
        run_id: str,
        graph: WorkflowGraph,
        context: ContextStore,
        event_log: EventLog,
        store: RunStore,
        **kwargs,
    ) -> Engine:
        """Rebuild an Engine in a fresh process: replay the event log into
        RunState and rehydrate artifact content from context.persist_dir."""
        engine = cls(run_id, graph, context, event_log, store, **kwargs)
        context.load_from_disk()
        engine.state = store.resume(run_id, event_log)
        return engine

    def trigger_replan(self, changed_node_id: str) -> None:
        self.replan.request(changed_node_id)

    # ------------------------------------------------------------------
    # main loop (Section 5.3)
    # ------------------------------------------------------------------
    async def run(self) -> RunState:
        _ = self._state  # raises early if start()/resume() was never called
        while True:
            if self._all_settled():
                return self._complete_run()

            ready = self._ready_set()
            if not ready:
                if self._has_pending_approval():
                    return self._persist_and_pause()
                return self._safe_stop("deadlock")

            for node_id in ready:
                status = self._state.nodes[node_id].status
                if status in (NodeStatus.PENDING, NodeStatus.BLOCKED, NodeStatus.INVALIDATED):
                    self._transition(node_id, NodeStatus.READY)

            approved, blocked = self._partition_by_approval(ready)
            for node_id in blocked:
                self._request_approval(node_id)

            if not approved:
                # everything currently runnable is waiting on a human
                return self._persist_and_pause()

            results = await asyncio.gather(
                *[self._execute_node(node_id) for node_id in approved],
                return_exceptions=True,
            )
            for node_id, outcome in zip(approved, results, strict=True):
                await self._settle(node_id, outcome)

            if self.replan.pending():
                try:
                    self._apply_replan()
                except ReplanBudgetExceeded:
                    return self._safe_stop("replan_budget_exhausted")

            self.store.save(self._state)

    # ------------------------------------------------------------------
    # readiness / approval
    # ------------------------------------------------------------------
    def _ready_set(self) -> list[str]:
        ready = []
        for node_id in self.graph.node_ids:
            node_run = self._state.nodes[node_id]
            if node_run.status not in _CANDIDATE_STATUSES:
                continue
            ctx = GateContext(
                node=self.graph[node_id],
                graph=self.graph,
                context=self.context,
                run_state=self._state,
                node_run=node_run,
            )
            if aggregate(self.gates.evaluate_entry(ctx)) == GateVerdict.FAIL:
                continue
            ready.append(node_id)
        return ready

    def _all_settled(self) -> bool:
        terminal = {NodeStatus.SUCCEEDED, NodeStatus.SKIPPED}
        return all(nr.status in terminal for nr in self._state.nodes.values())

    def _has_pending_approval(self) -> bool:
        return any(nr.status == NodeStatus.AWAITING_APPROVAL for nr in self._state.nodes.values())

    def _partition_by_approval(self, ready: list[str]) -> tuple[list[str], list[str]]:
        approved, blocked = [], []
        for node_id in ready:
            node = self.graph[node_id]
            if not node.requires_approval:
                approved.append(node_id)
                continue
            current_hash = self.context.input_hash(node_id)
            if self.granted_approvals.get(node_id) == current_hash:
                approved.append(node_id)
            else:
                blocked.append(node_id)
        return approved, blocked

    def _request_approval(self, node_id: str) -> None:
        node_run = self._state.nodes[node_id]
        if node_run.status == NodeStatus.AWAITING_APPROVAL:
            return  # already requested; waiting on a human
        self._transition(node_id, NodeStatus.AWAITING_APPROVAL)
        self.events.append(
            EventType.APPROVAL_REQUESTED,
            SYSTEM,
            node_id=node_id,
            payload={"input_hash": self.context.input_hash(node_id)},
        )
        self._state.status = RunStatus.AWAITING_APPROVAL

    def grant_approval(self, node_id: str, note: str = "") -> None:
        current_hash = self.context.input_hash(node_id)
        self.granted_approvals[node_id] = current_hash
        self.events.append(
            EventType.APPROVAL_GRANTED,
            APPROVER,
            node_id=node_id,
            payload={"input_hash": current_hash, "note": note},
        )
        if self._state.status == RunStatus.AWAITING_APPROVAL:
            self._state.status = RunStatus.RUNNING

    def reject_approval(self, node_id: str, note: str = "") -> None:
        self.events.append(
            EventType.APPROVAL_REJECTED, APPROVER, node_id=node_id, payload={"note": note}
        )
        self._transition(node_id, NodeStatus.REJECTED)

    # ------------------------------------------------------------------
    # execution / settlement
    # ------------------------------------------------------------------
    async def _execute_node(self, node_id: str) -> NodeExecutionResult:
        input_hash = self.context.input_hash(node_id)
        self._transition(node_id, NodeStatus.RUNNING, extra_payload={"input_hash": input_hash})
        node = self.graph[node_id]
        executor = self.node_executors.get(node_id, _default_control_executor)
        async with self.semaphore:
            if node.agent:
                self.events.append(
                    EventType.AGENT_INVOKED,
                    Actor(kind="agent", id=node.agent),
                    node_id=node_id,
                    payload={},
                )
            view = self.context.view_for(node_id)
            return await executor(node, view)

    async def _settle(self, node_id: str, outcome: NodeExecutionResult | BaseException) -> None:
        node = self.graph[node_id]
        node_run = self._state.nodes[node_id]

        if isinstance(outcome, BaseException):
            node_run.error = ErrorRecord(error_class=ErrorClass.SYSTEMIC, message=str(outcome))
            self._transition(node_id, NodeStatus.FAILED)
            return

        actor = Actor(kind="agent", id=node.agent) if node.agent else SYSTEM
        for artifact in outcome.artifacts:
            self.context.put(artifact)
            node_run.produced.append(artifact.artifact_id)
            self.events.append(
                EventType.ARTIFACT_PRODUCED,
                actor,
                node_id=node_id,
                payload={"artifact_id": artifact.artifact_id, "name": artifact.name},
            )
        for decision in outcome.decisions:
            self.context.record_decision(decision)
            node_run.decisions.append(decision.decision_id)
            self.events.append(
                EventType.DECISION_RECORDED,
                actor,
                node_id=node_id,
                payload={"decision_id": decision.decision_id},
            )

        exit_ctx = GateContext(
            node=node,
            graph=self.graph,
            context=self.context,
            run_state=self._state,
            node_run=node_run,
        )
        exit_results = self.gates.evaluate_exit(exit_ctx)
        node_run.gate_results.extend(exit_results)
        for result in exit_results:
            self.events.append(
                EventType.GATE_EVALUATED, SYSTEM, node_id=node_id, payload=result.model_dump()
            )

        if aggregate(exit_results) == GateVerdict.FAIL:
            self._transition(node_id, NodeStatus.FAILED)
        else:
            self._transition(node_id, NodeStatus.SUCCEEDED)

    # ------------------------------------------------------------------
    # re-planning
    # ------------------------------------------------------------------
    def _apply_replan(self) -> None:
        changed_node_id, invalidated = self.replan.apply(self._state, self.graph, self.context)
        self.events.append(
            EventType.REPLAN_TRIGGERED,
            SYSTEM,
            payload={"changed_node": changed_node_id, "invalidated": sorted(invalidated)},
        )
        for node_id in invalidated:
            self._transition(node_id, NodeStatus.INVALIDATED)
            self.events.append(EventType.NODE_INVALIDATED, SYSTEM, node_id=node_id, payload={})
            self._transition(node_id, NodeStatus.PENDING, extra_payload={"input_hash": None})
        self._state.replan_count += 1
        self._state.metrics.replans += 1

    # ------------------------------------------------------------------
    # transitions / termination
    # ------------------------------------------------------------------
    def _transition(
        self, node_id: str, to: NodeStatus, extra_payload: dict | None = None
    ) -> None:
        node_run = self._state.nodes[node_id]
        assert_transition(node_run.status, to)
        frm = node_run.status
        payload: dict[str, object] = {
            "from": frm.value, "to": to.value, "attempt": node_run.attempt,
        }
        if extra_payload:
            payload.update(extra_payload)
        event = self.events.append(
            EventType.NODE_STATE_CHANGED, SYSTEM, node_id=node_id, payload=payload
        )
        node_run.status = to
        if "input_hash" in payload:
            input_hash = payload["input_hash"]
            assert input_hash is None or isinstance(input_hash, str)
            node_run.input_hash = input_hash
        if to == NodeStatus.RUNNING and node_run.started_at is None:
            node_run.started_at = event.ts
        if to in _ENDED_STATUSES:
            node_run.ended_at = event.ts
            if node_run.started_at is not None:
                node_run.duration_ms = int(
                    (node_run.ended_at - node_run.started_at).total_seconds() * 1000
                )

    def _complete_run(self) -> RunState:
        if self._state.status not in (RunStatus.SUCCEEDED, RunStatus.HALTED):
            self._state.status = RunStatus.SUCCEEDED
            self.events.append(EventType.RUN_COMPLETED, SYSTEM, payload={})
            self.store.save(self._state)
        return self._state

    def _persist_and_pause(self) -> RunState:
        self.store.save(self._state)
        return self._state

    def _safe_stop(self, reason: str) -> RunState:
        self._state.status = RunStatus.HALTED
        self.events.append(EventType.SAFE_STOP_ENGAGED, SYSTEM, payload={"reason": reason})
        self.store.save(self._state)
        return self._state
