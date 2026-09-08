"""One schema and one prompt template per agent (Section 8.2 / 9.2)."""

from pathlib import Path

from agentic.llm.schemas import SCHEMAS, render_prompt

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "src" / "agentic" / "llm" / "prompts"

_EXPECTED_AGENTS = {
    "requirements", "ambiguity", "planner", "codebase_analyst", "architect", "data_model",
    "api_contract", "implementer", "test_engineer", "security_reviewer", "technical_writer",
    "release_manager",
}


def test_all_twelve_agents_have_exactly_one_schema() -> None:
    assert set(SCHEMAS.keys()) == _EXPECTED_AGENTS
    assert len(SCHEMAS) == 12


def test_every_agent_has_a_prompt_template_file() -> None:
    for agent in _EXPECTED_AGENTS:
        path = PROMPTS_DIR / f"{agent}.md"
        assert path.exists(), f"missing prompt template for {agent}"


def test_render_prompt_injects_schema_json() -> None:
    rendered = render_prompt("requirements", PROMPTS_DIR)
    assert "normalized_spec" in rendered  # a field name from RequirementsOutput's schema
    assert "{{ schema_json }}" not in rendered  # placeholder was actually substituted


def test_render_prompt_unknown_agent_raises() -> None:
    import pytest

    with pytest.raises(KeyError):
        render_prompt("not_a_real_agent", PROMPTS_DIR)


def test_every_schema_round_trips_a_minimal_instance() -> None:
    """Each schema must be constructible and re-parseable from its own
    JSON — this is exactly the path ValidatingProvider exercises."""
    samples = {
        "requirements": {
            "normalized_spec": "s", "acceptance_criteria": [{"id": "AC-1", "statement": "s"}],
            "ambiguity_register": [],
        },
        "ambiguity": {"scored": [], "above_threshold_ids": [], "assumptions": []},
        "planner": {"tasks": [{"task_id": "T1", "description": "d"}]},
        "codebase_analyst": {"impacted_modules": []},
        "architect": {"components": ["api"], "boundaries": "b", "decisions": []},
        "data_model": {"entities": []},
        "api_contract": {"endpoints": []},
        "implementer": {"files": []},
        "test_engineer": {"test_files": []},
        "security_reviewer": {"findings": []},
        "technical_writer": {"documents": []},
        "release_manager": {"go_no_go": "go", "summary": "s"},
    }
    for agent, payload in samples.items():
        schema = SCHEMAS[agent]
        instance = schema.model_validate(payload)
        round_tripped = schema.model_validate_json(instance.model_dump_json())
        assert round_tripped == instance
