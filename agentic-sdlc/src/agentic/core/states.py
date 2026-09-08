"""Node state machine (Section 4.3).

TRANSITIONS is the single source of truth for legal node status changes.
Every state mutation in the engine, recovery manager and replan controller
must go through assert_transition() so an illegal transition raises loudly
instead of silently corrupting a run.
"""

from __future__ import annotations

from enum import StrEnum


class NodeStatus(StrEnum):
    PENDING = "PENDING"
    BLOCKED = "BLOCKED"
    READY = "READY"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    FALLBACK = "FALLBACK"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    REJECTED = "REJECTED"
    SKIPPED = "SKIPPED"
    INVALIDATED = "INVALIDATED"
    HALTED = "HALTED"


TRANSITIONS: dict[NodeStatus, frozenset[NodeStatus]] = {
    NodeStatus.PENDING: frozenset({NodeStatus.BLOCKED, NodeStatus.READY, NodeStatus.SKIPPED}),
    NodeStatus.BLOCKED: frozenset(
        {NodeStatus.READY, NodeStatus.SKIPPED, NodeStatus.HALTED}
    ),
    NodeStatus.READY: frozenset(
        {NodeStatus.RUNNING, NodeStatus.AWAITING_APPROVAL, NodeStatus.HALTED}
    ),
    NodeStatus.AWAITING_APPROVAL: frozenset(
        {NodeStatus.RUNNING, NodeStatus.REJECTED, NodeStatus.HALTED}
    ),
    NodeStatus.RUNNING: frozenset(
        {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.HALTED}
    ),
    NodeStatus.SUCCEEDED: frozenset({NodeStatus.INVALIDATED}),
    NodeStatus.FAILED: frozenset(
        {
            NodeStatus.RETRYING,
            NodeStatus.FALLBACK,
            NodeStatus.ROLLING_BACK,
            NodeStatus.HALTED,
        }
    ),
    NodeStatus.RETRYING: frozenset({NodeStatus.RUNNING, NodeStatus.FAILED}),
    NodeStatus.FALLBACK: frozenset({NodeStatus.RUNNING, NodeStatus.FAILED}),
    NodeStatus.ROLLING_BACK: frozenset({NodeStatus.ROLLED_BACK, NodeStatus.HALTED}),
    NodeStatus.ROLLED_BACK: frozenset({NodeStatus.PENDING}),
    NodeStatus.REJECTED: frozenset({NodeStatus.ROLLING_BACK, NodeStatus.HALTED}),
    NodeStatus.SKIPPED: frozenset({NodeStatus.PENDING}),
    NodeStatus.INVALIDATED: frozenset({NodeStatus.PENDING}),
    NodeStatus.HALTED: frozenset(),
}


class IllegalTransition(Exception):
    """Raised when a node status change is not in TRANSITIONS[from]."""

    def __init__(self, frm: NodeStatus, to: NodeStatus) -> None:
        self.frm = frm
        self.to = to
        super().__init__(f"illegal transition: {frm.value} -> {to.value}")


def assert_transition(frm: NodeStatus, to: NodeStatus) -> None:
    if to not in TRANSITIONS.get(frm, frozenset()):
        raise IllegalTransition(frm, to)
