"""Requirements agent (Section 8.2, L1_PROPOSE)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from agentic.agents.base import Agent
from agentic.core.context import ContextView
from agentic.core.models import Artifact as CoreArtifact
from agentic.core.models import AutonomyLevel, Decision, compute_content_hash
from agentic.llm.schemas import RequirementsOutput


class RequirementsAgent(Agent):
    name = "requirements"
    max_autonomy = AutonomyLevel.L1_PROPOSE
    input_contract = ["raw_requirement"]
    output_contract = ["normalized_spec", "ambiguity_register", "acceptance_criteria"]
    output_schema = RequirementsOutput
    prompt_template = "requirements.md"

    def _build_user_message(self, ctx: ContextView, **context: object) -> str:
        raw = ctx.get("raw_requirement")
        text = raw.payload if raw is not None else context.get("raw_requirement", "")
        return json.dumps({"raw_requirement": text})

    def _to_artifacts_and_decisions(
        self, parsed: BaseModel, ctx: ContextView, **context: object
    ) -> tuple[list[CoreArtifact], list[Decision]]:
        assert isinstance(parsed, RequirementsOutput)
        node_id = str(context["node_id"])
        run_id = str(context["run_id"])
        now = datetime.now(UTC)

        def make(
            name: str, payload: dict,
            kind: Literal["spec", "design", "code", "test", "doc", "report", "analysis"] = "spec",
        ) -> CoreArtifact:
            return CoreArtifact(
                artifact_id=str(uuid4()), name=name, kind=kind,
                content_hash=compute_content_hash(payload), payload=payload,
                produced_by_node=node_id, produced_by_agent=self.name, run_id=run_id, created_at=now,
            )

        artifacts = [
            make("normalized_spec", {"text": parsed.normalized_spec}),
            make(
                "acceptance_criteria",
                {"items": [c.model_dump() for c in parsed.acceptance_criteria]},
            ),
            make(
                "ambiguity_register",
                {"items": [a.model_dump() for a in parsed.ambiguity_register]},
                kind="analysis",
            ),
        ]
        return artifacts, []
