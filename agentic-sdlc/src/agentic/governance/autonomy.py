"""Autonomy matrix (Section 6.3): every agent has a maximum autonomy
level; every node action has a required level. Entry is denied when the
agent is under-privileged — this is what makes "defined autonomy
boundaries" a runtime-enforced property rather than prose.

L4_AUTONOMOUS is deliberately granted to no agent. That is a design
statement, not an oversight: nothing in this system is trusted to act
without either a gate or a scope restriction.
"""

from __future__ import annotations

from agentic.core.models import AutonomyLevel

AGENT_CEILINGS: dict[str, AutonomyLevel] = {
    # L0 — Observe: read context and workspace only, no writes, no tools.
    "codebase_analyst": AutonomyLevel.L0_OBSERVE,
    "security_reviewer": AutonomyLevel.L0_OBSERVE,
    # L1 — Propose: produce artifacts (specs, designs, plans), no workspace mutation.
    "requirements": AutonomyLevel.L1_PROPOSE,
    "ambiguity": AutonomyLevel.L1_PROPOSE,
    "planner": AutonomyLevel.L1_PROPOSE,
    "architect": AutonomyLevel.L1_PROPOSE,
    "data_model": AutonomyLevel.L1_PROPOSE,
    "api_contract": AutonomyLevel.L1_PROPOSE,
    # release_manager is not in the Section 6.3 table; it only aggregates
    # gate results into a report, so it fits the L1 profile exactly.
    "release_manager": AutonomyLevel.L1_PROPOSE,
    # L2 — Execute (gated): mutate workspace, only inside an approved node
    # and a git checkpoint.
    "implementer": AutonomyLevel.L2_EXECUTE_GATED,
    "test_engineer": AutonomyLevel.L2_EXECUTE_GATED,
    # L3 — Execute (scoped): mutate workspace autonomously within a
    # declared file scope (docs/ only); no approval per action.
    "technical_writer": AutonomyLevel.L3_EXECUTE_SCOPED,
}


class UnknownAgent(Exception):
    pass


class AutonomyViolation(Exception):
    def __init__(self, agent: str, ceiling: AutonomyLevel, required: AutonomyLevel) -> None:
        self.agent = agent
        self.ceiling = ceiling
        self.required = required
        super().__init__(
            f"{agent} autonomy ceiling {ceiling.name} is below required {required.name}"
        )


def ceiling_for(agent: str) -> AutonomyLevel:
    if agent not in AGENT_CEILINGS:
        raise UnknownAgent(f"unknown agent: {agent}")
    return AGENT_CEILINGS[agent]


def is_permitted(agent: str, required: AutonomyLevel) -> bool:
    return ceiling_for(agent) >= required


def enforce(agent: str, required: AutonomyLevel) -> None:
    ceiling = ceiling_for(agent)
    if ceiling < required:
        raise AutonomyViolation(agent, ceiling, required)


def can_use_tools(agent: str) -> bool:
    """L0 agents may read context/workspace directly but may not invoke
    any tool (Section 6.3: "No writes, no tools")."""
    return ceiling_for(agent) >= AutonomyLevel.L1_PROPOSE


def can_mutate_workspace(agent: str) -> bool:
    return ceiling_for(agent) >= AutonomyLevel.L2_EXECUTE_GATED


def requires_per_action_approval(agent: str) -> bool:
    """L2 agents may only mutate inside an approved, checkpointed node;
    L3 agents mutate autonomously within their declared scope."""
    return ceiling_for(agent) == AutonomyLevel.L2_EXECUTE_GATED
