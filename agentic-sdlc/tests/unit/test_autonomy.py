"""Autonomy matrix (Section 6.3): ceilings enforced, L4 granted to no one."""

import pytest

from agentic.core.models import AutonomyLevel
from agentic.governance.autonomy import (
    AGENT_CEILINGS,
    AutonomyViolation,
    UnknownAgent,
    can_mutate_workspace,
    can_use_tools,
    ceiling_for,
    enforce,
    is_permitted,
)


def test_all_twelve_agents_have_a_ceiling() -> None:
    expected_agents = {
        "requirements", "ambiguity", "planner", "architect", "data_model", "api_contract",
        "codebase_analyst", "implementer", "test_engineer", "security_reviewer",
        "technical_writer", "release_manager",
    }
    assert expected_agents.issubset(AGENT_CEILINGS.keys())


def test_no_agent_is_granted_l4_autonomous() -> None:
    assert AutonomyLevel.L4_AUTONOMOUS not in AGENT_CEILINGS.values()


def test_l0_agent_cannot_use_tools() -> None:
    assert can_use_tools("codebase_analyst") is False
    assert can_use_tools("security_reviewer") is False


def test_l1_agent_can_use_tools_but_not_mutate_workspace() -> None:
    assert can_use_tools("architect") is True
    assert can_mutate_workspace("architect") is False


def test_l2_agent_can_mutate_workspace() -> None:
    assert can_mutate_workspace("implementer") is True
    assert can_mutate_workspace("test_engineer") is True


def test_is_permitted_true_when_ceiling_meets_requirement() -> None:
    assert is_permitted("implementer", AutonomyLevel.L2_EXECUTE_GATED) is True
    assert is_permitted("implementer", AutonomyLevel.L1_PROPOSE) is True  # ceiling exceeds requirement


def test_is_permitted_false_when_ceiling_below_requirement() -> None:
    assert is_permitted("codebase_analyst", AutonomyLevel.L1_PROPOSE) is False


def test_enforce_raises_autonomy_violation_when_under_privileged() -> None:
    with pytest.raises(AutonomyViolation):
        enforce("codebase_analyst", AutonomyLevel.L2_EXECUTE_GATED)


def test_enforce_passes_silently_when_sufficiently_privileged() -> None:
    enforce("implementer", AutonomyLevel.L2_EXECUTE_GATED)  # must not raise


def test_ceiling_for_unknown_agent_raises() -> None:
    with pytest.raises(UnknownAgent):
        ceiling_for("some_agent_that_does_not_exist")
