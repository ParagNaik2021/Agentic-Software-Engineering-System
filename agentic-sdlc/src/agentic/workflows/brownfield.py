"""Brownfield workflow (Section 11.2): operates on the repository
workflows/greenfield.py already produced. Inserts analysis.impact as a
*mandatory* node (Section 5.1: "Optional; active only in brownfield")
ahead of design.api, using a real AstIndex over the existing workspace —
not a cassette-only claim about what changed.

Change-control is enforced directly rather than through a full
PolicyEngine wiring into the engine (a known gap noted since P4):
_impl_executor_with_impact_check wraps the implementer dispatch with a
live PolicyEngine.evaluate() call carrying CHG-001 (impact report must
exist), and CHG-002 (change budget) is checked against the actual files
the implementer just wrote.

The fault-injection demo (Section 9.3 / 13 P8: "a rollback event on the
injected failure") needs no special cassette content: it reuses P5's
FaultInjectingProvider wrapped around verify.unit's provider, so a
persistent QUALITY_FAILURE flows through the exact same
governance/recovery.py -> Engine._recover path P8's other tests exercise
with stub executors.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agentic.agents.ambiguity import AmbiguityAgent
from agentic.agents.api_contract import ApiContractAgent
from agentic.agents.base import make_node_executor
from agentic.agents.codebase_analyst import CodebaseAnalystAgent
from agentic.agents.implementer import ImplementerAgent
from agentic.agents.planner import PlannerAgent
from agentic.agents.release_manager import ReleaseManagerAgent
from agentic.agents.requirements import RequirementsAgent
from agentic.agents.security_reviewer import SecurityReviewerAgent
from agentic.agents.technical_writer import TechnicalWriterAgent
from agentic.agents.test_engineer import TestEngineerAgent
from agentic.core.context import ContextStore, ContextView
from agentic.core.engine import NodeExecutionResult, NodeExecutor
from agentic.core.graph import WorkflowGraph
from agentic.core.models import Decision, GateConditionSpec, GateSpec, NodeSpec, SDLCStage
from agentic.governance.policy import PolicyContext, PolicyEngine
from agentic.llm.provider import LLMProvider
from agentic.tools.ast_index import AstIndex
from agentic.tools.fs import JailedFS
from agentic.tools.git import GitCheckpointer
from agentic.workflows import common

RAW_REQUIREMENT = "Add a bulk link creation endpoint to the existing URL shortener service."


def build_graph() -> WorkflowGraph:
    graph = WorkflowGraph(root="intake")
    graph.add_node(NodeSpec(node_id="intake", stage=SDLCStage.INTAKE))
    graph.add_node(
        NodeSpec(node_id="req.analyze", stage=SDLCStage.REQUIREMENTS, agent="requirements", depends_on=["intake"])
    )
    graph.add_node(
        NodeSpec(node_id="req.ambiguity", stage=SDLCStage.REQUIREMENTS, agent="ambiguity", depends_on=["req.analyze"])
    )
    graph.add_node(
        NodeSpec(
            node_id="plan.decompose", stage=SDLCStage.DECOMPOSITION, agent="planner", depends_on=["req.ambiguity"]
        )
    )
    # mandatory in brownfield (Section 5.1); codebase_analyst is L0 — read only
    graph.add_node(
        NodeSpec(
            node_id="analysis.impact", stage=SDLCStage.IMPACT_ANALYSIS, agent="codebase_analyst",
            depends_on=["plan.decompose"],
        )
    )
    graph.add_node(
        NodeSpec(
            node_id="design.api", stage=SDLCStage.API_DESIGN, agent="api_contract",
            depends_on=["plan.decompose", "analysis.impact"],
        )
    )
    graph.add_node(
        NodeSpec(
            node_id="design.review", stage=SDLCStage.DESIGN_REVIEW, depends_on=["design.api"],
            requires_approval=True,
        )
    )
    graph.add_node(NodeSpec(node_id="impl.join", stage=SDLCStage.IMPLEMENTATION, depends_on=["design.review"]))
    for node_id, deps in (
        ("verify.static", ["impl.join"]),
        ("verify.security", ["impl.join"]),
        ("verify.unit", ["impl.join"]),
    ):
        graph.add_node(
            NodeSpec(
                node_id=node_id, stage=SDLCStage.STATIC_ANALYSIS if node_id == "verify.static" else (
                    SDLCStage.SECURITY_REVIEW if node_id == "verify.security" else SDLCStage.UNIT_TEST
                ),
                agent="security_reviewer" if node_id == "verify.security" else (
                    "test_engineer" if node_id == "verify.unit" else None
                ),
                depends_on=deps, parallel_group="verify",
            )
        )
    graph.add_node(
        NodeSpec(node_id="verify.integration", stage=SDLCStage.INTEGRATION_TEST, depends_on=["verify.unit"])
    )
    graph.add_node(
        NodeSpec(
            node_id="verify.gate", stage=SDLCStage.QUALITY_GATE,
            depends_on=["verify.static", "verify.security", "verify.integration"], join_policy="all",
            exit_gate=GateSpec(conditions=[
                GateConditionSpec(type="tests_pass"),
                GateConditionSpec(type="coverage_threshold", params={"floor": 0.80}),
                GateConditionSpec(type="no_high_findings"),
            ]),
        )
    )
    graph.add_node(
        NodeSpec(
            node_id="docs.generate", stage=SDLCStage.DOCUMENTATION, agent="technical_writer", depends_on=["impl.join"]
        )
    )
    graph.add_node(
        NodeSpec(
            node_id="release.readiness", stage=SDLCStage.RELEASE_READINESS, agent="release_manager",
            depends_on=["verify.gate", "docs.generate"], join_policy="all", requires_approval=True,
        )
    )
    graph.add_node(NodeSpec(node_id="summary", stage=SDLCStage.SUMMARY, depends_on=["release.readiness"]))
    graph.validate()
    return graph


expand_impl_tasks = common.expand_impl_tasks


def _intake_executor(run_id: str) -> NodeExecutor:
    async def _executor(node: NodeSpec, view: ContextView) -> NodeExecutionResult:
        artifact = common.make_artifact(
            "raw_requirement", {"text": RAW_REQUIREMENT}, node.node_id, run_id, kind="spec"
        )
        return NodeExecutionResult(artifacts=[artifact])

    return _executor


def _analysis_impact_executor(
    agent: CodebaseAnalystAgent, run_id: str, workspace_root: Path
) -> NodeExecutor:
    """Real AST-based impact analysis (Section 8.2 codebase_analyst):
    builds an index over the actual existing workspace and hands its
    module list to the agent alongside the LLM's own reasoning, so the
    resulting impact_report names modules that genuinely exist."""

    async def _executor(node: NodeSpec, view: ContextView) -> NodeExecutionResult:
        index = AstIndex(workspace_root)
        index.build()
        agent.ast_index = index

        plan = await agent.plan(view)
        agent_result = await agent.execute(view, plan, node_id=node.node_id, run_id=run_id)

        # cross-check: keep only impacted_modules that actually exist in
        # the real index, and record the true blast radius alongside
        # whatever the LLM estimated.
        for artifact in agent_result.artifacts:
            if artifact.name == "impact_report" and isinstance(artifact.payload, dict):
                real_modules = set(index.modules.keys())
                artifact.payload["impacted_modules"] = [
                    m for m in artifact.payload.get("impacted_modules", [])
                    if m.get("path") in real_modules
                ]
                artifact.payload["indexed_module_count"] = len(real_modules)

        return NodeExecutionResult(artifacts=agent_result.artifacts, decisions=agent_result.decisions)

    return _executor


class ChangeControlDenied(Exception):
    pass


def _impl_executor_with_change_control(
    agent: ImplementerAgent, run_id: str, workspace_root: Path
) -> NodeExecutor:
    """Wraps the implementer with a live CHG-001/CHG-002 check (Section
    6.1) — this is what makes "CHG-001 blocks implementation until the
    impact report exists" a runtime behaviour, not documentation."""
    policy = PolicyEngine()

    async def _executor(node: NodeSpec, view: ContextView) -> NodeExecutionResult:
        impact_report_present = "impact_report" in view
        ctx = PolicyContext(
            node=node, hook="before_node_entry", is_brownfield=True,
            impact_report_present=impact_report_present,
        )
        verdicts = policy.evaluate("before_node_entry", ctx)
        deny = [v for v in verdicts if v.verdict.value == "DENY"]
        if deny:
            raise ChangeControlDenied(f"CHG policy denied {node.node_id}: {[v.message for v in deny]}")

        base_executor = make_node_executor(agent, run_id)
        result = await base_executor(node, view)

        files_touched = [a.name.removeprefix("code:") for a in result.artifacts if a.name.startswith("code:")]
        change_ctx = PolicyContext(
            node=node, hook="after_agent_output", is_brownfield=True,
            impact_report_present=impact_report_present, files_touched=files_touched,
        )
        change_verdicts = policy.evaluate("after_agent_output", change_ctx)
        escalations = [v for v in change_verdicts if v.verdict.value == "REQUIRE_APPROVAL"]
        if escalations:
            result.decisions.append(Decision(
                decision_id=str(uuid4()), node_id=node.node_id, agent=agent.name,
                statement=f"Change budget escalation: {[v.message for v in escalations]}",
                rationale="Recorded per CHG-002 for audit; the run proceeds without blocking in this demo profile.",
                created_at=datetime.now(UTC),
            ))

        return result

    return _executor


def build_node_executors(
    provider: LLMProvider,
    prompts_dir: Path,
    workspace_root: Path,
    run_id: str,
    verify_unit_provider: LLMProvider | None = None,
) -> tuple[dict[str, NodeExecutor], dict[str, NodeExecutor]]:
    """`verify_unit_provider`, when given, is used only for test_engineer
    (e.g. wrapped in a FaultInjectingProvider for the rollback demo) —
    every other agent uses `provider` unmodified."""
    fs = JailedFS(workspace_root)

    requirements_agent = RequirementsAgent(provider, prompts_dir)
    ambiguity_agent = AmbiguityAgent(provider, prompts_dir)
    planner_agent = PlannerAgent(provider, prompts_dir)
    codebase_analyst_agent = CodebaseAnalystAgent(provider, prompts_dir)
    api_contract_agent = ApiContractAgent(provider, prompts_dir)
    implementer_agent = ImplementerAgent(provider, prompts_dir, fs=fs)
    test_engineer_agent = TestEngineerAgent(verify_unit_provider or provider, prompts_dir, fs=fs)
    security_reviewer_agent = SecurityReviewerAgent(provider, prompts_dir)
    technical_writer_agent = TechnicalWriterAgent(provider, prompts_dir, fs=fs)
    release_manager_agent = ReleaseManagerAgent(provider, prompts_dir)

    node_executors: dict[str, NodeExecutor] = {
        "intake": _intake_executor(run_id),
        "req.analyze": make_node_executor(requirements_agent, run_id),
        "req.ambiguity": make_node_executor(ambiguity_agent, run_id),
        "plan.decompose": make_node_executor(planner_agent, run_id),
        "analysis.impact": _analysis_impact_executor(codebase_analyst_agent, run_id, workspace_root),
        "design.api": make_node_executor(api_contract_agent, run_id),
        "impl.join": common.checkpoint_executor(run_id, workspace_root, "post-implementation checkpoint"),
        "verify.static": common.verify_static_executor(workspace_root, run_id),
        "verify.security": common.verify_security_executor(security_reviewer_agent, run_id, workspace_root),
        "verify.unit": common.verify_unit_executor(test_engineer_agent, run_id, workspace_root),
        "verify.integration": common.verify_integration_executor(workspace_root, run_id),
        "docs.generate": make_node_executor(technical_writer_agent, run_id),
        "release.readiness": make_node_executor(release_manager_agent, run_id),
        "summary": common.checkpoint_executor(
            run_id, workspace_root, "brownfield: bulk endpoint, verified workspace"
        ),
    }
    executors_by_agent: dict[str, NodeExecutor] = {
        "implementer": _impl_executor_with_change_control(implementer_agent, run_id, workspace_root),
    }
    return node_executors, executors_by_agent


def make_rollback_handler(workspace_root: Path, context: ContextStore) -> object:
    """Reverts the workspace to impl.join's checkpoint — Section 9.3's
    "rollback of the implementation commit". Pass the result as
    Engine(rollback_handlers={"verify.unit": ...}) (or whichever node id
    the fault is injected on) to wire it into recovery."""

    def _handler() -> None:
        checkpoint_artifact = context.get("workspace_checkpoint")
        payload = checkpoint_artifact.payload
        assert isinstance(payload, dict)
        GitCheckpointer(workspace_root).revert_to(payload["commit_sha"])

    return _handler
