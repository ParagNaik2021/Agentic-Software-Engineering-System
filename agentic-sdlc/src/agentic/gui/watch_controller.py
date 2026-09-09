"""Tk-free core of `agentic watch`: state polling and approve/reject
dispatch. Kept separate from watch.py (the Tkinter view) so this can be
unit-tested without a display, and so the view stays presentation-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentic.core.engine import Engine
from agentic.core.events import EventType
from agentic.core.models import RunState, SDLCStage
from agentic.core.states import NodeStatus
from agentic.governance.approvals import ApprovalManager, DecisionPackage
from agentic.runtime import approve_and_save, reject_and_save, submit_clarification_and_save

_TERMINAL_RUN_STATUSES = {"SUCCEEDED", "FAILED", "HALTED"}


@dataclass
class WatchSnapshot:
    """What the view needs to redraw itself after any state change.

    `awaiting` stays the full set of paused nodes; the two narrower lists
    split it by what the human is actually being asked for, because a
    CLARIFICATION node and an approval gate reuse the same
    AWAITING_APPROVAL status but need different controls — an answer box
    versus Approve/Reject. Splitting it here rather than in the view
    keeps that distinction testable without a display.
    """

    state: RunState
    awaiting: list[str]
    is_terminal: bool
    awaiting_clarification: list[str] = field(default_factory=list)
    awaiting_approval: list[str] = field(default_factory=list)


class WatchController:
    """Owns the one live Engine a `agentic watch` session drives. Every
    approve/reject goes through runtime.py's approve_and_save/
    reject_and_save — the same functions `agentic approve`/`agentic
    reject` call — so approving from the GUI is not a second code path."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._approval_manager = ApprovalManager()

    def poll(self) -> WatchSnapshot:
        """Re-reads the persisted RunState from disk (not just this
        process's in-memory copy) so a run being driven concurrently by
        another `agentic resume`/`agentic approve` elsewhere is reflected
        here too."""
        stored = self.engine.store.load(self.engine.run_id)
        state = stored if stored is not None else self.engine.state
        assert state is not None
        awaiting = sorted(nid for nid, nr in state.nodes.items() if nr.status == NodeStatus.AWAITING_APPROVAL)
        return WatchSnapshot(
            state=state,
            awaiting=awaiting,
            is_terminal=state.status.value in _TERMINAL_RUN_STATUSES,
            awaiting_clarification=[nid for nid in awaiting if self.is_clarification(nid)],
            awaiting_approval=[nid for nid in awaiting if not self.is_clarification(nid)],
        )

    def is_clarification(self, node_id: str) -> bool:
        """A clarification checkpoint is identified by its graph stage,
        not by its status — req.clarify and design.review are both
        AWAITING_APPROVAL when paused, but only one of them is asking a
        question."""
        return self.engine.graph[node_id].stage == SDLCStage.CLARIFICATION

    def clarification_questions(self, node_id: str) -> list[dict]:
        """The questions the ambiguity agent raised, read straight off the
        `clarification_questions` artifact its upstream node produced.
        Empty list if the artifact is absent, so the view can still offer
        a free-text box rather than refusing to render."""
        try:
            artifact = self.engine.context.get("clarification_questions")
        except (KeyError, LookupError):
            return []
        payload = artifact.payload
        if not isinstance(payload, dict):
            return []
        questions = payload.get("questions", [])
        return [q for q in questions if isinstance(q, dict)]

    def state_transitions(self) -> list[dict]:
        """Every NODE_STATE_CHANGED in the log, in recorded order.

        The view uses this to reveal a run's progression one step at a
        time instead of jumping a node straight to its current status.
        It reads the transitions that were actually recorded rather than
        interpolating plausible ones, so a staggered display still shows
        the real sequence — including the SUCCEEDED -> INVALIDATED ->
        PENDING flips of a re-plan, which in replay mode complete far
        faster than a human can follow.

        Read-only: nothing here influences execution, the event log, or
        any recorded timing.
        """
        out: list[dict] = []
        for event in self.engine.events.read():
            if event.type != EventType.NODE_STATE_CHANGED or not event.node_id:
                continue
            out.append({
                "seq": event.seq,
                "node_id": event.node_id,
                "to": str(event.payload.get("to", "")),
                "attempt": int(event.payload.get("attempt", 0) or 0),
            })
        return out

    def last_replan(self) -> dict | None:
        """The most recent REPLAN_TRIGGERED payload, read off the event
        log. The view needs this because in replay mode a re-plan
        completes well inside one poll interval, so the INVALIDATED ->
        PENDING -> RUNNING flicker can finish between two redraws; the
        event is the durable record that it happened, and the banner
        built from it does not depend on catching the run mid-flight."""
        replans = [e for e in self.engine.events.read() if e.type == EventType.REPLAN_TRIGGERED]
        if not replans:
            return None
        return dict(replans[-1].payload)

    def submit_clarification(self, node_id: str, answer: str) -> None:
        """Hand the typed answer to runtime.submit_clarification_and_save
        — the same clarify_executor the --clarification-answer flag
        installs, so the GUI is not a second injection path."""
        submit_clarification_and_save(self.engine, node_id, answer)

    def decision_package(self, node_id: str) -> DecisionPackage:
        assert self.engine.state is not None
        node_run = self.engine.state.nodes[node_id]
        return self._approval_manager.render_package(
            self.engine.graph[node_id], node_run, self.engine.context,
            policy_verdicts=node_run.policy_verdicts,
        )

    async def advance(self) -> RunState:
        """Runs the scheduler until it next pauses: a new approval
        checkpoint, or the run reaching a terminal status. Identical to
        what `agentic resume` does with the same Engine."""
        return await self.engine.run()

    def approve(self, node_id: str, note: str = "") -> None:
        approve_and_save(self.engine, node_id, note=note)

    def reject(self, node_id: str, note: str = "") -> None:
        reject_and_save(self.engine, node_id, note=note)
