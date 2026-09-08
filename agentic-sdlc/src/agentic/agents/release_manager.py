"""Release manager agent (Section 8.2, L1_PROPOSE): aggregates gate
results, coverage, findings and open risks into a go/no-go
recommendation for the human approver at release.readiness."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel

from agentic.agents.base import Agent
from agentic.core.context import ContextView
from agentic.core.models import Artifact as CoreArtifact
from agentic.core.models import AutonomyLevel, Decision, compute_content_hash
from agentic.llm.schemas import ReleaseManagerOutput


class ReleaseManagerAgent(Agent):
    name = "release_manager"
    max_autonomy = AutonomyLevel.L1_PROPOSE
    input_contract = []
    output_contract = ["release_readiness_report", "engineering_summary"]
    output_schema = ReleaseManagerOutput
    prompt_template = "release_manager.md"

    def _build_user_message(self, ctx: ContextView, **context: object) -> str:
        summary = {name: artifact.kind for name, artifact in ctx.artifacts.items()}
        return json.dumps({"produced_artifacts": summary})

    def _to_artifacts_and_decisions(
        self, parsed: BaseModel, ctx: ContextView, **context: object
    ) -> tuple[list[CoreArtifact], list[Decision]]:
        assert isinstance(parsed, ReleaseManagerOutput)
        node_id = str(context["node_id"])
        run_id = str(context["run_id"])
        now = datetime.now(UTC)

        def make(name: str, payload: dict) -> CoreArtifact:
            return CoreArtifact(
                artifact_id=str(uuid4()), name=name, kind="report",
                content_hash=compute_content_hash(payload), payload=payload,
                produced_by_node=node_id, produced_by_agent=self.name, run_id=run_id, created_at=now,
            )

        readiness_payload = {
            "go_no_go": parsed.go_no_go, "risks": parsed.risks, "limitations": parsed.limitations,
        }
        summary_payload = {"summary": parsed.summary}
        return [make("release_readiness_report", readiness_payload), make("engineering_summary", summary_payload)], []
