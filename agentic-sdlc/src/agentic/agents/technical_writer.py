"""Technical writer agent (Section 8.2, L3_EXECUTE_SCOPED): writes
autonomously, but only under docs/ and README.md — no approval is
sought per action within that scope, but the scope itself is still
enforced in code, not merely by convention.
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
from agentic.llm.schemas import TechnicalWriterOutput
from agentic.tools.fs import JailedFS

_SCOPE = ["docs/", "README.md"]


class TechnicalWriterAgent(Agent):
    name = "technical_writer"
    max_autonomy = AutonomyLevel.L3_EXECUTE_SCOPED
    input_contract = []
    output_contract = []
    output_schema = TechnicalWriterOutput
    prompt_template = "technical_writer.md"

    def __init__(self, provider: LLMProvider, prompts_dir: Path, fs: JailedFS, model: str = "claude-sonnet-5") -> None:
        super().__init__(provider, prompts_dir, model)
        self.fs = fs

    def _build_user_message(self, ctx: ContextView, **context: object) -> str:
        artifact_names = list(ctx.artifacts.keys())
        return json.dumps({"available_artifacts": artifact_names})

    def _to_artifacts_and_decisions(
        self, parsed: BaseModel, ctx: ContextView, **context: object
    ) -> tuple[list[CoreArtifact], list[Decision]]:
        assert isinstance(parsed, TechnicalWriterOutput)
        node_id = str(context["node_id"])
        run_id = str(context["run_id"])
        now = datetime.now(UTC)

        out_of_scope = [d.path for d in parsed.documents if not within_scope(d.path, _SCOPE)]
        if out_of_scope:
            raise FileScopeViolation(f"{self.name} may only write under docs/ or README.md: {out_of_scope}")

        artifacts = []
        for d in parsed.documents:
            self.fs.write_text(d.path, d.content)
            artifacts.append(
                CoreArtifact(
                    artifact_id=str(uuid4()), name=f"doc:{d.path}", kind="doc",
                    content_hash=compute_content_hash(d.content), payload=d.content,
                    produced_by_node=node_id, produced_by_agent=self.name, run_id=run_id, created_at=now,
                )
            )
        return artifacts, []

    async def validate(self, result: AgentResult, ctx: ContextView) -> ValidationReport:
        if not result.artifacts:
            return ValidationReport(ok=False, errors=["technical_writer produced no documents"])
        return ValidationReport(ok=True)
