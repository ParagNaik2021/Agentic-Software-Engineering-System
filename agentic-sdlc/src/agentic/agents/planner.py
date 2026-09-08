"""Planner agent (Section 8.2, L1_PROPOSE). Also invoked during
re-plan (core/replan.py's reshape step, once wired) to reshape the
task graph."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel

from agentic.agents.base import Agent
from agentic.core.context import ContextView
from agentic.core.models import Artifact as CoreArtifact
from agentic.core.models import AutonomyLevel, Decision, compute_content_hash
from agentic.llm.schemas import PlannerOutput


class PlannerAgent(Agent):
    name = "planner"
    max_autonomy = AutonomyLevel.L1_PROPOSE
    input_contract = ["normalized_spec"]
    output_contract = ["task_graph"]
    output_schema = PlannerOutput
    prompt_template = "planner.md"

    def _build_user_message(self, ctx: ContextView, **context: object) -> str:
        spec = ctx.get("normalized_spec")
        clarifications = ctx.get("clarification_questions")
        answer = ctx.get("clarification_answer")
        return json.dumps({
            "normalized_spec": spec.payload if spec else {},
            "clarifications": clarifications.payload if clarifications else {},
            "clarification_answer": answer.payload if answer else None,
        })

    def _to_artifacts_and_decisions(
        self, parsed: BaseModel, ctx: ContextView, **context: object
    ) -> tuple[list[CoreArtifact], list[Decision]]:
        assert isinstance(parsed, PlannerOutput)
        node_id = str(context["node_id"])
        run_id = str(context["run_id"])
        payload = {"tasks": [t.model_dump() for t in parsed.tasks]}
        artifact = CoreArtifact(
            artifact_id=str(uuid4()), name="task_graph", kind="spec",
            content_hash=compute_content_hash(payload), payload=payload,
            produced_by_node=node_id, produced_by_agent=self.name, run_id=run_id,
            created_at=datetime.now(UTC),
        )
        return [artifact], []
