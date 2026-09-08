"""PolicyEngine hook dispatch and verdict aggregation."""

from agentic.core.models import NodeSpec, PolicyVerdictType, SDLCStage
from agentic.governance.policy import PolicyContext, PolicyEngine, aggregate


def test_evaluate_only_runs_rules_assigned_to_the_given_hook() -> None:
    engine = PolicyEngine()
    node = NodeSpec(node_id="n", stage=SDLCStage.IMPLEMENTATION)
    ctx = PolicyContext(node=node, hook="before_tool_invocation")

    results = engine.evaluate("before_tool_invocation", ctx)
    rule_ids = {r.rule_id for r in results}

    assert rule_ids == {"SEC-004", "SEC-005", "CHG-006"}


def test_evaluate_after_agent_output_runs_the_larger_rule_set() -> None:
    engine = PolicyEngine()
    node = NodeSpec(node_id="n", stage=SDLCStage.IMPLEMENTATION)
    ctx = PolicyContext(node=node, hook="after_agent_output")

    results = engine.evaluate("after_agent_output", ctx)

    assert len(results) == 13
    assert all(r.verdict == PolicyVerdictType.ALLOW for r in results)  # clean context, nothing fires


def test_aggregate_picks_deny_over_require_approval_over_warn() -> None:
    engine = PolicyEngine()
    node = NodeSpec(node_id="n", stage=SDLCStage.DESIGN_REVIEW)
    ctx = PolicyContext(node=node, hook="before_node_entry")

    results = engine.evaluate("before_node_entry", ctx)  # GOV-001 fires REQUIRE_APPROVAL here

    assert aggregate(results) == PolicyVerdictType.REQUIRE_APPROVAL

    ctx.budget_exhausted = True
    results = engine.evaluate("before_node_entry", ctx)  # GOV-002 now also fires DENY
    assert aggregate(results) == PolicyVerdictType.DENY
