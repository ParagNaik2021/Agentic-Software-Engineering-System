"""Greenfield workflow (Section 5.1's canonical graph, Section 11.1).

req.clarify and analysis.impact are omitted from this graph entirely —
per Section 5.1 they are "skipped when ambiguity score < threshold" and
"active only in brownfield" respectively, and the ambiguity agent's
cassette response for this scenario keeps every ambiguity below
threshold, so no clarification branch is ever reachable here.

impl.<task_id> nodes are dynamically expanded from plan.decompose's
task_graph via a GraphExpander (core/engine.py); impl.join is declared
with a placeholder dependency that the same expander patches once the
real task ids exist, before design.review (the first approval gate)
can possibly let anything downstream run — see engine.py's
_expand_graph_if_needed for why this ordering is race-free.
"""

from __future__ import annotations

from pathlib import Path

from agentic.agents.ambiguity import AmbiguityAgent
from agentic.agents.api_contract import ApiContractAgent
from agentic.agents.architect import ArchitectAgent
from agentic.agents.base import make_node_executor
from agentic.agents.data_model import DataModelAgent
from agentic.agents.implementer import ImplementerAgent
from agentic.agents.planner import PlannerAgent
from agentic.agents.release_manager import ReleaseManagerAgent
from agentic.agents.requirements import RequirementsAgent
from agentic.agents.security_reviewer import SecurityReviewerAgent
from agentic.agents.technical_writer import TechnicalWriterAgent
from agentic.agents.test_engineer import TestEngineerAgent
from agentic.core.context import ContextView
from agentic.core.engine import NodeExecutionResult, NodeExecutor
from agentic.core.graph import WorkflowGraph
from agentic.core.models import GateConditionSpec, GateSpec, NodeSpec, SDLCStage
from agentic.llm.provider import LLMProvider
from agentic.tools.fs import JailedFS
from agentic.workflows import common

RAW_REQUIREMENT = (
    "Build a URL shortener service with core APIs, click analytics, and "
    "reliability features suitable for production."
)


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
        NodeSpec(node_id="plan.decompose", stage=SDLCStage.DECOMPOSITION, agent="planner", depends_on=["req.ambiguity"])
    )
    for node_id, agent in (("design.arch", "architect"), ("design.data", "data_model"), ("design.api", "api_contract")):
        graph.add_node(
            NodeSpec(
                node_id=node_id, stage=SDLCStage.ARCHITECTURE, agent=agent,
                depends_on=["plan.decompose"], parallel_group="design",
            )
        )
    graph.add_node(
        NodeSpec(
            node_id="design.review", stage=SDLCStage.DESIGN_REVIEW,
            depends_on=["design.arch", "design.data", "design.api"], join_policy="all",
            requires_approval=True,
        )
    )
    # placeholder depends_on, patched by common.expand_impl_tasks once task_graph exists
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


def build_node_executors(
    provider: LLMProvider, prompts_dir: Path, workspace_root: Path, run_id: str
) -> tuple[dict[str, NodeExecutor], dict[str, NodeExecutor]]:
    """Wires every LLM-backed node to its Agent (via make_node_executor)
    and every tool-backed control node to a bespoke executor. Returns
    (node_executors, executors_by_agent) — the second covers the
    dynamically-expanded impl.<task_id> nodes, all agent="implementer",
    whose exact node_ids don't exist until plan.decompose runs. Nodes
    with no entry anywhere (design.review, verify.gate, summary) fall
    back to the engine's default no-op control executor."""
    fs = JailedFS(workspace_root)

    requirements_agent = RequirementsAgent(provider, prompts_dir)
    ambiguity_agent = AmbiguityAgent(provider, prompts_dir)
    planner_agent = PlannerAgent(provider, prompts_dir)
    architect_agent = ArchitectAgent(provider, prompts_dir)
    data_model_agent = DataModelAgent(provider, prompts_dir)
    api_contract_agent = ApiContractAgent(provider, prompts_dir)
    implementer_agent = ImplementerAgent(provider, prompts_dir, fs=fs)
    test_engineer_agent = TestEngineerAgent(provider, prompts_dir, fs=fs)
    security_reviewer_agent = SecurityReviewerAgent(provider, prompts_dir)
    technical_writer_agent = TechnicalWriterAgent(provider, prompts_dir, fs=fs)
    release_manager_agent = ReleaseManagerAgent(provider, prompts_dir)

    node_executors: dict[str, NodeExecutor] = {
        "intake": _intake_executor(run_id),
        "req.analyze": make_node_executor(requirements_agent, run_id),
        "req.ambiguity": make_node_executor(ambiguity_agent, run_id),
        "plan.decompose": make_node_executor(planner_agent, run_id),
        "design.arch": make_node_executor(architect_agent, run_id),
        "design.data": make_node_executor(data_model_agent, run_id),
        "design.api": make_node_executor(api_contract_agent, run_id),
        "impl.join": common.checkpoint_executor(run_id, workspace_root, "post-implementation checkpoint"),
        "verify.static": common.verify_static_executor(workspace_root, run_id),
        "verify.security": common.verify_security_executor(security_reviewer_agent, run_id, workspace_root),
        "verify.unit": common.verify_unit_executor(test_engineer_agent, run_id, workspace_root),
        "verify.integration": common.verify_integration_executor(workspace_root, run_id),
        "docs.generate": make_node_executor(technical_writer_agent, run_id),
        "release.readiness": make_node_executor(release_manager_agent, run_id),
        "summary": common.checkpoint_executor(
            run_id, workspace_root, "greenfield baseline: complete, verified workspace"
        ),
    }
    executors_by_agent: dict[str, NodeExecutor] = {
        "implementer": make_node_executor(implementer_agent, run_id),
    }
    return node_executors, executors_by_agent
