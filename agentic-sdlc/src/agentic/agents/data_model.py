"""Data model agent (Section 8.2, L1_PROPOSE). Privacy/retention
trade-offs (e.g. hashing an IP instead of storing it raw) must be
recorded as decisions — handled the same way as the architect agent."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel

from agentic.agents.base import Agent
from agentic.core.context import ContextView
from agentic.core.models import Artifact as CoreArtifact
from agentic.core.models import AutonomyLevel, Decision, compute_content_hash
from agentic.llm.schemas import DataModelOutput


class DataModelAgent(Agent):
    name = "data_model"
    max_autonomy = AutonomyLevel.L1_PROPOSE
    input_contract = ["normalized_spec"]
    output_contract = ["data_model_design", "migration_plan"]
    output_schema = DataModelOutput
    prompt_template = "data_model.md"

    def _build_user_message(self, ctx: ContextView, **context: object) -> str:
        spec = ctx.get("normalized_spec")
        impact = ctx.get("impact_report")
        return json.dumps({
            "normalized_spec": spec.payload if spec else {},
            "impact_report": impact.payload if impact else {},
        })

    def _to_artifacts_and_decisions(
        self, parsed: BaseModel, ctx: ContextView, **context: object
    ) -> tuple[list[CoreArtifact], list[Decision]]:
        assert isinstance(parsed, DataModelOutput)
        node_id = str(context["node_id"])
        run_id = str(context["run_id"])
        now = datetime.now(UTC)

        def make(name: str, payload: dict) -> CoreArtifact:
            return CoreArtifact(
                artifact_id=str(uuid4()), name=name, kind="design",
                content_hash=compute_content_hash(payload), payload=payload,
                produced_by_node=node_id, produced_by_agent=self.name, run_id=run_id, created_at=now,
            )

        design_payload = {"entities": [e.model_dump() for e in parsed.entities]}
        migration_payload = {"plan": parsed.migration_plan}
        artifacts = [make("data_model_design", design_payload), make("migration_plan", migration_payload)]

        decisions = [
            Decision(
                decision_id=str(uuid4()), node_id=node_id, agent=self.name,
                statement=d.statement, rationale=d.rationale,
                alternatives=d.alternatives_rejected, created_at=now,
            )
            for d in parsed.decisions
        ]
        return artifacts, decisions
