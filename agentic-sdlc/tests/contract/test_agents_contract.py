"""P6 acceptance: every agent passes its contract test against golden
fixtures (Section 12's Contract testing layer)."""

import json
from pathlib import Path

import pytest

from agentic.agents.registry import AGENT_REGISTRY
from agentic.core.context import ContextView
from agentic.llm.mock import MockProvider, ScriptedResponse
from agentic.tools.fs import JailedFS

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "src" / "agentic" / "llm" / "prompts"

DYNAMIC_OUTPUT_AGENTS = {"implementer", "test_engineer", "technical_writer"}

GOLDEN: dict[str, dict] = {
    "requirements": {
        "normalized_spec": "A service that shortens URLs and records click analytics.",
        "acceptance_criteria": [{"id": "AC-1", "statement": "Creating a link returns a short code.", "testable": True}],
        "ambiguity_register": [{"id": "AMB-1", "description": "No expiry policy.", "assumption_if_unresolved": "No default expiry."}],
    },
    "ambiguity": {
        "scored": [{"id": "AMB-1", "question": "Should links expire?", "proposed_default": "No.", "impact": 0.4, "uncertainty": 0.6}],
        "above_threshold_ids": [],
        "assumptions": ["No default expiry."],
    },
    "planner": {
        "tasks": [{"task_id": "T1", "description": "Link model", "depends_on": [], "file_scope": ["src/app/models/link.py"], "effort": "M", "risk": "low"}],
    },
    "codebase_analyst": {
        "impacted_modules": [{"path": "src/app/services/link_service.py", "reason": "alias validation"}],
        "impacted_endpoints": ["POST /api/v1/links"],
        "migration_required": False,
        "blast_radius_files": 3,
    },
    "architect": {
        "components": ["api", "services", "repositories"],
        "boundaries": "Routers depend only on services.",
        "cross_cutting_concerns": ["structured logging"],
        "decisions": [{"statement": "Layered architecture.", "rationale": "Testability.", "alternatives_rejected": ["Active Record"]}],
    },
    "data_model": {
        "entities": [{"name": "Link", "fields": [{"name": "id", "type": "uuid"}], "indexes": []}],
        "migration_plan": "New table.",
        "decisions": [{"statement": "Hash IPs.", "rationale": "Privacy.", "alternatives_rejected": ["store raw IP"]}],
    },
    "api_contract": {
        "openapi_version": "3.1.0",
        "endpoints": [{"method": "POST", "path": "/api/v1/links", "summary": "Create a link.", "request_schema": {}, "response_schema": {}}],
        "examples": {},
    },
    "implementer": {
        "files": [{"path": "src/app/models/link.py", "content": "class Link:\n    pass\n"}],
        "summary": "Added the Link model.",
    },
    "test_engineer": {
        "test_files": [{"path": "tests/unit/test_link.py", "content": "def test_link():\n    assert True\n"}],
        "summary": "Added a unit test for Link.",
    },
    "security_reviewer": {
        "findings": [{"severity": "LOW", "description": "Minor logging verbosity.", "location": "src/app/api/links.py:10"}],
    },
    "technical_writer": {
        "documents": [{"path": "docs/adr/0001-layered-architecture.md", "content": "# ADR 1\n\nLayered architecture.\n"}],
    },
    "release_manager": {
        "go_no_go": "go",
        "summary": "All gates passed.",
        "risks": [],
        "limitations": ["No auth beyond an API key placeholder."],
    },
}

EXTRA_EXECUTE_KWARGS: dict[str, dict] = {
    "implementer": {"task_id": "T1", "file_scope": ["src/app/models/link.py"]},
}


def _make_agent(name: str, tmp_path: Path):
    cls = AGENT_REGISTRY[name]
    provider = MockProvider(default=ScriptedResponse(content=json.dumps(GOLDEN[name])))
    if name == "codebase_analyst":
        return cls(provider, PROMPTS_DIR, ast_index=None)
    if name in DYNAMIC_OUTPUT_AGENTS:
        return cls(provider, PROMPTS_DIR, fs=JailedFS(tmp_path))
    return cls(provider, PROMPTS_DIR)


def test_golden_fixtures_cover_every_registered_agent() -> None:
    assert set(GOLDEN.keys()) == set(AGENT_REGISTRY.keys())


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", sorted(AGENT_REGISTRY.keys()))
async def test_agent_contract_against_golden_fixture(agent_name: str, tmp_path: Path) -> None:
    agent = _make_agent(agent_name, tmp_path)
    ctx = ContextView()

    plan = await agent.plan(ctx)
    assert plan.steps

    kwargs = {"node_id": f"node.{agent_name}", "run_id": "run-1", **EXTRA_EXECUTE_KWARGS.get(agent_name, {})}
    result = await agent.execute(ctx, plan, **kwargs)

    assert result.artifacts, f"{agent_name} produced no artifacts from its golden fixture"

    report = await agent.validate(result, ctx)
    assert report.ok, report.errors

    if agent.output_contract:
        produced_names = {a.name for a in result.artifacts}
        assert set(agent.output_contract).issubset(produced_names)

    for artifact in result.artifacts:
        assert artifact.produced_by_agent == agent_name
        assert artifact.run_id == "run-1"
