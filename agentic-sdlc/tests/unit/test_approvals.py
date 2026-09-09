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


def test_render_package_includes_the_nodes_own_output_once_it_has_executed() -> None:
    """Bug: `agentic approvals show <run> release.readiness` was showing
    only upstream context (spec, tasks, design docs) and never the
    release_manager's own recommendation. view_for() is deliberately
    upstream-only (correct for a *pending* approval, which hasn't run
    yet and has nothing of its own to show) — but render_package is also
    used retrospectively, after the node has executed, and at that point
    its own produced artifacts are exactly what the reviewer showed up
    to see. NodeRun.produced is the record of what a node produced."""
    graph = _graph()
    context = ContextStore(graph)
    context.put(_artifact({"v": 1}))
    own_output = Artifact(
        artifact_id="release-report-1", name="release_readiness_report", kind="report",
        content_hash="h", payload={"go_no_go": "go", "risks": [], "limitations": []},
        produced_by_node="design", produced_by_agent="release_manager", run_id="run-1", created_at=NOW,
    )
    context.put(own_output)
    node_run = NodeRun(
        node_id="design", run_id="run-1", status=NodeStatus.SUCCEEDED, produced=["release-report-1"],
    )

    manager = ApprovalManager()
    package = manager.render_package(node=graph["design"], node_run=node_run, context=context)

    names = {a.name for a in package.artifacts}
    assert "spec" in names  # still has upstream context
    assert "release_readiness_report" in names  # and now its own output too


def test_render_package_shows_nothing_of_its_own_before_the_node_has_run() -> None:
    """The flip side: a genuinely pending approval (produced=[]) must not
    regress — there is nothing of the node's own to show yet, only
    upstream context, same as before this fix."""
    graph = _graph()
    context = ContextStore(graph)
    context.put(_artifact({"v": 1}))
    node_run = NodeRun(node_id="design", run_id="run-1", status=NodeStatus.AWAITING_APPROVAL, produced=[])

    manager = ApprovalManager()
    package = manager.render_package(node=graph["design"], node_run=node_run, context=context)

    assert {a.name for a in package.artifacts} == {"spec"}


def test_consequence_of_rejection_is_dynamic_and_never_claims_rollback() -> None:
    """Bug fix: the old hardcoded string claimed "the node is routed to
    rollback" for every rejected node, which is false for a control node
    like design.review (no agent, no rollback_handler ever wired for a
    rejected — as opposed to FAILED — node). The text must accurately
    describe the actual mechanism (safe-stop + --from-rejection) and
    differ meaningfully between a pure control node and an agent-backed
    one."""
    graph = WorkflowGraph(root="intake")
    graph.add_node(NodeSpec(node_id="intake", stage=SDLCStage.INTAKE))
    graph.add_node(
        NodeSpec(node_id="design_a", stage=SDLCStage.ARCHITECTURE, depends_on=["intake"])
    )
    graph.add_node(
        NodeSpec(
            node_id="control_gate", stage=SDLCStage.DESIGN_REVIEW, depends_on=["design_a"],
            requires_approval=True,
        )
    )
    graph.add_node(
        NodeSpec(
            node_id="agent_gate", stage=SDLCStage.RELEASE_READINESS, agent="release_manager",
            depends_on=["design_a"], requires_approval=True,
        )
    )
    graph.validate()
    context = ContextStore(graph)
    context.put(_artifact({"v": 1}))
    manager = ApprovalManager()

    control_package = manager.render_package(
        node=graph["control_gate"],
        node_run=NodeRun(node_id="control_gate", run_id="run-1", status=NodeStatus.REJECTED),
        context=context,
    )
    agent_package = manager.render_package(
        node=graph["agent_gate"],
        node_run=NodeRun(node_id="agent_gate", run_id="run-1", status=NodeStatus.REJECTED),
        context=context,
    )

    for text in (control_package.consequence_of_rejection, agent_package.consequence_of_rejection):
        assert "routed to rollback" not in text
        assert "safe-stop" in text
        assert "--from-rejection" in text

    # the two must actually differ — the agent-backed one names its agent
    assert control_package.consequence_of_rejection != agent_package.consequence_of_rejection
    assert "release_manager" in agent_package.consequence_of_rejection
    assert "release_manager" not in control_package.consequence_of_rejection
    assert "design_a" in control_package.consequence_of_rejection  # names what --from-rejection reopens


def test_reject_records_the_decision() -> None:
    manager = ApprovalManager()
    manager.request("design", "hash1")
    record = manager.reject("design", approver="bob", note="scope too broad")

    assert record.status == "REJECTED"
    assert record.approver == "bob"
    assert record.note == "scope too broad"
