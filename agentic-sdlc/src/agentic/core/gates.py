"""Gate evaluation (Section 5.2): thirteen condition types, aggregated to
the worst of PASS/WARN/FAIL.

Two conditions are structural rather than opt-in: upstream_satisfied and
budget_available are evaluated on every node's entry gate regardless of
whether they appear in its declared GateSpec, because they are
scheduler-level concerns (dependency/join readiness, run budgets) rather
than a per-node policy choice. Every other condition only fires when a
workflow author declares it with node-specific params (which artifact
names, which schema, which coverage floor, ...).

Notably absent from the structural set: approval_granted. Section 6.2's
approval flow needs to distinguish "not ready" from "ready but awaiting
a human", so the engine (not the entry gate) decides whether an
otherwise-ready, requires_approval node may proceed — see
Engine._partition_by_approval in engine.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel

from agentic.core.context import ContextStore
from agentic.core.graph import WorkflowGraph
from agentic.core.models import (
    AutonomyLevel,
    GateResult,
    GateVerdict,
    NodeRun,
    NodeSpec,
    PolicyVerdict,
    PolicyVerdictType,
    RunState,
)
from agentic.core.states import NodeStatus

_VERDICT_RANK = {GateVerdict.PASS_: 0, GateVerdict.WARN: 1, GateVerdict.FAIL: 2}


def aggregate(results: list[GateResult]) -> GateVerdict:
    """The aggregate verdict is the worst individual verdict."""
    if not results:
        return GateVerdict.PASS_
    return max((r.verdict for r in results), key=lambda v: _VERDICT_RANK[v])


@dataclass
class GateContext:
    """Everything a condition evaluator might need. Governance (P3),
    observability (P4) and the LLM/tool layers (P5-P6) populate the
    optional fields as they come online; until then conditions that
    depend on them degrade to PASS with an explanatory message rather
    than blocking nodes on data that does not exist yet."""

    node: NodeSpec
    graph: WorkflowGraph
    context: ContextStore
    run_state: RunState
    node_run: NodeRun | None = None
    policy_verdicts: list[PolicyVerdict] = field(default_factory=list)
    agent_autonomy: AutonomyLevel | None = None
    approved_input_hashes: dict[str, str] = field(default_factory=dict)
    budget_exhausted: bool = False
    schema_registry: dict[str, type[BaseModel]] = field(default_factory=dict)
    test_results: dict | None = None
    security_findings: list[dict] | None = None


ConditionFn = Callable[["GateContext", dict], tuple[GateVerdict, str]]


def _join_satisfied(ctx: GateContext) -> bool:
    node = ctx.node
    if not node.depends_on:
        return True
    statuses = []
    for dep in node.depends_on:
        dep_run = ctx.run_state.nodes.get(dep)
        statuses.append(dep_run.status if dep_run else NodeStatus.PENDING)
    if node.join_policy == "all":
        return all(s == NodeStatus.SUCCEEDED for s in statuses)
    if node.join_policy == "any":
        return any(s == NodeStatus.SUCCEEDED for s in statuses)
    if node.join_policy == "quorum":
        threshold = node.join_quorum if node.join_quorum is not None else len(statuses)
        return sum(1 for s in statuses if s == NodeStatus.SUCCEEDED) >= threshold
    raise ValueError(f"unknown join_policy: {node.join_policy}")


def _cond_upstream_satisfied(ctx: GateContext, params: dict) -> tuple[GateVerdict, str]:
    if _join_satisfied(ctx):
        return GateVerdict.PASS_, "upstream dependencies satisfied"
    return GateVerdict.FAIL, "upstream dependencies not yet satisfied"


def _cond_artifacts_present(ctx: GateContext, params: dict) -> tuple[GateVerdict, str]:
    names: list[str] = params.get("names", [])
    view = ctx.context.view_for(ctx.node.node_id)
    missing = [n for n in names if n not in view or not (a := view.get(n)) or not a.payload]
    if missing:
        return GateVerdict.FAIL, f"missing required artifacts: {missing}"
    return GateVerdict.PASS_, "all required artifacts present"


def _cond_schema_valid(ctx: GateContext, params: dict) -> tuple[GateVerdict, str]:
    name = params.get("name")
    if not name:
        return GateVerdict.PASS_, "no artifact named for schema check"
    schema = ctx.schema_registry.get(name)
    if schema is None:
        return GateVerdict.PASS_, f"no schema registered for '{name}'"
    try:
        artifact = ctx.context.get(name)
    except KeyError:
        return GateVerdict.FAIL, f"artifact '{name}' not found"
    try:
        if isinstance(artifact.payload, dict):
            schema.model_validate(artifact.payload)
        else:
            schema.model_validate_json(artifact.payload)
    except Exception as exc:  # pydantic ValidationError, JSON decode errors, ...
        return GateVerdict.FAIL, f"schema validation failed for '{name}': {exc}"
    return GateVerdict.PASS_, f"'{name}' validates against its schema"


def _cond_policy_clear(ctx: GateContext, params: dict) -> tuple[GateVerdict, str]:
    if any(v.verdict == PolicyVerdictType.DENY for v in ctx.policy_verdicts):
        return GateVerdict.FAIL, "a policy rule returned DENY"
    if any(
        v.verdict in (PolicyVerdictType.WARN, PolicyVerdictType.REQUIRE_APPROVAL)
        for v in ctx.policy_verdicts
    ):
        return GateVerdict.WARN, "a policy rule returned WARN or REQUIRE_APPROVAL"
    return GateVerdict.PASS_, "no policy findings"


def _cond_autonomy_permitted(ctx: GateContext, params: dict) -> tuple[GateVerdict, str]:
    if ctx.node.agent is None:
        return GateVerdict.PASS_, "control node, no agent autonomy required"
    if ctx.agent_autonomy is None:
        return GateVerdict.FAIL, "no autonomy ceiling supplied for the assigned agent"
    if ctx.agent_autonomy >= ctx.node.autonomy_required:
        return GateVerdict.PASS_, "agent autonomy ceiling permits this node"
    return GateVerdict.FAIL, "agent autonomy ceiling is below the node's requirement"


def _cond_approval_granted(ctx: GateContext, params: dict) -> tuple[GateVerdict, str]:
    if not ctx.node.requires_approval:
        return GateVerdict.PASS_, "node does not require approval"
    current_hash = ctx.context.input_hash(ctx.node.node_id)
    granted_hash = ctx.approved_input_hashes.get(ctx.node.node_id)
    if granted_hash == current_hash:
        return GateVerdict.PASS_, "approval recorded for current input_hash"
    return GateVerdict.FAIL, "approval missing or void (input changed since approval)"


def _cond_budget_available(ctx: GateContext, params: dict) -> tuple[GateVerdict, str]:
    if ctx.budget_exhausted:
        return GateVerdict.FAIL, "run budget exhausted"
    return GateVerdict.PASS_, "budget available"


def _cond_artifact_produced(ctx: GateContext, params: dict) -> tuple[GateVerdict, str]:
    names: list[str] = params.get("names", [])
    if not names:
        return GateVerdict.PASS_, "no output contract declared"
    if ctx.node_run is None:
        return GateVerdict.FAIL, "node has not executed"
    produced_names = {
        a.name for aid in ctx.node_run.produced if (a := ctx.context.get_by_id(aid)) is not None
    }
    missing = [n for n in names if n not in produced_names]
    if missing:
        return GateVerdict.FAIL, f"node did not produce declared artifacts: {missing}"
    return GateVerdict.PASS_, "all declared output artifacts produced"


def _upstream_payload(ctx: GateContext, artifact_name: str) -> dict | None:
    """Fallback for join-style nodes (e.g. verify.gate) that have no
    test_results/security_findings of their own on GateContext but sit
    downstream of a node that produced an artifact by that name — read
    it from the node's context view instead of requiring the engine to
    thread it through explicitly."""
    artifact = ctx.context.view_for(ctx.node.node_id).get(artifact_name)
    if artifact is None or not isinstance(artifact.payload, dict):
        return None
    return artifact.payload


def _cond_tests_pass(ctx: GateContext, params: dict) -> tuple[GateVerdict, str]:
    results = ctx.test_results if ctx.test_results is not None else _upstream_payload(ctx, "test_results")
    if results is None:
        return GateVerdict.PASS_, "no test execution associated with this node"
    if results.get("exit_code", 0) == 0:
        return GateVerdict.PASS_, "tests passed"
    return GateVerdict.FAIL, "test execution returned a non-zero exit code"


def _cond_coverage_threshold(ctx: GateContext, params: dict) -> tuple[GateVerdict, str]:
    results = ctx.test_results if ctx.test_results is not None else _upstream_payload(ctx, "test_results")
    if results is None or "coverage" not in results:
        return GateVerdict.PASS_, "no coverage data associated with this node"
    floor = params.get("floor", 0.80)
    coverage = results["coverage"]
    if coverage >= floor:
        return GateVerdict.PASS_, f"coverage {coverage:.0%} meets floor {floor:.0%}"
    return GateVerdict.FAIL, f"coverage {coverage:.0%} below floor {floor:.0%}"


def _cond_no_high_findings(ctx: GateContext, params: dict) -> tuple[GateVerdict, str]:
    findings = ctx.security_findings
    if findings is None:
        payload = _upstream_payload(ctx, "security_findings")
        findings = payload.get("findings") if isinstance(payload, dict) else None
    if not findings:
        return GateVerdict.PASS_, "no security findings"
    high = [f for f in findings if f.get("severity") in ("HIGH", "CRITICAL")]
    if high:
        return GateVerdict.FAIL, f"{len(high)} HIGH/CRITICAL security finding(s)"
    return GateVerdict.PASS_, "no HIGH/CRITICAL security findings"


def _cond_decision_recorded(ctx: GateContext, params: dict) -> tuple[GateVerdict, str]:
    if ctx.node_run is None or not ctx.node_run.decisions:
        return GateVerdict.FAIL, "no decision with rationale recorded for a design-stage node"
    return GateVerdict.PASS_, "at least one decision recorded"


def _cond_lineage_complete(ctx: GateContext, params: dict) -> tuple[GateVerdict, str]:
    if ctx.node_run is None or not ctx.node_run.produced:
        return GateVerdict.PASS_, "node produced no artifacts"
    if not ctx.node.depends_on:
        return GateVerdict.PASS_, "root node, no upstream lineage required"
    if not ctx.node_run.decisions:
        return GateVerdict.FAIL, "produced artifacts are not linked to any decision"
    return GateVerdict.PASS_, "produced artifacts trace to recorded decisions"


_CONDITIONS: dict[str, ConditionFn] = {
    "upstream_satisfied": _cond_upstream_satisfied,
    "artifacts_present": _cond_artifacts_present,
    "schema_valid": _cond_schema_valid,
    "policy_clear": _cond_policy_clear,
    "autonomy_permitted": _cond_autonomy_permitted,
    "approval_granted": _cond_approval_granted,
    "budget_available": _cond_budget_available,
    "artifact_produced": _cond_artifact_produced,
    "tests_pass": _cond_tests_pass,
    "coverage_threshold": _cond_coverage_threshold,
    "no_high_findings": _cond_no_high_findings,
    "decision_recorded": _cond_decision_recorded,
    "lineage_complete": _cond_lineage_complete,
}

_STRUCTURAL_ENTRY_CONDITIONS = ("upstream_satisfied", "budget_available")


class GateEvaluator:
    def evaluate_entry(self, ctx: GateContext) -> list[GateResult]:
        results: list[GateResult] = []
        declared_types = {c.type for c in ctx.node.entry_gate.conditions}

        for cond_type in _STRUCTURAL_ENTRY_CONDITIONS:
            if cond_type in declared_types:
                continue  # declared explicitly below with its own params instead
            verdict, message = _CONDITIONS[cond_type](ctx, {})
            results.append(
                GateResult(
                    node_id=ctx.node.node_id,
                    phase="entry",
                    condition=cond_type,
                    verdict=verdict,
                    message=message,
                )
            )

        for cond in ctx.node.entry_gate.conditions:
            fn = _CONDITIONS.get(cond.type)
            if fn is None:
                raise ValueError(f"unknown entry gate condition type: {cond.type}")
            verdict, message = fn(ctx, cond.params)
            results.append(
                GateResult(
                    node_id=ctx.node.node_id,
                    phase="entry",
                    condition=cond.type,
                    verdict=verdict,
                    message=message,
                )
            )
        return results

    def evaluate_exit(self, ctx: GateContext) -> list[GateResult]:
        results: list[GateResult] = []
        for cond in ctx.node.exit_gate.conditions:
            fn = _CONDITIONS.get(cond.type)
            if fn is None:
                raise ValueError(f"unknown exit gate condition type: {cond.type}")
            verdict, message = fn(ctx, cond.params)
            results.append(
                GateResult(
                    node_id=ctx.node.node_id,
                    phase="exit",
                    condition=cond.type,
                    verdict=verdict,
                    message=message,
                )
            )
        return results
