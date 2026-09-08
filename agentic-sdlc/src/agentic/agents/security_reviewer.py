"""Security reviewer agent (Section 8.2, L0_OBSERVE). Like
codebase_analyst, structurally cannot write: no JailedFS reference, and
SecurityReviewerOutput carries only findings, never file content."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel

from agentic.agents.base import Agent
from agentic.core.context import ContextView
from agentic.core.models import Artifact as CoreArtifact
from agentic.core.models import AutonomyLevel, Decision, compute_content_hash
from agentic.llm.schemas import SecurityReviewerOutput


class SecurityReviewerAgent(Agent):
    name = "security_reviewer"
    max_autonomy = AutonomyLevel.L0_OBSERVE
    input_contract = []
    output_contract = ["security_findings"]
    output_schema = SecurityReviewerOutput
    prompt_template = "security_reviewer.md"

    def _build_user_message(self, ctx: ContextView, **context: object) -> str:
        source_files = [
            {"name": name, "payload": artifact.payload}
            for name, artifact in ctx.artifacts.items()
            if artifact.kind == "code"
        ]
        return json.dumps({"source_files": source_files})

    def _to_artifacts_and_decisions(
        self, parsed: BaseModel, ctx: ContextView, **context: object
    ) -> tuple[list[CoreArtifact], list[Decision]]:
        assert isinstance(parsed, SecurityReviewerOutput)
        node_id = str(context["node_id"])
        run_id = str(context["run_id"])
        payload = {"findings": [f.model_dump() for f in parsed.findings]}
        artifact = CoreArtifact(
            artifact_id=str(uuid4()), name="security_findings", kind="report",
            content_hash=compute_content_hash(payload), payload=payload,
            produced_by_node=node_id, produced_by_agent=self.name, run_id=run_id,
            created_at=datetime.now(UTC),
        )
        return [artifact], []
