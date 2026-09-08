"""Shared workflow machinery (Section 5.1's common node vocabulary):
dynamic impl.<task_id> expansion, artifact construction, and the
tool-backed verify.* / checkpoint executors every scenario's graph
reuses identically. workflows/greenfield.py and workflows/brownfield.py
both build on this rather than duplicating it.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from agentic.agents.security_reviewer import SecurityReviewerAgent
from agentic.agents.test_engineer import TestEngineerAgent
from agentic.core.context import ContextView
from agentic.core.engine import NodeExecutionResult, NodeExecutor
from agentic.core.graph import WorkflowGraph
from agentic.core.models import Artifact, NodeSpec, SDLCStage, compute_content_hash
from agentic.tools.git import GitCheckpointer
from agentic.tools.shell import AllowlistedShell


def make_artifact(name: str, payload: dict, node_id: str, run_id: str, kind: str = "report") -> Artifact:
    return Artifact(
        artifact_id=f"{name}-{node_id}", name=name, kind=kind,  # type: ignore[arg-type]
        content_hash=compute_content_hash(payload), payload=payload,
        produced_by_node=node_id, produced_by_agent="system", run_id=run_id, created_at=datetime.now(UTC),
    )


def expand_impl_tasks(graph: WorkflowGraph, view: ContextView) -> list[str]:
    """Section 5.1: impl.<task_id> nodes, one per task in task_graph,
    all depending on design.review and all patched into impl.join's
    dependency list — the same shape for every scenario."""
    task_graph = view.get("task_graph")
    tasks = task_graph.payload.get("tasks", []) if task_graph and isinstance(task_graph.payload, dict) else []
    new_ids = []
    for task in tasks:
        node_id = f"impl.{task['task_id']}"
        graph.add_node(
            NodeSpec(
                node_id=node_id, stage=SDLCStage.IMPLEMENTATION, agent="implementer",
                depends_on=["design.review"], parallel_group="impl",
            )
        )
        new_ids.append(node_id)
    graph["impl.join"].depends_on = list(new_ids)
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
