"""Codebase analyst agent (Section 8.2, L0_OBSERVE).

Structurally cannot write: unlike implementer/test_engineer/
technical_writer, this class never receives a JailedFS reference and
its output schema (CodebaseAnalystOutput) has no field that could hold
file content — there is no code path by which this agent could mutate
the workspace even if instructed to.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from agentic.agents.base import Agent
from agentic.core.context import ContextView
from agentic.core.models import Artifact as CoreArtifact
from agentic.core.models import AutonomyLevel, Decision, compute_content_hash
from agentic.llm.provider import LLMProvider
from agentic.llm.schemas import CodebaseAnalystOutput
from agentic.tools.ast_index import AstIndex


class CodebaseAnalystAgent(Agent):
    name = "codebase_analyst"
    max_autonomy = AutonomyLevel.L0_OBSERVE
    input_contract = ["task_graph"]
    output_contract = ["impact_report", "module_map"]
    output_schema = CodebaseAnalystOutput
    prompt_template = "codebase_analyst.md"

    def __init__(
        self,
        provider: LLMProvider,
        prompts_dir: Path,
        ast_index: AstIndex | None = None,
        model: str = "claude-sonnet-5",
    ) -> None:
        super().__init__(provider, prompts_dir, model)
        self.ast_index = ast_index  # read-only; never used to write

    def _build_user_message(self, ctx: ContextView, **context: object) -> str:
        task_graph = ctx.get("task_graph")
        modules = list(self.ast_index.modules.keys()) if self.ast_index else []
        return json.dumps({
            "task_graph": task_graph.payload if task_graph else {},
            "workspace_modules": modules,
        })

    def _to_artifacts_and_decisions(
        self, parsed: BaseModel, ctx: ContextView, **context: object
    ) -> tuple[list[CoreArtifact], list[Decision]]:
        assert isinstance(parsed, CodebaseAnalystOutput)
        node_id = str(context["node_id"])
        run_id = str(context["run_id"])
        now = datetime.now(UTC)

        def make(name: str, payload: dict) -> CoreArtifact:
            return CoreArtifact(
                artifact_id=str(uuid4()), name=name, kind="analysis",
                content_hash=compute_content_hash(payload), payload=payload,
                produced_by_node=node_id, produced_by_agent=self.name, run_id=run_id, created_at=now,
            )

        impact_payload = {
            "impacted_modules": [m.model_dump() for m in parsed.impacted_modules],
            "impacted_endpoints": parsed.impacted_endpoints,
            "migration_required": parsed.migration_required,
            "blast_radius_files": parsed.blast_radius_files,
        }
        module_map_payload = {"modules": [m.path for m in parsed.impacted_modules]}
        return [make("impact_report", impact_payload), make("module_map", module_map_payload)], []
