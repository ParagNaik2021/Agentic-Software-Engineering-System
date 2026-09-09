"""The orchestration run loop (Section 5.3), node execution and exit-gate
settlement, and pause/resume across human approval checkpoints
(Section 6.2).

Node execution is injected via `node_executors`: a mapping from node_id
to an async callable. Real agents (P6) populate this from the agent
registry; until then, and in every test, small deterministic stub
executors stand in for them. `executors_by_agent` is a fallback keyed
by NodeSpec.agent instead of node_id, for dynamically-expanded nodes
(e.g. every impl.<task_id> node shares agent="implementer") whose exact
node_id cannot be registered before the graph is expanded mid-run.

Every status change goes through _transition(), which emits the
NODE_STATE_CHANGED event *before* mutating in-memory state, and derives
started_at/ended_at/duration_ms from the event's own timestamp — the
same derivation events.replay_to_state() performs — so a live run and a
replayed run agree exactly.

When a node ends FAILED and a RecoveryManager is configured, _recover()
classifies the failure and re-executes it in place (retry/fallback) or
reverts it (rollback) via `rollback_handlers[node_id]` — a workflow-
supplied callable, since only the workflow knows what "revert" means
for a given node (e.g. git-reset to a prior checkpoint). A node left
ROLLED_BACK is not retried automatically; its downstream nodes stay
BLOCKED, which the next tick's deadlock check turns into a safe-stop —
the "escalate to a human" Section 6.4 describes for this path.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

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
    compute_content_hash,
)
from agentic.core.replan import (
    ReplanBudgetExceeded,
    ReplanController,
    compute_invalidation_set,
)
from agentic.core.states import NodeStatus, assert_transition
from agentic.core.store import RunStore
from agentic.governance.approvals import ApprovalNotPermitted
from agentic.governance.recovery import RecoveryManager
from agentic.llm.provider import MalformedOutputError, QualityFaultError, TransientProviderError
from agentic.llm.replay import ReplayMiss

SYSTEM = Actor(kind="system", id="engine")
APPROVER = Actor(kind="human", id="approver")


def _classify_exception(exc: BaseException) -> ErrorClass:
    """Section 6.4's error classification for exceptions raised during
    execution (as opposed to exit-gate FAIL, classified in _settle as
    QUALITY_FAILURE). The three named exception types are the fault
    profiles a provider can raise (llm/provider.py, Section 9.3);
    anything else is SYSTEMIC — safe-stop rather than a blind retry,
    since an unclassified exception's retry-safety is unknown."""
    if isinstance(exc, TransientProviderError):
        return ErrorClass.TRANSIENT
    if isinstance(exc, MalformedOutputError):
        return ErrorClass.MALFORMED_OUTPUT
    if isinstance(exc, QualityFaultError):
        return ErrorClass.QUALITY_FAILURE
    if isinstance(exc, ReplayMiss):
        # A cassette miss is deterministic: the same prompt will miss
        # again, so retrying is pointless and fabricating a response is
        # not an option. It is SYSTEMIC (safe-stop), but classified
        # explicitly so the safe-stop reason says 'replay cassette miss'
        # rather than 'unclassified error'.
        return ErrorClass.SYSTEMIC
    return ErrorClass.SYSTEMIC

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

GraphExpander = Callable[[WorkflowGraph, ContextView], list[str]]
"""Section 5.1: impl.<task_id> nodes are "dynamically expanded, one node
per task in task_graph". An expander is keyed by the node_id whose
success triggers it (e.g. "plan.decompose"); it mutates the graph in
place (adding new NodeSpecs, and may patch an existing node's
depends_on — e.g. impl.join's — to point at what it just added) and
returns the newly added node_ids so the engine can seed their NodeRun
entries."""


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
        executors_by_agent: dict[str, NodeExecutor] | None = None,
        graph_expanders: dict[str, GraphExpander] | None = None,
        gates: GateEvaluator | None = None,
        recovery: RecoveryManager | None = None,
        rollback_handlers: dict[str, Callable[[], object]] | None = None,
        concurrency: int = 4,
        replan_budget: int = 3,
    ) -> None:
        self.run_id = run_id
        self.graph = graph
        self.context = context
        self.events = event_log
        self.store = store
        self.node_executors = node_executors or {}
        self.executors_by_agent = executors_by_agent or {}
        self.graph_expanders = graph_expanders or {}
        self.gates = gates or GateEvaluator()
        self.recovery = recovery
        self.rollback_handlers = rollback_handlers or {}
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
        RunState, rehydrate artifact content from context.persist_dir,
        reconstruct granted_approvals from APPROVAL_GRANTED events (it is
        in-memory only on a live Engine and would otherwise vanish across
        the process boundary this method exists to cross), and re-apply
        any graph_expanders whose trigger node already succeeded — a
        fresh graph object never saw the mutation the original process's
        expander made, so it must be redone against this graph. The
        resulting node_ids already exist in the replayed RunState, so no
        duplicate NodeRun entries are created; callers must pass a fresh
        WorkflowGraph instance to resume(), never one already expanded."""
        engine = cls(run_id, graph, context, event_log, store, **kwargs)
        context.load_from_disk()
        engine.state = store.resume(run_id, event_log)

        for event in event_log.read():
            if event.type == EventType.APPROVAL_GRANTED and event.node_id:
                input_hash = event.payload.get("input_hash")
                if isinstance(input_hash, str):
                    engine.granted_approvals[event.node_id] = input_hash
            elif event.type == EventType.APPROVAL_REJECTED and event.node_id:
                engine.granted_approvals.pop(event.node_id, None)

        for trigger_node_id, expander in engine.graph_expanders.items():
            node_run = engine.state.nodes.get(trigger_node_id)
            if node_run is None or node_run.status != NodeStatus.SUCCEEDED:
                continue
            view = context.view_for(trigger_node_id)
            for artifact_id in node_run.produced:
                artifact = context.get_by_id(artifact_id)
                if artifact is not None:
                    view.artifacts[artifact.name] = artifact
            expander(graph, view)
        return engine

    def trigger_replan(self, changed_node_id: str) -> None:
        self.replan.request(changed_node_id)

    def replan_now(self, changed_node_id: str) -> None:
        """Queues and immediately applies a re-plan, then persists —
        `agentic replan` must do this within one process invocation,
        since ReplanController's queue is in-memory only and would not
        survive to a later `agentic resume` in a fresh process."""
        self.trigger_replan(changed_node_id)
        self._apply_replan()
        self.store.save(self._state)

    def halt(self, reason: str = "operator halt") -> RunState:
        """Public wrapper for `agentic halt <run_id>` (Section 6.4:
        "Safe-stop is reachable by ... an operator command")."""
        return self._safe_stop(reason)

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
                await self._recover(node_id)

            if self.replan.pending():
                try:
                    self._apply_replan()
                except ReplanBudgetExceeded:
                    return self._safe_stop("replan_budget_exhausted")

            self.store.save(self._state)

    # ------------------------------------------------------------------
    # readiness / approval
    # ------------------------------------------------------------------
    def _node_run(self, node_id: str) -> NodeRun:
        """Nodes added by a GraphExpander mid-run are seeded into
        state.nodes at expansion time, but this stays defensive so
        ready-set computation never KeyErrors on a graph/state gap."""
        node_run = self._state.nodes.get(node_id)
        if node_run is None:
            node_run = NodeRun(node_id=node_id, run_id=self.run_id, status=NodeStatus.PENDING)
            self._state.nodes[node_id] = node_run
        return node_run

    def _ready_set(self) -> list[str]:
        ready = []
        for node_id in self.graph.node_ids:
            node_run = self._node_run(node_id)
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
        return all(self._node_run(node_id).status in terminal for node_id in self.graph.node_ids)

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

    _TERMINAL_RUN_STATUSES = (RunStatus.HALTED, RunStatus.FAILED, RunStatus.SUCCEEDED)

    def _assert_decision_allowed(self, node_id: str, action: str) -> None:
        """Guard for grant_approval/reject_approval.

        A human decision is only meaningful while the checkpoint is live.
        Without this check the engine recorded APPROVAL_GRANTED for
        anything at any time — including four duplicate grants on a run
        that had already SAFE_STOP_ENGAGED, which is an audit-trail defect:
        the log implied a human released a gate on a halted run.
        """
        assert self._state is not None
        if self._state.status in self._TERMINAL_RUN_STATUSES:
            raise ApprovalNotPermitted(
                f"cannot {action} '{node_id}': run {self.run_id} is {self._state.status.value}. "
                f"Recover it first (agentic replan ... --from-rejection, or start a new run)."
            )
        node_run = self._state.nodes.get(node_id)
        if node_run is None:
            raise ApprovalNotPermitted(f"cannot {action}: no node '{node_id}' in this run")
        if node_run.status != NodeStatus.AWAITING_APPROVAL:
            raise ApprovalNotPermitted(
                f"cannot {action} '{node_id}': node is {node_run.status.value}, "
                f"not AWAITING_APPROVAL"
            )

    def grant_approval(self, node_id: str, note: str = "") -> None:
        self._assert_decision_allowed(node_id, "approve")
        current_hash = self.context.input_hash(node_id)
        if self.granted_approvals.get(node_id) == current_hash:
            # idempotence guard: a second click (or a second CLI call)
            # must not append another APPROVAL_GRANTED for a decision
            # that already stands against these exact inputs
            raise ApprovalNotPermitted(
                f"'{node_id}' is already approved for input_hash {current_hash[:12]}; "
                f"run `agentic resume {self.run_id}` to continue"
            )
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
        self._assert_decision_allowed(node_id, "reject")
        self.events.append(
            EventType.APPROVAL_REJECTED, APPROVER, node_id=node_id, payload={"note": note}
        )
        self._transition(node_id, NodeStatus.REJECTED)

    def _last_rejection_note(self, node_id: str) -> str:
        note = ""
        for event in self.events.read():
            if event.type == EventType.APPROVAL_REJECTED and event.node_id == node_id:
                note = str(event.payload.get("note", ""))
        return note

    async def replan_from_rejection(self, node_id: str) -> None:
        """`agentic replan <run_id> --node <node> --from-rejection`
        (Section 6.4's "escalate to a human" path, made actionable):
        deliberate, human-initiated recovery for a node the operator
        REJECTED, as opposed to trigger_replan()/replan_now()'s path,
        which fires when an *upstream artifact's content* changed. The
        two are kept structurally distinct in the audit trail (different
        REPLAN_TRIGGERED payload shape, `trigger` field) because they
        answer different questions later: "why did this get invalidated"
        -- because a human rejected it and asked for rework, versus
        because its own inputs drifted out from under it.

        Rejection never triggers this automatically -- reject_approval()
        only transitions to REJECTED, and the run loop correctly halts on
        the resulting deadlock (Section 6.4: halting on rejection is the
        safe default). This method exists to be called deliberately,
        never from the run loop itself.

        node.depends_on (not the whole upstream closure) is what gets
        reopened: for design.review that is exactly
        [design.arch, design.data, design.api]; for release.readiness
        it is [verify.gate, docs.generate] -- the same mechanism
        generalizes to any requires_approval node without special-casing
        which one it is.
        """
        node_run = self._state.nodes.get(node_id)
        if node_run is None or node_run.status != NodeStatus.REJECTED:
            status = node_run.status.value if node_run else "unknown"
            raise ValueError(f"'{node_id}' is not REJECTED (status={status}); nothing to recover from")

        note = self._last_rejection_note(node_id)
        node = self.graph[node_id]

        reopened = [
            dep_id for dep_id in node.depends_on
            if (dep_run := self._state.nodes.get(dep_id)) is not None and dep_run.status == NodeStatus.SUCCEEDED
        ]
        for dep_id in reopened:
            self._transition(dep_id, NodeStatus.INVALIDATED)
            self.events.append(
                EventType.NODE_INVALIDATED, SYSTEM, node_id=dep_id,
                payload={"reason": "rejection_feedback", "source_node": node_id},
            )
            self._transition(dep_id, NodeStatus.PENDING, extra_payload={"input_hash": None})

        # Visible to any node downstream of `intake` -- i.e. everything --
        # since view_for() only shows a node artifacts produced by its own
        # upstream, and the node being reopened is never upstream of
        # itself. intake is the one node guaranteed upstream of every
        # reopened node regardless of which approval gate this is.
        if note:
            feedback_payload = {"text": note}
            self.context.put(Artifact(
                artifact_id=str(uuid4()), name=f"{node_id}_rejection_feedback", kind="spec",
                content_hash=compute_content_hash(feedback_payload), payload=feedback_payload,
                produced_by_node="intake", produced_by_agent="human", run_id=self.run_id,
                created_at=datetime.now(UTC),
            ))

        await self._perform_rollback(node_id, "human rejection recovery")
        self._transition(node_id, NodeStatus.PENDING, extra_payload={"input_hash": None})

        self.events.append(
            EventType.REPLAN_TRIGGERED, SYSTEM,
            payload={
                "changed_node": node_id, "invalidated": sorted(reopened),
                "trigger": "rejection", "note": note,
            },
        )
        self._state.replan_count += 1
        self._state.metrics.replans += 1
        self.store.save(self._state)

    # ------------------------------------------------------------------
    # execution / settlement
    # ------------------------------------------------------------------
    async def _execute_node(self, node_id: str) -> NodeExecutionResult:
        input_hash = self.context.input_hash(node_id)
        self._transition(node_id, NodeStatus.RUNNING, extra_payload={"input_hash": input_hash})
        self._state.nodes[node_id].attempt += 1
        node = self.graph[node_id]
        executor = self.node_executors.get(node_id)
        if executor is None and node.agent:
            executor = self.executors_by_agent.get(node.agent)
        if executor is None:
            executor = _default_control_executor
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
            node_run.error = ErrorRecord(error_class=_classify_exception(outcome), message=str(outcome))
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

        own_test_results = None
        own_security_findings = None
        for artifact_id in node_run.produced:
            produced_artifact = self.context.get_by_id(artifact_id)
            if produced_artifact is None or not isinstance(produced_artifact.payload, dict):
                continue
            if produced_artifact.name == "test_results":
                own_test_results = produced_artifact.payload
            elif produced_artifact.name == "security_findings":
                own_security_findings = produced_artifact.payload.get("findings")

        exit_ctx = GateContext(
            node=node,
            graph=self.graph,
            context=self.context,
            run_state=self._state,
            node_run=node_run,
            test_results=own_test_results,
            security_findings=own_security_findings,
        )
        exit_results = self.gates.evaluate_exit(exit_ctx)
        node_run.gate_results.extend(exit_results)
        for result in exit_results:
            self.events.append(
                EventType.GATE_EVALUATED, SYSTEM, node_id=node_id, payload=result.model_dump()
            )

        if aggregate(exit_results) == GateVerdict.FAIL:
            failing = [r for r in exit_results if r.verdict == GateVerdict.FAIL]
            node_run.error = ErrorRecord(
                error_class=ErrorClass.QUALITY_FAILURE,
                message="; ".join(f"{r.condition}: {r.message}" for r in failing),
            )
            self._transition(node_id, NodeStatus.FAILED)
        else:
            self._transition(node_id, NodeStatus.SUCCEEDED)
            self._expand_graph_if_needed(node_id)
            self._request_replan_if_staled(node_id)

    def _expand_graph_if_needed(self, node_id: str) -> None:
        expander = self.graph_expanders.get(node_id)
        if expander is None:
            return
        view = self.context.view_for(node_id)
        # a successful node's own artifacts are upstream of nothing but
        # itself in view_for's sense; the expander needs to see what
        # this node just produced too, so extend the view with it.
        for artifact_id in self._state.nodes[node_id].produced:
            artifact = self.context.get_by_id(artifact_id)
            if artifact is not None:
                view.artifacts[artifact.name] = artifact
        new_node_ids = expander(self.graph, view)
        for new_id in new_node_ids:
            if new_id not in self._state.nodes:
                self._state.nodes[new_id] = NodeRun(node_id=new_id, run_id=self.run_id, status=NodeStatus.PENDING)

    # ------------------------------------------------------------------
    # recovery (Section 6.4)
    # ------------------------------------------------------------------
    async def _recover(self, node_id: str) -> None:
        """Loops retry/fallback re-executions in place; a rollback or
        safe-stop decision ends the loop (rollback leaves the node
        ROLLED_BACK — not retried again automatically)."""
        if self.recovery is None:
            return
        node = self.graph[node_id]
        while self._state.nodes[node_id].status == NodeStatus.FAILED:
            node_run = self._state.nodes[node_id]
            error_class = node_run.error.error_class if node_run.error else ErrorClass.SYSTEMIC
            detail = node_run.error.message if node_run.error else ""
            decision = self.recovery.decide(
                error_class, node_run.attempt, node.retry, node.fallback, detail=detail
            )

            if decision.strategy == "retry":
                self.events.append(
                    EventType.RETRY_SCHEDULED, SYSTEM, node_id=node_id,
                    payload={"reason": decision.reason, "delay_seconds": decision.delay_seconds},
                )
                self._state.metrics.retries += 1
                self._transition(node_id, NodeStatus.RETRYING)
                if decision.delay_seconds:
                    await asyncio.sleep(decision.delay_seconds)
                await self._reexecute_and_settle(node_id)
            elif decision.strategy == "fallback":
                self.events.append(
                    EventType.FALLBACK_ENGAGED, SYSTEM, node_id=node_id, payload={"reason": decision.reason}
                )
                self._transition(node_id, NodeStatus.FALLBACK)
                await self._reexecute_and_settle(node_id)
            elif decision.strategy == "rollback":
                await self._perform_rollback(node_id, decision.reason)
                return
            elif decision.strategy == "safe_stop":
                self._safe_stop(decision.reason)
                return
            else:
                return

    async def _reexecute_and_settle(self, node_id: str) -> None:
        try:
            outcome: NodeExecutionResult | BaseException = await self._execute_node(node_id)
        except BaseException as exc:  # noqa: BLE001 - captured as a settlement outcome, not re-raised
            outcome = exc
        await self._settle(node_id, outcome)  # _settle itself expands the graph on success

    async def _perform_rollback(self, node_id: str, reason: str) -> None:
        self._transition(node_id, NodeStatus.ROLLING_BACK)
        self.events.append(EventType.ROLLBACK_STARTED, SYSTEM, node_id=node_id, payload={"reason": reason})
        handler = self.rollback_handlers.get(node_id)
        if handler is not None:
            result = handler()
            if inspect.isawaitable(result):
                await result
        self._transition(node_id, NodeStatus.ROLLED_BACK)
        self._state.metrics.rollbacks += 1
        self.events.append(EventType.ROLLBACK_COMPLETED, SYSTEM, node_id=node_id, payload={"reason": reason})

    # ------------------------------------------------------------------
    # re-planning
    # ------------------------------------------------------------------
    def _request_replan_if_staled(self, node_id: str) -> None:
        """A node finishing can invalidate work that already ran on older
        inputs. The case this exists for is workflows/ambiguous.py's
        `any`-join: plan.decompose and everything under it run on the
        ambiguity agent's proposed defaults while req.clarify is still
        sitting at its human checkpoint, so when req.clarify finally
        succeeds the design downstream of it is stale by definition.

        Without this hook the ReplanController's queue was only ever
        filled by `agentic replan` from the CLI, so run()'s
        `if self.replan.pending()` step never fired on its own and the
        clarified answer silently never reached the design.

        Detection is the pure compute_invalidation_set; the status flips
        stay in _apply_replan, so every one of them is still paired with
        its NODE_INVALIDATED event and the run replays correctly.
        """
        assert self._state is not None
        if compute_invalidation_set(self._state, self.graph, self.context, node_id):
            self.trigger_replan(node_id)

    def _apply_replan(self) -> None:
        """Triggered by an *upstream artifact's content* changing (Section
        5.4) -- distinct from replan_from_rejection()'s human-initiated
        path, which fires on an operator's explicit --from-rejection
        recovery instead. Both emit REPLAN_TRIGGERED but with a different
        `trigger` value, so the audit trail never conflates a human
        rejection-and-rework request with a node's inputs drifting out
        from under it."""
        changed_node_id, invalidated = self.replan.apply(self._state, self.graph, self.context)
        self.events.append(
            EventType.REPLAN_TRIGGERED,
            SYSTEM,
            payload={
                "changed_node": changed_node_id, "invalidated": sorted(invalidated),
                "trigger": "input_hash_mismatch",
            },
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
