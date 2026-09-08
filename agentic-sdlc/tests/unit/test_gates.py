"""P2: gate condition evaluators and verdict aggregation."""

from datetime import UTC, datetime

from agentic.core.context import ContextStore
from agentic.core.gates import GateContext, GateEvaluator, aggregate
from agentic.core.graph import WorkflowGraph
from agentic.core.models import (
    Artifact,
    GateConditionSpec,
    GateResult,
    GateSpec,
    GateVerdict,
    NodeRun,
    NodeSpec,
    PolicyVerdict,
    PolicyVerdictType,
    RunState,
    RunStatus,
    SDLCStage,
    compute_content_hash,
)
from agentic.core.states import NodeStatus

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _run_state(graph: WorkflowGraph, node_statuses: dict[str, NodeStatus]) -> RunState:
    nodes = {
        nid: NodeRun(node_id=nid, run_id="run-1", status=status)
        for nid, status in node_statuses.items()
    }
    return RunState(
        run_id="run-1", scenario="s", workflow="w", status=RunStatus.RUNNING,
        nodes=nodes, created_at=NOW,
    )


def test_aggregate_empty_is_pass() -> None:
    assert aggregate([]) == GateVerdict.PASS_


def test_aggregate_takes_worst_verdict() -> None:
    results = [
        GateResult(node_id="n", phase="entry", condition="a", verdict=GateVerdict.PASS_),
        GateResult(node_id="n", phase="entry", condition="b", verdict=GateVerdict.WARN),
    ]
    assert aggregate(results) == GateVerdict.WARN
    results.append(GateResult(node_id="n", phase="entry", condition="c", verdict=GateVerdict.FAIL))
    assert aggregate(results) == GateVerdict.FAIL


def test_upstream_satisfied_structural_condition_is_always_evaluated() -> None:
    graph = WorkflowGraph(root="a")
    graph.add_node(NodeSpec(node_id="a", stage=SDLCStage.INTAKE))
    graph.add_node(NodeSpec(node_id="b", stage=SDLCStage.REQUIREMENTS, depends_on=["a"]))
    graph.validate()
    context = ContextStore(graph)
    state = _run_state(graph, {"a": NodeStatus.PENDING, "b": NodeStatus.PENDING})

    ctx = GateContext(node=graph["b"], graph=graph, context=context, run_state=state)
    results = GateEvaluator().evaluate_entry(ctx)

    assert any(r.condition == "upstream_satisfied" and r.verdict == GateVerdict.FAIL for r in results)


def test_upstream_satisfied_passes_once_dependency_succeeds() -> None:
    graph = WorkflowGraph(root="a")
    graph.add_node(NodeSpec(node_id="a", stage=SDLCStage.INTAKE))
    graph.add_node(NodeSpec(node_id="b", stage=SDLCStage.REQUIREMENTS, depends_on=["a"]))
    graph.validate()
    context = ContextStore(graph)
    state = _run_state(graph, {"a": NodeStatus.SUCCEEDED, "b": NodeStatus.PENDING})

    ctx = GateContext(node=graph["b"], graph=graph, context=context, run_state=state)
    verdict = aggregate(GateEvaluator().evaluate_entry(ctx))

    assert verdict == GateVerdict.PASS_


def test_join_policy_any_passes_with_one_success() -> None:
    graph = WorkflowGraph(root="a")
    graph.add_node(NodeSpec(node_id="a", stage=SDLCStage.INTAKE))
    graph.add_node(NodeSpec(node_id="b1", stage=SDLCStage.REQUIREMENTS, depends_on=["a"]))
    graph.add_node(NodeSpec(node_id="b2", stage=SDLCStage.REQUIREMENTS, depends_on=["a"]))
    graph.add_node(
        NodeSpec(
            node_id="join", stage=SDLCStage.CLARIFICATION, depends_on=["b1", "b2"],
            join_policy="any",
        )
    )
    graph.validate()
    context = ContextStore(graph)
    state = _run_state(
        graph,
        {
            "a": NodeStatus.SUCCEEDED,
            "b1": NodeStatus.SUCCEEDED,
            "b2": NodeStatus.FAILED,
            "join": NodeStatus.PENDING,
        },
    )

    ctx = GateContext(node=graph["join"], graph=graph, context=context, run_state=state)
    verdict = aggregate(GateEvaluator().evaluate_entry(ctx))

    assert verdict == GateVerdict.PASS_


def test_join_policy_quorum_respects_threshold() -> None:
    graph = WorkflowGraph(root="a")
    graph.add_node(NodeSpec(node_id="a", stage=SDLCStage.INTAKE))
    for i in range(3):
        graph.add_node(NodeSpec(node_id=f"b{i}", stage=SDLCStage.REQUIREMENTS, depends_on=["a"]))
    graph.add_node(
        NodeSpec(
            node_id="join", stage=SDLCStage.CLARIFICATION, depends_on=["b0", "b1", "b2"],
            join_policy="quorum", join_quorum=2,
        )
    )
    graph.validate()
    context = ContextStore(graph)

    state = _run_state(
        graph,
        {
            "a": NodeStatus.SUCCEEDED, "b0": NodeStatus.SUCCEEDED,
            "b1": NodeStatus.FAILED, "b2": NodeStatus.PENDING, "join": NodeStatus.PENDING,
        },
    )
    ctx = GateContext(node=graph["join"], graph=graph, context=context, run_state=state)
    assert aggregate(GateEvaluator().evaluate_entry(ctx)) == GateVerdict.FAIL  # only 1 of 2 needed

    state.nodes["b2"].status = NodeStatus.SUCCEEDED
    ctx = GateContext(node=graph["join"], graph=graph, context=context, run_state=state)
    assert aggregate(GateEvaluator().evaluate_entry(ctx)) == GateVerdict.PASS_  # now 2 of 2


def test_artifacts_present_fails_when_missing() -> None:
    graph = WorkflowGraph(root="a")
    graph.add_node(NodeSpec(node_id="a", stage=SDLCStage.INTAKE))
    graph.add_node(
        NodeSpec(
            node_id="b",
            stage=SDLCStage.REQUIREMENTS,
            depends_on=["a"],
            entry_gate=GateSpec(
                conditions=[GateConditionSpec(type="artifacts_present", params={"names": ["spec"]})]
            ),
        )
    )
    graph.validate()
    context = ContextStore(graph)
    state = _run_state(graph, {"a": NodeStatus.SUCCEEDED, "b": NodeStatus.PENDING})

    ctx = GateContext(node=graph["b"], graph=graph, context=context, run_state=state)
    assert aggregate(GateEvaluator().evaluate_entry(ctx)) == GateVerdict.FAIL

    context.put(
        Artifact(
            artifact_id="spec-1", name="spec", kind="spec",
            content_hash=compute_content_hash({"k": "v"}),
            payload={"k": "v"}, produced_by_node="a", produced_by_agent="requirements",
            run_id="run-1", created_at=NOW,
        )
    )
    ctx = GateContext(node=graph["b"], graph=graph, context=context, run_state=state)
    assert aggregate(GateEvaluator().evaluate_entry(ctx)) == GateVerdict.PASS_


def test_policy_clear_deny_fails_and_warn_warns() -> None:
    graph = WorkflowGraph(root="a")
    graph.add_node(
        NodeSpec(
            node_id="a", stage=SDLCStage.INTAKE,
            entry_gate=GateSpec(conditions=[GateConditionSpec(type="policy_clear")]),
        )
    )
    graph.validate()
    context = ContextStore(graph)
    state = _run_state(graph, {"a": NodeStatus.PENDING})

    ctx = GateContext(
        node=graph["a"], graph=graph, context=context, run_state=state,
        policy_verdicts=[PolicyVerdict(rule_id="SEC-001", category="security", verdict=PolicyVerdictType.DENY)],
    )
    assert aggregate(GateEvaluator().evaluate_entry(ctx)) == GateVerdict.FAIL

    ctx = GateContext(
        node=graph["a"], graph=graph, context=context, run_state=state,
        policy_verdicts=[PolicyVerdict(rule_id="SEC-007", category="security", verdict=PolicyVerdictType.WARN)],
    )
    assert aggregate(GateEvaluator().evaluate_entry(ctx)) == GateVerdict.WARN


def test_no_high_findings_fails_on_high_severity() -> None:
    graph = WorkflowGraph(root="a")
    graph.add_node(
        NodeSpec(
            node_id="a", stage=SDLCStage.SECURITY_REVIEW,
            exit_gate=GateSpec(conditions=[GateConditionSpec(type="no_high_findings")]),
        )
    )
    graph.validate()
    context = ContextStore(graph)
    state = _run_state(graph, {"a": NodeStatus.RUNNING})

    ctx = GateContext(
        node=graph["a"], graph=graph, context=context, run_state=state,
        node_run=state.nodes["a"], security_findings=[{"severity": "HIGH"}],
    )
    assert aggregate(GateEvaluator().evaluate_exit(ctx)) == GateVerdict.FAIL

    ctx.security_findings = [{"severity": "LOW"}]
    assert aggregate(GateEvaluator().evaluate_exit(ctx)) == GateVerdict.PASS_


def test_coverage_threshold() -> None:
    graph = WorkflowGraph(root="a")
    graph.add_node(
        NodeSpec(
            node_id="a", stage=SDLCStage.UNIT_TEST,
            exit_gate=GateSpec(
                conditions=[GateConditionSpec(type="coverage_threshold", params={"floor": 0.8})]
            ),
        )
    )
    graph.validate()
    context = ContextStore(graph)
    state = _run_state(graph, {"a": NodeStatus.RUNNING})

    ctx = GateContext(
        node=graph["a"], graph=graph, context=context, run_state=state,
        node_run=state.nodes["a"], test_results={"exit_code": 0, "coverage": 0.75},
    )
    assert aggregate(GateEvaluator().evaluate_exit(ctx)) == GateVerdict.FAIL

    ctx.test_results = {"exit_code": 0, "coverage": 0.85}
    assert aggregate(GateEvaluator().evaluate_exit(ctx)) == GateVerdict.PASS_
