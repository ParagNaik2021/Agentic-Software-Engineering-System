"""ApprovalManager (Section 6.2): human approval checkpoints bound to a
node's input_hash. If inputs change after approval, the approval is void
and must be re-sought — this is what closes the loop between approvals
and re-planning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from agentic.core.context import ContextStore
from agentic.core.models import Artifact, Decision, NodeRun, NodeSpec, PolicyVerdict

ApprovalStatus = Literal["PENDING", "GRANTED", "REJECTED", "VOID"]


class ApprovalNotPermitted(Exception):
    """Raised when an approve/reject action cannot be honoured at all —
    the run has already terminated, the node is not at a checkpoint, or
    the identical decision already stands. Distinct from ApprovalVoid,
    which means the checkpoint is still live but its inputs moved.
    """



@dataclass
class DecisionPackage:
    """Everything Section 6.2 says must be presented to the approver."""

    node_id: str
    stage: str
    input_hash: str
    artifacts: list[Artifact]
    decisions: list[Decision]
    policy_verdicts: list[PolicyVerdict]
    blast_radius: dict
    consequence_of_rejection: str


@dataclass
class ApprovalRecord:
    node_id: str
    input_hash: str
    status: ApprovalStatus = "PENDING"
    note: str = ""
    approver: str | None = None
    requested_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None


class ApprovalVoid(Exception):
    pass


def _consequence_of_rejection(node: NodeSpec) -> str:
    """What actually happens on rejection is the same for every
    requires_approval node in this engine: reject_approval() transitions
    straight to REJECTED (core/engine.py), which is not a candidate
    status the scheduler ever revisits and has no rollback_handler wired
    for it, so the next tick finds nothing ready and safe-stops on
    deadlock. There is no automatic rollback here — that path only
    exists for a node FAILED during execution, not one rejected before
    it ever ran. What differs by node type is what a human's
    `--from-rejection` recovery actually re-opens."""
    recover_cmd = f"agentic replan <run_id> --node {node.node_id} --from-rejection"
    if node.agent is None:
        return (
            "this is a control checkpoint with no agent of its own — rejecting it halts the run "
            f"via safe-stop (deadlock), not a rollback. Recovery requires an operator to run "
            f"`{recover_cmd}`, which re-opens {', '.join(node.depends_on) or 'its upstream'} with "
            "the rejection note attached so they re-run informed by it, then re-evaluates this "
            "checkpoint once they finish"
        )
    return (
        f"rejecting this halts the run via safe-stop (deadlock) — the '{node.agent}' agent's own "
        "output is not rolled back automatically. Recovery requires an operator to run "
        f"`{recover_cmd}`, which re-opens {', '.join(node.depends_on) or 'its upstream'} with the "
        f"rejection note attached and re-runs '{node.agent}' to produce a fresh recommendation"
    )


class ApprovalManager:
    def __init__(self) -> None:
        self._records: dict[str, ApprovalRecord] = {}

    def request(self, node_id: str, input_hash: str) -> ApprovalRecord:
        record = ApprovalRecord(node_id=node_id, input_hash=input_hash, status="PENDING")
        self._records[node_id] = record
        return record

    def get(self, node_id: str) -> ApprovalRecord | None:
        return self._records.get(node_id)

    def all(self) -> list[ApprovalRecord]:
        return list(self._records.values())

    def render_package(
        self,
        node: NodeSpec,
        node_run: NodeRun,
        context: ContextStore,
        policy_verdicts: list[PolicyVerdict] | None = None,
        blast_radius: dict | None = None,
    ) -> DecisionPackage:
        view = context.view_for(node.node_id)
        decisions = [
            d for did in node_run.decisions if (d := context.get_decision(did)) is not None
        ]
        # view_for() is strictly upstream-of-node_id, which is correct for
        # a *pending* approval (the node hasn't run, so it has produced
        # nothing yet to show). But render_package is also used
        # retrospectively (`agentic approvals show` after a run has
        # completed), and for a node with agent="release_manager" (or
        # any other agent-bearing gate) the whole point of looking is its
        # own output — the go/no-go recommendation, not its inputs.
        # node_run.produced already exists for exactly this case: it's
        # only non-empty once the node has actually executed.
        own_artifacts = [
            a for aid in node_run.produced if (a := context.get_by_id(aid)) is not None
        ]
        return DecisionPackage(
            node_id=node.node_id,
            stage=node.stage.value,
            input_hash=context.input_hash(node.node_id),
            artifacts=[*view.artifacts.values(), *own_artifacts],
            decisions=decisions,
            policy_verdicts=policy_verdicts or [],
            blast_radius=blast_radius or {"files": [], "endpoints": []},
            consequence_of_rejection=_consequence_of_rejection(node),
        )

    def grant(self, node_id: str, current_input_hash: str, approver: str, note: str = "") -> ApprovalRecord:
        record = self._records.get(node_id)
        if record is None:
            record = self.request(node_id, current_input_hash)
        if record.input_hash != current_input_hash:
            raise ApprovalVoid(
                f"cannot grant: pending approval for '{node_id}' was requested against a "
                f"different input_hash (inputs changed since the request)"
            )
        record.status = "GRANTED"
        record.approver = approver
        record.note = note
        record.decided_at = datetime.now(UTC)
        return record

    def reject(self, node_id: str, approver: str, note: str = "") -> ApprovalRecord:
        record = self._records.get(node_id)
        if record is None:
            raise KeyError(f"no approval record for node '{node_id}'")
        record.status = "REJECTED"
        record.approver = approver
        record.note = note
        record.decided_at = datetime.now(UTC)
        return record

    def is_granted(self, node_id: str, current_input_hash: str) -> bool:
        record = self._records.get(node_id)
        return (
            record is not None
            and record.status == "GRANTED"
            and record.input_hash == current_input_hash
        )

    def void_if_stale(self, node_id: str, current_input_hash: str) -> bool:
        """Section 6.2: "If inputs change after approval, the approval is
        void and must be re-sought." Returns True iff a previously
        GRANTED record was voided by this call."""
        record = self._records.get(node_id)
        if record is not None and record.status == "GRANTED" and record.input_hash != current_input_hash:
            record.status = "VOID"
            return True
        return False
