"""Structured output schemas, one per agent (Section 8.2 / 9.2). Agents
(P6) reference SCHEMAS[agent_name] rather than importing a class by
name directly, so the registry is the single source of truth the
provider layer, gate conditions and prompt templates all share.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from jinja2 import Template
from pydantic import BaseModel, Field


class AcceptanceCriterion(BaseModel):
    id: str
    statement: str
    testable: bool = True


class AmbiguityCandidate(BaseModel):
    id: str
    description: str
    assumption_if_unresolved: str


class RequirementsOutput(BaseModel):
    normalized_spec: str
    acceptance_criteria: list[AcceptanceCriterion]
    ambiguity_register: list[AmbiguityCandidate]


class ClarificationQuestion(BaseModel):
    id: str
    question: str
    proposed_default: str
    impact: float = Field(ge=0, le=1)
    uncertainty: float = Field(ge=0, le=1)


class AmbiguityOutput(BaseModel):
    scored: list[ClarificationQuestion]
    above_threshold_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class Task(BaseModel):
    task_id: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    file_scope: list[str] = Field(default_factory=list)
    effort: Literal["S", "M", "L"] = "M"
    risk: Literal["low", "medium", "high"] = "low"


class PlannerOutput(BaseModel):
    tasks: list[Task]


class ImpactedModule(BaseModel):
    path: str
    reason: str


class CodebaseAnalystOutput(BaseModel):
    impacted_modules: list[ImpactedModule] = Field(default_factory=list)
    impacted_endpoints: list[str] = Field(default_factory=list)
    migration_required: bool = False
    blast_radius_files: int = 0


class ArchitectureDecision(BaseModel):
    statement: str
    rationale: str
    alternatives_rejected: list[str] = Field(default_factory=list)


class ArchitectOutput(BaseModel):
    components: list[str]
    boundaries: str
    cross_cutting_concerns: list[str] = Field(default_factory=list)
    decisions: list[ArchitectureDecision]


class DataField(BaseModel):
    name: str
    type: str
    constraints: str = ""


class DataEntity(BaseModel):
    name: str
    fields: list[DataField]
    indexes: list[str] = Field(default_factory=list)


class DataModelOutput(BaseModel):
    entities: list[DataEntity]
    migration_plan: str = ""
    decisions: list[ArchitectureDecision] = Field(default_factory=list)


class ApiEndpoint(BaseModel):
    method: str
    path: str
    summary: str
    request_schema: dict = Field(default_factory=dict)
    response_schema: dict = Field(default_factory=dict)


class ApiContractOutput(BaseModel):
    openapi_version: str = "3.1.0"
    endpoints: list[ApiEndpoint]
    examples: dict = Field(default_factory=dict)


class GeneratedFile(BaseModel):
    path: str
    content: str


class ImplementerOutput(BaseModel):
    files: list[GeneratedFile]
    summary: str = ""


class TestEngineerOutput(BaseModel):
    test_files: list[GeneratedFile]
    summary: str = ""


class SecurityFinding(BaseModel):
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    description: str
    location: str = ""


class SecurityReviewerOutput(BaseModel):
    findings: list[SecurityFinding] = Field(default_factory=list)


class TechnicalWriterOutput(BaseModel):
    documents: list[GeneratedFile]


class ReleaseManagerOutput(BaseModel):
    go_no_go: Literal["go", "no-go"]
    summary: str
    risks: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


SCHEMAS: dict[str, type[BaseModel]] = {
    "requirements": RequirementsOutput,
    "ambiguity": AmbiguityOutput,
    "planner": PlannerOutput,
    "codebase_analyst": CodebaseAnalystOutput,
    "architect": ArchitectOutput,
    "data_model": DataModelOutput,
    "api_contract": ApiContractOutput,
    "implementer": ImplementerOutput,
    "test_engineer": TestEngineerOutput,
    "security_reviewer": SecurityReviewerOutput,
    "technical_writer": TechnicalWriterOutput,
    "release_manager": ReleaseManagerOutput,
}


def render_prompt(agent: str, prompts_dir: Path, **context: object) -> str:
    """Load prompts/<agent>.md and inject its schema's JSON schema plus
    any caller-supplied context via Jinja2 ({{ schema_json }} and
    whatever else the template references)."""
    schema = SCHEMAS.get(agent)
    if schema is None:
        raise KeyError(f"no schema registered for agent '{agent}'")
    template_path = Path(prompts_dir) / f"{agent}.md"
    template = Template(template_path.read_text(encoding="utf-8"))
    return template.render(schema_json=_pretty_schema(schema), **context)


def _pretty_schema(schema: type[BaseModel]) -> str:
    return json.dumps(schema.model_json_schema(), indent=2, sort_keys=True)
