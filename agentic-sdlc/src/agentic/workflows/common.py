"""Shared workflow machinery (Section 5.1's common node vocabulary):
the canonical graph itself, dynamic impl.<task_id> expansion, artifact
construction, and the tool-backed verify.* / checkpoint executors every
scenario reuses identically.

build_canonical_graph/build_canonical_node_executors are the single
definition of Section 5.1's graph; greenfield.py and ambiguous.py are
both thin wrappers over them, differing only in their RAW_REQUIREMENT,
their summary label, and whether the clarification branch is present.
One definition is a governance requirement as much as a DRY one: GOV-001
says design acceptance and release readiness always require human
approval, and a scenario that re-declares its own graph can silently omit
those gates -- which is exactly what ambiguous.py did before this was
factored out. brownfield.py still builds its own graph, because its shape
genuinely differs (analysis.impact, a single design.api node).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
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
from agentic.core.models import (
    Artifact,
    GateConditionSpec,
    GateSpec,
    NodeSpec,
    SDLCStage,
    compute_content_hash,
)
from agentic.llm.provider import LLMProvider
from agentic.tools.fs import JailedFS
from agentic.tools.git import GitCheckpointer
from agentic.tools.shell import AllowlistedShell


def make_artifact(name: str, payload: dict, node_id: str, run_id: str, kind: str = "report") -> Artifact:
    return Artifact(
        artifact_id=f"{name}-{node_id}", name=name, kind=kind,  # type: ignore[arg-type]
        content_hash=compute_content_hash(payload), payload=payload,
        produced_by_node=node_id, produced_by_agent="system", run_id=run_id, created_at=datetime.now(UTC),
    )


def build_canonical_graph(*, with_clarification: bool = False) -> WorkflowGraph:
    """Section 5.1's canonical graph, shared by greenfield and ambiguous.

    with_clarification=True adds req.clarify (a CLARIFICATION-stage human
    checkpoint hanging off req.ambiguity) and re-points plan.decompose at
    [req.ambiguity, req.clarify] with join_policy="any" -- so planning and
    the whole design fan-out proceed on the ambiguity agent's proposed
    defaults rather than blocking on a human, and a later answer
    invalidates them through the normal input_hash path. Everything else
    is identical by construction, gates included.

    impl.<task_id> nodes are dynamically expanded from plan.decompose's
    task_graph via expand_impl_tasks below; impl.join is declared with a
    placeholder dependency that the expander patches once the real task
    ids exist, before design.review (the first approval gate) can let
    anything downstream run.
    """
    graph = WorkflowGraph(root="intake")
    graph.add_node(NodeSpec(node_id="intake", stage=SDLCStage.INTAKE))
    graph.add_node(
        NodeSpec(node_id="req.analyze", stage=SDLCStage.REQUIREMENTS, agent="requirements", depends_on=["intake"])
    )
    graph.add_node(
        NodeSpec(node_id="req.ambiguity", stage=SDLCStage.REQUIREMENTS, agent="ambiguity", depends_on=["req.analyze"])
    )

    plan_depends_on = ["req.ambiguity"]
    plan_join_policy = "all"
    if with_clarification:
        graph.add_node(
            NodeSpec(
                node_id="req.clarify", stage=SDLCStage.CLARIFICATION, depends_on=["req.ambiguity"],
                requires_approval=True,
            )
        )
        plan_depends_on = ["req.ambiguity", "req.clarify"]
        plan_join_policy = "any"

    graph.add_node(
        NodeSpec(
            node_id="plan.decompose", stage=SDLCStage.DECOMPOSITION, agent="planner",
            depends_on=plan_depends_on, join_policy=plan_join_policy,
        )
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
    # placeholder depends_on, patched by expand_impl_tasks once task_graph exists
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


def intake_executor(run_id: str, requirement_text: str) -> NodeExecutor:
    """The intake node differs between scenarios only by the
    requirement text it seeds the run with."""

    async def _executor(node: NodeSpec, view: ContextView) -> NodeExecutionResult:
        artifact = make_artifact(
            "raw_requirement", {"text": requirement_text}, node.node_id, run_id, kind="spec"
        )
        return NodeExecutionResult(artifacts=[artifact])

    return _executor


def expand_impl_tasks(graph: WorkflowGraph, view: ContextView) -> list[str]:
    """Section 5.1: impl.<task_id> nodes, one per task in task_graph,
    all depending on design.review and all patched into impl.join's
    dependency list — the same shape for every scenario.

    Idempotent, because a re-plan re-runs plan.decompose and therefore
    runs this expander a second time: a task_id that already has a node
    keeps it (re-adding would raise GraphValidationError, and the node's
    own invalidation is the engine's job, not the expander's). impl.join's
    dependencies are recomputed from the current task list either way, so
    a second pass that drops or adds a task is reflected correctly.

    Only newly created node_ids are returned, since that is what the
    engine seeds into RunState.
    """
    task_graph = view.get("task_graph")
    tasks = task_graph.payload.get("tasks", []) if task_graph and isinstance(task_graph.payload, dict) else []
    new_ids: list[str] = []
    task_node_ids: list[str] = []
    for task in tasks:
        node_id = f"impl.{task['task_id']}"
        task_node_ids.append(node_id)
        if node_id in graph:
            continue
        graph.add_node(
            NodeSpec(
                node_id=node_id, stage=SDLCStage.IMPLEMENTATION, agent="implementer",
                depends_on=["design.review"], parallel_group="impl",
            )
        )
        new_ids.append(node_id)
    graph["impl.join"].depends_on = list(task_node_ids)
    return new_ids


def checkpoint_executor(run_id: str, workspace_root: Path, label: str) -> NodeExecutor:
    """A git checkpoint with no agent of its own — used at impl.join
    (post-implementation) and summary (final, complete baseline)."""

    async def _executor(node: NodeSpec, view: ContextView) -> NodeExecutionResult:
        checkpointer = GitCheckpointer(workspace_root)
        sha = checkpointer.checkpoint(label)
        artifact = make_artifact("workspace_checkpoint", {"commit_sha": sha}, node.node_id, run_id)
        return NodeExecutionResult(artifacts=[artifact])

    return _executor


_BANDIT_SEVERITY_MAP = {"HIGH": "HIGH", "MEDIUM": "MEDIUM", "LOW": "LOW"}


def run_bandit(shell: AllowlistedShell) -> list[dict]:
    result = shell.run(["bandit", "-r", "app", "-f", "json"], timeout=60.0)
    if not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    findings = []
    for item in data.get("results", []):
        severity = _BANDIT_SEVERITY_MAP.get(item.get("issue_severity", "LOW"), "LOW")
        findings.append({
            "severity": severity, "description": item.get("issue_text", ""),
            "location": f"{item.get('filename', '')}:{item.get('line_number', '')}",
        })
    return findings


def verify_security_executor(agent: SecurityReviewerAgent, run_id: str, workspace_root: Path) -> NodeExecutor:
    async def _executor(node: NodeSpec, view: ContextView) -> NodeExecutionResult:
        shell = AllowlistedShell(workspace_root)
        bandit_findings = run_bandit(shell)

        plan = await agent.plan(view)
        agent_result = await agent.execute(view, plan, node_id=node.node_id, run_id=run_id)
        llm_findings: list[dict] = []
        for artifact in agent_result.artifacts:
            if artifact.name == "security_findings" and isinstance(artifact.payload, dict):
                llm_findings.extend(artifact.payload.get("findings", []))

        combined = {"findings": bandit_findings + llm_findings}
        artifact = make_artifact("security_findings", combined, node.node_id, run_id)
        return NodeExecutionResult(artifacts=[artifact], decisions=agent_result.decisions)

    return _executor


_COVERAGE_TOTAL_RE = re.compile(r"^TOTAL\s+\d+\s+\d+(?:\s+\d+\s+\d+)?\s+(\d+)%", re.MULTILINE)


def run_tests_with_coverage(shell: AllowlistedShell) -> dict:
    shell.run(
        ["python", "-m", "coverage", "run", "--branch", "--source=app", "-m", "pytest", "-q"],
        timeout=120.0,
    )
    test_run = shell.run(["python", "-m", "pytest", "-q"], timeout=120.0)
    report = shell.run(["python", "-m", "coverage", "report"], timeout=60.0)
    match = _COVERAGE_TOTAL_RE.search(report.stdout)
    coverage = int(match.group(1)) / 100 if match else 0.0
    return {"exit_code": test_run.returncode, "coverage": coverage, "stdout": test_run.stdout[-4000:]}


def verify_unit_executor(agent: TestEngineerAgent, run_id: str, workspace_root: Path) -> NodeExecutor:
    async def _executor(node: NodeSpec, view: ContextView) -> NodeExecutionResult:
        plan = await agent.plan(view)
        agent_result = await agent.execute(view, plan, node_id=node.node_id, run_id=run_id)

        shell = AllowlistedShell(workspace_root)
        test_results_payload = run_tests_with_coverage(shell)
        test_results_artifact = make_artifact("test_results", test_results_payload, node.node_id, run_id)

        return NodeExecutionResult(
            artifacts=[*agent_result.artifacts, test_results_artifact], decisions=agent_result.decisions
        )

    return _executor


def verify_static_executor(workspace_root: Path, run_id: str) -> NodeExecutor:
    async def _executor(node: NodeSpec, view: ContextView) -> NodeExecutionResult:
        shell = AllowlistedShell(workspace_root)
        ruff_result = shell.run(["ruff", "check", "app"], timeout=60.0)
        mypy_result = shell.run(
            ["mypy", "app", "--ignore-missing-imports", "--explicit-package-bases"], timeout=60.0
        )
        payload = {
            "ruff_exit_code": ruff_result.returncode, "mypy_exit_code": mypy_result.returncode,
            "ruff_output": ruff_result.stdout[-2000:], "mypy_output": mypy_result.stdout[-2000:],
        }
        artifact = make_artifact("lint_report", payload, node.node_id, run_id)
        return NodeExecutionResult(artifacts=[artifact])

    return _executor


def verify_integration_executor(workspace_root: Path, run_id: str) -> NodeExecutor:
    async def _executor(node: NodeSpec, view: ContextView) -> NodeExecutionResult:
        shell = AllowlistedShell(workspace_root)
        result = shell.run(["pytest", "-q"], timeout=120.0)
        payload = {"exit_code": result.returncode, "stdout": result.stdout[-2000:]}
        artifact = make_artifact("integration_test_results", payload, node.node_id, run_id)
        return NodeExecutionResult(artifacts=[artifact])

    return _executor


def build_canonical_node_executors(
    provider: LLMProvider,
    prompts_dir: Path,
    workspace_root: Path,
    run_id: str,
    requirement_text: str,
    summary_label: str,
) -> tuple[dict[str, NodeExecutor], dict[str, NodeExecutor]]:
    """Wires every node of build_canonical_graph() to its agent or
    tool-backed executor. Returns (node_executors, executors_by_agent) --
    the second covers the dynamically-expanded impl.<task_id> nodes, all
    agent="implementer", whose node_ids don't exist until plan.decompose
    runs. Nodes with no entry anywhere (design.review, verify.gate, and
    req.clarify unless a caller supplies one) fall back to the engine's
    default no-op control executor.

    Shared by greenfield and ambiguous so the two cannot drift: the only
    scenario-specific inputs are the requirement text and the summary
    checkpoint label.
    """
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
        "intake": intake_executor(run_id, requirement_text),
        "req.analyze": make_node_executor(requirements_agent, run_id),
        "req.ambiguity": make_node_executor(ambiguity_agent, run_id),
        "plan.decompose": make_node_executor(planner_agent, run_id),
        "design.arch": make_node_executor(architect_agent, run_id),
        "design.data": make_node_executor(data_model_agent, run_id),
        "design.api": make_node_executor(api_contract_agent, run_id),
        "impl.join": checkpoint_executor(run_id, workspace_root, "post-implementation checkpoint"),
        "verify.static": verify_static_executor(workspace_root, run_id),
        "verify.security": verify_security_executor(security_reviewer_agent, run_id, workspace_root),
        "verify.unit": verify_unit_executor(test_engineer_agent, run_id, workspace_root),
        "verify.integration": verify_integration_executor(workspace_root, run_id),
        "docs.generate": make_node_executor(technical_writer_agent, run_id),
        "release.readiness": make_node_executor(release_manager_agent, run_id),
        "summary": checkpoint_executor(run_id, workspace_root, summary_label),
    }
    executors_by_agent: dict[str, NodeExecutor] = {
        "implementer": make_node_executor(implementer_agent, run_id),
    }
    return node_executors, executors_by_agent
