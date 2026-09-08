"""Test engineer agent (Section 8.2, L2_EXECUTE_GATED): generates unit
and integration tests traced to acceptance_criteria. Scoped to tests/
only, and — per CHG-005 — must never remove an existing test file; this
agent only ever writes, never deletes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from agentic.agents.base import (
    Agent,
    AgentResult,
    FileScopeViolation,
    ValidationReport,
    within_scope,
)
from agentic.core.context import ContextView
from agentic.core.models import Artifact as CoreArtifact
from agentic.core.models import AutonomyLevel, Decision, compute_content_hash
from agentic.llm.provider import LLMProvider
from agentic.llm.schemas import TestEngineerOutput
from agentic.tools.fs import JailedFS

_SCOPE = ["tests/"]


class TestEngineerAgent(Agent):
    name = "test_engineer"
    max_autonomy = AutonomyLevel.L2_EXECUTE_GATED
    input_contract = ["acceptance_criteria"]
    output_contract = []
    output_schema = TestEngineerOutput
    prompt_template = "test_engineer.md"

    def __init__(self, provider: LLMProvider, prompts_dir: Path, fs: JailedFS, model: str = "claude-sonnet-5") -> None:
        super().__init__(provider, prompts_dir, model)
        self.fs = fs

    def _build_user_message(self, ctx: ContextView, **context: object) -> str:
        criteria = ctx.get("acceptance_criteria")
        return json.dumps({"acceptance_criteria": criteria.payload if criteria else {}})

    def _to_artifacts_and_decisions(
        self, parsed: BaseModel, ctx: ContextView, **context: object
    ) -> tuple[list[CoreArtifact], list[Decision]]:
        assert isinstance(parsed, TestEngineerOutput)
        node_id = str(context["node_id"])
        run_id = str(context["run_id"])
        now = datetime.now(UTC)

        out_of_scope = [f.path for f in parsed.test_files if not within_scope(f.path, _SCOPE)]
        if out_of_scope:
            raise FileScopeViolation(f"{self.name} may only write under tests/: {out_of_scope}")

        artifacts = []
        for f in parsed.test_files:
            self.fs.write_text(f.path, f.content)
            artifacts.append(
                CoreArtifact(
                    artifact_id=str(uuid4()), name=f"test:{f.path}", kind="test",
                    content_hash=compute_content_hash(f.content), payload=f.content,
                    produced_by_node=node_id, produced_by_agent=self.name, run_id=run_id, created_at=now,
                )
            )
        return artifacts, []

    async def validate(self, result: AgentResult, ctx: ContextView) -> ValidationReport:
        if not result.artifacts:
            return ValidationReport(ok=False, errors=["test_engineer produced no test files"])
        return ValidationReport(ok=True)
