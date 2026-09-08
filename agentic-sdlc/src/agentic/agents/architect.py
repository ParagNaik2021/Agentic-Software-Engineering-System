"""Architect agent (Section 8.2, L1_PROPOSE). Must record alternatives
considered and rejected — enforced structurally here by always emitting
one Decision per parsed ArchitectureDecision, satisfying CMP-002."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel

from agentic.agents.base import Agent
from agentic.core.context import ContextView
from agentic.core.models import Artifact as CoreArtifact
from agentic.core.models import AutonomyLevel, Decision, compute_content_hash
from agentic.llm.schemas import ArchitectOutput


class ArchitectAgent(Agent):
    name = "architect"
    max_autonomy = AutonomyLevel.L1_PROPOSE
    input_contract = ["normalized_spec", "task_graph"]
    output_contract = ["architecture_design", "adr_records"]
    output_schema = ArchitectOutput
    prompt_template = "architect.md"

    def _build_user_message(self, ctx: ContextView, **context: object) -> str:
        spec = ctx.get("normalized_spec")
        tasks = ctx.get("task_graph")
        impact = ctx.get("impact_report")
        return json.dumps({
            "normalized_spec": spec.payload if spec else {},
            "task_graph": tasks.payload if tasks else {},
            "impact_report": impact.payload if impact else {},
        })

    def _to_artifacts_and_decisions(
        self, parsed: BaseModel, ctx: ContextView, **context: object
    ) -> tuple[list[CoreArtifact], list[Decision]]:
        assert isinstance(parsed, ArchitectOutput)
        node_id = str(context["node_id"])
        run_id = str(context["run_id"])
        now = datetime.now(UTC)

        def make(name: str, payload: dict) -> CoreArtifact:
            return CoreArtifact(
                artifact_id=str(uuid4()), name=name, kind="design",
                content_hash=compute_content_hash(payload), payload=payload,
                produced_by_node=node_id, produced_by_agent=self.name, run_id=run_id, created_at=now,
            )

        design_payload = {
            "components": parsed.components, "boundaries": parsed.boundaries,
            "cross_cutting_concerns": parsed.cross_cutting_concerns,
        }
        adr_payload = {"decisions": [d.model_dump() for d in parsed.decisions]}
        artifacts = [make("architecture_design", design_payload), make("adr_records", adr_payload)]

        decisions = [
            Decision(
                decision_id=str(uuid4()), node_id=node_id, agent=self.name,
                statement=d.statement, rationale=d.rationale,
                alternatives=d.alternatives_rejected, created_at=now,
            )
            for d in parsed.decisions
        ]
        return artifacts, decisions
