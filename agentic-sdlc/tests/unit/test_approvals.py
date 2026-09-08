"""P3 acceptance: an approval granted, then invalidated by an input
change, is not honoured."""

from datetime import UTC, datetime

import pytest

from agentic.core.context import ContextStore
from agentic.core.graph import WorkflowGraph
from agentic.core.models import Artifact, NodeRun, NodeSpec, SDLCStage, compute_content_hash
from agentic.core.states import NodeStatus
from agentic.governance.approvals import ApprovalManager, ApprovalVoid

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _graph() -> WorkflowGraph:
    graph = WorkflowGraph(root="intake")
    graph.add_node(NodeSpec(node_id="intake", stage=SDLCStage.INTAKE))
    graph.add_node(
        NodeSpec(node_id="design", stage=SDLCStage.DESIGN_REVIEW, depends_on=["intake"],
                 requires_approval=True)
    )
    graph.validate()
    return graph


def _artifact(payload: dict, version: int = 1) -> Artifact:
    return Artifact(
        artifact_id=f"spec-v{version}", name="spec", kind="spec",
        content_hash=compute_content_hash(payload), payload=payload,
        produced_by_node="intake", produced_by_agent="requirements", run_id="run-1",
        created_at=NOW, version=version,
    )


def test_grant_then_input_change_voids_the_approval() -> None:
    graph = _graph()
    context = ContextStore(graph)
    context.put(_artifact({"v": 1}))

    manager = ApprovalManager()
    h1 = context.input_hash("design")
    manager.request("design", h1)
    manager.grant("design", h1, approver="alice")

    assert manager.is_granted("design", h1) is True

    # inputs change: a new version of the upstream artifact arrives
    context.put(_artifact({"v": 2}, version=2))
    h2 = context.input_hash("design")

    assert h2 != h1
    assert manager.is_granted("design", h2) is False  # not honoured against the new input_hash

    voided = manager.void_if_stale("design", h2)
    assert voided is True
    assert manager.get("design").status == "VOID"


def test_grant_fails_outright_if_input_already_changed_before_granting() -> None:
    graph = _graph()
    context = ContextStore(graph)
    context.put(_artifact({"v": 1}))

    manager = ApprovalManager()
    h1 = context.input_hash("design")
    manager.request("design", h1)

    context.put(_artifact({"v": 2}, version=2))
    h2 = context.input_hash("design")

    with pytest.raises(ApprovalVoid):
        manager.grant("design", h2, approver="alice")


def test_render_package_includes_artifacts_decisions_and_policy_verdicts() -> None:
    from agentic.core.models import Decision, PolicyVerdict, PolicyVerdictType

    graph = _graph()
    context = ContextStore(graph)
    context.put(_artifact({"v": 1}))
    context.record_decision(
        Decision(decision_id="d1", node_id="intake", agent="requirements",
                  statement="s", rationale="r", created_at=NOW)
    )
    node_run = NodeRun(node_id="design", run_id="run-1", status=NodeStatus.PENDING, decisions=["d1"])

    manager = ApprovalManager()
    package = manager.render_package(
        node=graph["design"], node_run=node_run, context=context,
        policy_verdicts=[PolicyVerdict(rule_id="GOV-001", category="governance",
                                        verdict=PolicyVerdictType.REQUIRE_APPROVAL)],
    )

    assert package.node_id == "design"
    assert any(a.name == "spec" for a in package.artifacts)
    assert len(package.decisions) == 1
    assert package.policy_verdicts[0].rule_id == "GOV-001"
    assert "rollback" in package.consequence_of_rejection


def test_reject_records_the_decision() -> None:
    manager = ApprovalManager()
    manager.request("design", "hash1")
    record = manager.reject("design", approver="bob", note="scope too broad")

    assert record.status == "REJECTED"
    assert record.approver == "bob"
    assert record.note == "scope too broad"
