"""Ambiguity agent (Section 8.2, L1_PROPOSE)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel

from agentic.agents.base import Agent
from agentic.core.context import ContextView
from agentic.core.models import Artifact as CoreArtifact
from agentic.core.models import AutonomyLevel, Decision, compute_content_hash
from agentic.llm.schemas import AmbiguityOutput


class AmbiguityAgent(Agent):
    name = "ambiguity"
    max_autonomy = AutonomyLevel.L1_PROPOSE
    input_contract = ["ambiguity_register"]
    output_contract = ["ambiguity_assessment", "clarification_questions"]
    output_schema = AmbiguityOutput
    prompt_template = "ambiguity.md"

    def _build_user_message(self, ctx: ContextView, **context: object) -> str:
        register = ctx.get("ambiguity_register")
        return json.dumps({"ambiguity_register": register.payload if register else {}})

    def _to_artifacts_and_decisions(
        self, parsed: BaseModel, ctx: ContextView, **context: object
    ) -> tuple[list[CoreArtifact], list[Decision]]:
        assert isinstance(parsed, AmbiguityOutput)
        node_id = str(context["node_id"])
        run_id = str(context["run_id"])
        now = datetime.now(UTC)

        def make(name: str, payload: dict) -> CoreArtifact:
            return CoreArtifact(
                artifact_id=str(uuid4()), name=name, kind="analysis",
                content_hash=compute_content_hash(payload), payload=payload,
                produced_by_node=node_id, produced_by_agent=self.name, run_id=run_id, created_at=now,
            )

        above = set(parsed.above_threshold_ids)
        artifacts = [
            make(
                "ambiguity_assessment",
                {"scored": [q.model_dump() for q in parsed.scored], "assumptions": parsed.assumptions},
            ),
            make(
                "clarification_questions",
                {"questions": [q.model_dump() for q in parsed.scored if q.id in above]},
            ),
        ]
        return artifacts, []
