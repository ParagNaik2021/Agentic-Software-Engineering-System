"""PolicyEngine (Section 6.1): evaluates rules at four hook points and
aggregates their verdicts to the worst-of ALLOW/WARN/REQUIRE_APPROVAL/DENY.

Rule *logic* lives in rules.py; the YAML files under policies/ describe
the same 20-rule catalogue for a human auditor who should not need to
read Python to know what governs the system (a consistency test checks
the two never drift apart). This module only owns hook dispatch and
verdict aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from agentic.core.models import Artifact, Decision, NodeSpec, PolicyVerdict, PolicyVerdictType
from agentic.governance.rules import RULE_HOOKS, RULES

PolicyHook = Literal[
    "before_node_entry", "before_tool_invocation", "after_agent_output", "before_run_completion"
]

_VERDICT_RANK = {
    PolicyVerdictType.ALLOW: 0,
    PolicyVerdictType.WARN: 1,
    PolicyVerdictType.REQUIRE_APPROVAL: 2,
    PolicyVerdictType.DENY: 3,
}


@dataclass
class PolicyContext:
    """Everything a rule might need. Fields default to values that
    represent "nothing to flag" so a rule not yet wired to real data
    (bandit output, coverage reports, ...) degrades to ALLOW rather than
    blocking on data that doesn't exist until later phases populate it."""

    node: NodeSpec
    hook: PolicyHook
    files_touched: list[str] = field(default_factory=list)
    file_contents: dict[str, str] = field(default_factory=dict)
    deleted_files: list[str] = field(default_factory=list)
    workspace_root: Path | None = None
    shell_command: list[str] | None = None
    shell_allowlist: tuple[str, ...] = ("pytest", "ruff", "mypy", "bandit", "git", "python")
    produced_artifacts: list[Artifact] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    declared_dependencies: list[str] = field(default_factory=list)
    dependency_allowlist: set[str] = field(default_factory=set)
    is_brownfield: bool = False
    impact_report_present: bool = False
    coverage_before: float | None = None
    coverage_after: float | None = None
    removed_or_renamed_endpoints: list[str] = field(default_factory=list)
    in_git_checkpoint: bool = True
    audit_chain_ok: bool = True
    bandit_high_findings: int = 0
    agent_output_text: str = ""
    budget_exhausted: bool = False
    change_budget_files: int = 10


def aggregate(verdicts: list[PolicyVerdict]) -> PolicyVerdictType:
    if not verdicts:
        return PolicyVerdictType.ALLOW
    return max((v.verdict for v in verdicts), key=lambda v: _VERDICT_RANK[v])


class PolicyEngine:
    def __init__(self) -> None:
        self.rules = RULES
        self.rule_hooks = RULE_HOOKS

    def evaluate(self, hook: PolicyHook, ctx: PolicyContext) -> list[PolicyVerdict]:
        """Every rule assigned to this hook runs; each always returns a
        verdict (ALLOW when it does not fire) so the full evaluation is
        recorded, not just violations — Section 6.1: "ALLOW: Proceed.
        Recorded but not surfaced.\""""
        results = []
        for rule_id, fn in self.rules.items():
            if self.rule_hooks.get(rule_id) != hook:
                continue
            results.append(fn(ctx))
        return results
