"""Implementer agent (Section 8.2, L2_EXECUTE_GATED): writes code for
exactly one task, only inside that task's declared file_scope. Every
write goes through a JailedFS (workspace jail, SEC-004) *and* an
explicit scope check — the file scope is narrower than the jail and is
what makes "the implementer cannot write outside its task file scope" a
property enforced in code, not just a prompt instruction.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from agentic.agents.base import (
    Agent,
    AgentPlan,
    AgentResult,
    FileScopeViolation,
    ToolCall,
    ValidationReport,
    within_scope,
)
from agentic.core.context import ContextView
from agentic.core.models import Artifact as CoreArtifact
from agentic.core.models import AutonomyLevel, Decision, NodeSpec, compute_content_hash
from agentic.llm.provider import LLMProvider
from agentic.llm.schemas import ImplementerOutput
from agentic.tools.fs import JailedFS


class ImplementerAgent(Agent):
    name = "implementer"
    max_autonomy = AutonomyLevel.L2_EXECUTE_GATED
    input_contract = ["task_graph"]
    output_contract = []  # dynamic: one artifact per file written, checked in validate()
    output_schema = ImplementerOutput
    prompt_template = "implementer.md"

    def __init__(self, provider: LLMProvider, prompts_dir: Path, fs: JailedFS, model: str = "claude-sonnet-5") -> None:
        super().__init__(provider, prompts_dir, model)
        self.fs = fs

    def extra_execute_context(self, node: NodeSpec, view: ContextView) -> dict:
        task_id = node.node_id.removeprefix("impl.")
        file_scope: list[str] = []
        task_graph = view.get("task_graph")
        if task_graph is not None and isinstance(task_graph.payload, dict):
            for task in task_graph.payload.get("tasks", []):
                if task.get("task_id") == task_id:
                    file_scope = task.get("file_scope", [])
                    break
        return {"task_id": task_id, "file_scope": file_scope}

    def _build_user_message(self, ctx: ContextView, **context: object) -> str:
        task_graph = ctx.get("task_graph")
        return json.dumps({
            "task_id": context.get("task_id"),
            "file_scope": context.get("file_scope"),
            "task_graph": task_graph.payload if task_graph else {},
        })

    def _to_artifacts_and_decisions(
        self, parsed: BaseModel, ctx: ContextView, **context: object
    ) -> tuple[list[CoreArtifact], list[Decision]]:
        assert isinstance(parsed, ImplementerOutput)
        node_id = str(context["node_id"])
        run_id = str(context["run_id"])
        file_scope: list[str] = context.get("file_scope") or []  # type: ignore[assignment]
        now = datetime.now(UTC)

        out_of_scope = [f.path for f in parsed.files if not within_scope(f.path, file_scope)]
        if out_of_scope:
            raise FileScopeViolation(
                f"{self.name} attempted to write outside its declared file scope "
                f"{file_scope}: {out_of_scope}"
            )

        artifacts = []
        for f in parsed.files:
            self.fs.write_text(f.path, f.content)
            artifacts.append(
                CoreArtifact(
                    artifact_id=str(uuid4()), name=f"code:{f.path}", kind="code",
                    content_hash=compute_content_hash(f.content), payload=f.content,
                    produced_by_node=node_id, produced_by_agent=self.name, run_id=run_id, created_at=now,
                )
            )

        decisions = []
        if parsed.summary:
            decisions.append(
                Decision(
                    decision_id=str(uuid4()), node_id=node_id, agent=self.name,
                    statement=parsed.summary, rationale=parsed.summary, created_at=now,
                )
            )
        return artifacts, decisions

    async def execute(self, ctx: ContextView, plan: AgentPlan, **context: object) -> AgentResult:
        result = await super().execute(ctx, plan, **context)
        result.tool_calls = [
            ToolCall(tool="fs.write_text", args={"path": a.name.removeprefix("code:")})
            for a in result.artifacts
        ]
        return result

    async def validate(self, result: AgentResult, ctx: ContextView) -> ValidationReport:
        if not result.artifacts:
            return ValidationReport(ok=False, errors=["implementer produced no files"])
        return ValidationReport(ok=True)
