"""API contract agent (Section 8.2, L1_PROPOSE): OpenAPI 3.1 with
request/response schemas, error envelopes, status codes and idempotency
semantics."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel

from agentic.agents.base import Agent
from agentic.core.context import ContextView
from agentic.core.models import Artifact as CoreArtifact
from agentic.core.models import AutonomyLevel, Decision, compute_content_hash
from agentic.llm.schemas import ApiContractOutput


class ApiContractAgent(Agent):
    name = "api_contract"
    max_autonomy = AutonomyLevel.L1_PROPOSE
    input_contract = ["normalized_spec", "architecture_design"]
    output_contract = ["openapi_schema", "api_examples"]
    output_schema = ApiContractOutput
    prompt_template = "api_contract.md"

    def _build_user_message(self, ctx: ContextView, **context: object) -> str:
        spec = ctx.get("normalized_spec")
        design = ctx.get("architecture_design")
        message: dict[str, object] = {
            "normalized_spec": spec.payload if spec else {},
            "architecture_design": design.payload if design else {},
        }
        # See architect.py's identical handling: present only after a
        # human's `--from-rejection` recovery, omitted (not null) otherwise
        # so a pre-existing cassette recorded before this field existed
        # still replay-hits.
        feedback = ctx.get("design.review_rejection_feedback")
        if feedback is not None:
            message["rejection_feedback"] = feedback.payload
        return json.dumps(message)

    def _to_artifacts_and_decisions(
        self, parsed: BaseModel, ctx: ContextView, **context: object
    ) -> tuple[list[CoreArtifact], list[Decision]]:
        assert isinstance(parsed, ApiContractOutput)
        node_id = str(context["node_id"])
        run_id = str(context["run_id"])
        now = datetime.now(UTC)

        def make(name: str, payload: dict) -> CoreArtifact:
            return CoreArtifact(
                artifact_id=str(uuid4()), name=name, kind="design",
                content_hash=compute_content_hash(payload), payload=payload,
                produced_by_node=node_id, produced_by_agent=self.name, run_id=run_id, created_at=now,
            )

        schema_payload = {
            "openapi_version": parsed.openapi_version,
            "endpoints": [e.model_dump() for e in parsed.endpoints],
        }
        examples_payload = {"examples": parsed.examples}
        return [make("openapi_schema", schema_payload), make("api_examples", examples_payload)], []
