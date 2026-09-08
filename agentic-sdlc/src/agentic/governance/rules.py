"""The 20-rule policy catalogue (Section 6.1).

Each rule is a pure function PolicyContext -> PolicyVerdict, always
returning a verdict (ALLOW when it does not fire). RULES and RULE_HOOKS
below are the executable registry PolicyEngine dispatches against;
policies/*.yaml carries the same catalogue as data for a human auditor.
test_policy_rules.py asserts the two never drift apart.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from agentic.core.models import PolicyVerdict, PolicyVerdictType, SDLCStage

if TYPE_CHECKING:
    from agentic.governance.policy import PolicyContext, PolicyHook

RuleFn = Callable[["PolicyContext"], PolicyVerdict]


def _v(rule_id: str, category: str, verdict: PolicyVerdictType, message: str = "") -> PolicyVerdict:
    return PolicyVerdict(rule_id=rule_id, category=category, verdict=verdict, message=message)


def _allow(rule_id: str, category: str) -> PolicyVerdict:
    return _v(rule_id, category, PolicyVerdictType.ALLOW)


# ---------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
]


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    length = len(s)
    return -sum(
        (count / length) * math.log2(count / length) for count in Counter(s).values()
    )


def rule_sec_001(ctx: PolicyContext) -> PolicyVerdict:
    for path, content in ctx.file_contents.items():
        for pattern in _SECRET_PATTERNS:
            if pattern.search(content):
                return _v("SEC-001", "security", PolicyVerdictType.DENY,
                           f"possible hardcoded secret in {path}")
        for literal in re.findall(r"['\"]([A-Za-z0-9+/=_-]{20,})['\"]", content):
            if _shannon_entropy(literal) > 4.0:
                return _v("SEC-001", "security", PolicyVerdictType.DENY,
                           f"high-entropy string literal in {path} resembles a secret")
    return _allow("SEC-001", "security")


_DANGEROUS_PATTERNS = [
    re.compile(r"\beval\s*\("),
    re.compile(r"\bexec\s*\("),
    re.compile(r"\bpickle\.loads\s*\("),
    re.compile(r"subprocess\.\w+\([^)]*shell\s*=\s*True"),
    re.compile(r"\bos\.system\s*\("),
]


def rule_sec_002(ctx: PolicyContext) -> PolicyVerdict:
    for path, content in ctx.file_contents.items():
        for pattern in _DANGEROUS_PATTERNS:
            if pattern.search(content):
                return _v("SEC-002", "security", PolicyVerdictType.DENY,
                           f"dangerous construct in {path}: matches /{pattern.pattern}/")
    return _allow("SEC-002", "security")


_URL_PARAM_PATTERN = re.compile(r"(target_url|redirect_url)\s*[:=]")
_OUTBOUND_PATTERN = re.compile(r"(redirect|requests\.|httpx\.)", re.IGNORECASE)
_VALIDATION_CALL_PATTERN = re.compile(
    r"(validate_url|is_safe_url|check_ssrf|validate_target_url)\s*\("
)


def rule_sec_003(ctx: PolicyContext) -> PolicyVerdict:
    for path, content in ctx.file_contents.items():
        if _URL_PARAM_PATTERN.search(content) and _OUTBOUND_PATTERN.search(content):
            if not _VALIDATION_CALL_PATTERN.search(content):
                return _v("SEC-003", "security", PolicyVerdictType.DENY,
                           f"{path} accepts a URL for redirect/outbound request without SSRF validation")
    return _allow("SEC-003", "security")


def rule_sec_004(ctx: PolicyContext) -> PolicyVerdict:
    if ctx.workspace_root is None:
        return _allow("SEC-004", "security")
    root = ctx.workspace_root.resolve()
    for rel in ctx.files_touched:
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return _v("SEC-004", "security", PolicyVerdictType.DENY,
                       f"file write escapes workspace jail: {rel}")
    return _allow("SEC-004", "security")


def rule_sec_005(ctx: PolicyContext) -> PolicyVerdict:
    if not ctx.shell_command:
        return _allow("SEC-005", "security")
    program = Path(ctx.shell_command[0]).name
    if program not in ctx.shell_allowlist:
        return _v("SEC-005", "security", PolicyVerdictType.DENY,
                   f"shell command not in allowlist: {program}")
    return _allow("SEC-005", "security")


def rule_sec_006(ctx: PolicyContext) -> PolicyVerdict:
    if ctx.bandit_high_findings > 0:
        return _v("SEC-006", "security", PolicyVerdictType.DENY,
                   f"{ctx.bandit_high_findings} HIGH-severity bandit finding(s)")
    return _allow("SEC-006", "security")


_INJECTION_MARKERS = (
    "ignore previous instructions", "ignore all previous instructions",
    "disregard all prior", "disregard previous", "system prompt:", "you are now",
)


def rule_sec_007(ctx: PolicyContext) -> PolicyVerdict:
    text = ctx.agent_output_text.lower()
    for marker in _INJECTION_MARKERS:
        if marker in text:
            return _v("SEC-007", "security", PolicyVerdictType.WARN,
                       f"agent output contains a possible prompt-injection marker: '{marker}'")
    return _allow("SEC-007", "security")


# ---------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------

def rule_cmp_001(ctx: PolicyContext) -> PolicyVerdict:
    for artifact in ctx.produced_artifacts:
        if not artifact.run_id or not artifact.produced_by_node or not artifact.produced_by_agent:
            return _v("CMP-001", "compliance", PolicyVerdictType.DENY,
                       f"artifact {artifact.artifact_id} is missing provenance")
    return _allow("CMP-001", "compliance")


_DESIGN_STAGES = {
    SDLCStage.ARCHITECTURE, SDLCStage.DATA_DESIGN, SDLCStage.API_DESIGN, SDLCStage.DESIGN_REVIEW,
}


def rule_cmp_002(ctx: PolicyContext) -> PolicyVerdict:
    if ctx.node.stage not in _DESIGN_STAGES:
        return _allow("CMP-002", "compliance")
    if not ctx.decisions:
        return _v("CMP-002", "compliance", PolicyVerdictType.REQUIRE_APPROVAL,
                   f"design-stage node '{ctx.node.node_id}' has no recorded decision with rationale")
    return _allow("CMP-002", "compliance")


_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_PATTERN = re.compile(r"\b(?:\d[ -]?){13,16}\b")


def rule_cmp_003(ctx: PolicyContext) -> PolicyVerdict:
    for path, content in ctx.file_contents.items():
        if _EMAIL_PATTERN.search(content) or _SSN_PATTERN.search(content) or _CARD_PATTERN.search(content):
            return _v("CMP-003", "compliance", PolicyVerdictType.DENY,
                       f"possible PII pattern in {path}")
    return _allow("CMP-003", "compliance")


def rule_cmp_004(ctx: PolicyContext) -> PolicyVerdict:
    if not ctx.audit_chain_ok:
        return _v("CMP-004", "compliance", PolicyVerdictType.DENY,
                   "event log hash chain is broken")
    return _allow("CMP-004", "compliance")


def rule_cmp_005(ctx: PolicyContext) -> PolicyVerdict:
    new_deps = [d for d in ctx.declared_dependencies if d not in ctx.dependency_allowlist]
    if new_deps:
        return _v("CMP-005", "compliance", PolicyVerdictType.REQUIRE_APPROVAL,
                   f"dependencies not on the allowlist: {new_deps}")
    return _allow("CMP-005", "compliance")


# ---------------------------------------------------------------------
# Change control (+ Governance, sharing this category's YAML file)
# ---------------------------------------------------------------------

def rule_chg_001(ctx: PolicyContext) -> PolicyVerdict:
    if (
        ctx.is_brownfield
        and ctx.node.stage == SDLCStage.IMPLEMENTATION
        and not ctx.impact_report_present
    ):
        return _v("CHG-001", "change_control", PolicyVerdictType.DENY,
                   "brownfield implementation requires a current impact_report artifact")
    return _allow("CHG-001", "change_control")


def rule_chg_002(ctx: PolicyContext) -> PolicyVerdict:
    if len(ctx.files_touched) > ctx.change_budget_files:
        return _v("CHG-002", "change_control", PolicyVerdictType.REQUIRE_APPROVAL,
                   f"{len(ctx.files_touched)} files touched exceeds change budget of "
                   f"{ctx.change_budget_files}")
    return _allow("CHG-002", "change_control")


def rule_chg_003(ctx: PolicyContext) -> PolicyVerdict:
    if ctx.coverage_before is not None and ctx.coverage_after is not None:
        if ctx.coverage_after < ctx.coverage_before:
            return _v("CHG-003", "change_control", PolicyVerdictType.DENY,
                       f"coverage decreased from {ctx.coverage_before:.0%} to "
                       f"{ctx.coverage_after:.0%}")
    return _allow("CHG-003", "change_control")


def rule_chg_004(ctx: PolicyContext) -> PolicyVerdict:
    if ctx.removed_or_renamed_endpoints:
        return _v("CHG-004", "change_control", PolicyVerdictType.REQUIRE_APPROVAL,
                   f"public API contract changes: {ctx.removed_or_renamed_endpoints}")
    return _allow("CHG-004", "change_control")


def rule_chg_005(ctx: PolicyContext) -> PolicyVerdict:
    deleted_tests = [f for f in ctx.deleted_files if "test" in Path(f).name.lower()]
    if deleted_tests:
        return _v("CHG-005", "change_control", PolicyVerdictType.DENY,
                   f"deletion of existing test file(s) prohibited: {deleted_tests}")
    return _allow("CHG-005", "change_control")


def rule_chg_006(ctx: PolicyContext) -> PolicyVerdict:
    if ctx.files_touched and not ctx.in_git_checkpoint:
        return _v("CHG-006", "change_control", PolicyVerdictType.DENY,
                   "workspace mutation occurred outside a git checkpoint window")
    return _allow("CHG-006", "change_control")


_APPROVAL_REQUIRED_STAGES = {SDLCStage.DESIGN_REVIEW, SDLCStage.RELEASE_READINESS}


def rule_gov_001(ctx: PolicyContext) -> PolicyVerdict:
    if ctx.node.stage in _APPROVAL_REQUIRED_STAGES:
        return _v("GOV-001", "governance", PolicyVerdictType.REQUIRE_APPROVAL,
                   f"{ctx.node.stage.value} always requires human approval")
    return _allow("GOV-001", "governance")


def rule_gov_002(ctx: PolicyContext) -> PolicyVerdict:
    if ctx.budget_exhausted:
        return _v("GOV-002", "governance", PolicyVerdictType.DENY,
                   "run token/cost/wall-clock budget exhausted")
    return _allow("GOV-002", "governance")


RULES: dict[str, RuleFn] = {
    "SEC-001": rule_sec_001,
    "SEC-002": rule_sec_002,
    "SEC-003": rule_sec_003,
    "SEC-004": rule_sec_004,
    "SEC-005": rule_sec_005,
    "SEC-006": rule_sec_006,
    "SEC-007": rule_sec_007,
    "CMP-001": rule_cmp_001,
    "CMP-002": rule_cmp_002,
    "CMP-003": rule_cmp_003,
    "CMP-004": rule_cmp_004,
    "CMP-005": rule_cmp_005,
    "CHG-001": rule_chg_001,
    "CHG-002": rule_chg_002,
    "CHG-003": rule_chg_003,
    "CHG-004": rule_chg_004,
    "CHG-005": rule_chg_005,
    "CHG-006": rule_chg_006,
    "GOV-001": rule_gov_001,
    "GOV-002": rule_gov_002,
}

RULE_HOOKS: dict[str, PolicyHook] = {
    "SEC-001": "after_agent_output",
    "SEC-002": "after_agent_output",
    "SEC-003": "after_agent_output",
    "SEC-004": "before_tool_invocation",
    "SEC-005": "before_tool_invocation",
    "SEC-006": "after_agent_output",
    "SEC-007": "after_agent_output",
    "CMP-001": "after_agent_output",
    "CMP-002": "after_agent_output",
    "CMP-003": "after_agent_output",
    "CMP-004": "before_run_completion",
    "CMP-005": "after_agent_output",
    "CHG-001": "before_node_entry",
    "CHG-002": "after_agent_output",
    "CHG-003": "after_agent_output",
    "CHG-004": "after_agent_output",
    "CHG-005": "after_agent_output",
    "CHG-006": "before_tool_invocation",
    "GOV-001": "before_node_entry",
    "GOV-002": "before_node_entry",
}

_CATALOGUE_FILES = ("security.yaml", "compliance.yaml", "change_control.yaml")


def load_rule_catalogue(policies_dir: Path) -> list[dict]:
    """Read the YAML catalogue for `agentic policy list` / audits — the
    same 20 rules described as data, so an auditor need not read code."""
    catalogue: list[dict] = []
    for filename in _CATALOGUE_FILES:
        path = policies_dir / filename
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        catalogue.extend(data.get("rules", []))
    return catalogue
