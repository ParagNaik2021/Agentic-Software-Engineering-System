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
        return DecisionPackage(
            node_id=node.node_id,
            stage=node.stage.value,
            input_hash=context.input_hash(node.node_id),
            artifacts=list(view.artifacts.values()),
            decisions=decisions,
            policy_verdicts=policy_verdicts or [],
            blast_radius=blast_radius or {"files": [], "endpoints": []},
            consequence_of_rejection=(
                "the node is routed to rollback; any workspace changes since the last "
                "checkpoint are reverted and the run awaits revised guidance"
            ),
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
