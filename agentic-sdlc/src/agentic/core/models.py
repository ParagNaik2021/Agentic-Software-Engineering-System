"""Core Pydantic data contracts (Section 4.2).

These are the persisted contract of the system: every other module reads
and writes these shapes rather than inventing equivalent ones. Gate and
policy *evaluation logic* lives in governance/observability modules; the
shapes those evaluations produce and consume (GateSpec, GateResult,
PolicyVerdict, ...) live here because NodeSpec and NodeRun need them at
import time and models.py must stay the single shared contract layer.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from agentic.core.states import NodeStatus


class SDLCStage(StrEnum):
    INTAKE = "INTAKE"
    REQUIREMENTS = "REQUIREMENTS"
    CLARIFICATION = "CLARIFICATION"
    DECOMPOSITION = "DECOMPOSITION"
    IMPACT_ANALYSIS = "IMPACT_ANALYSIS"
    ARCHITECTURE = "ARCHITECTURE"
    DATA_DESIGN = "DATA_DESIGN"
    API_DESIGN = "API_DESIGN"
    DESIGN_REVIEW = "DESIGN_REVIEW"
    IMPLEMENTATION = "IMPLEMENTATION"
    STATIC_ANALYSIS = "STATIC_ANALYSIS"
    SECURITY_REVIEW = "SECURITY_REVIEW"
    UNIT_TEST = "UNIT_TEST"
    INTEGRATION_TEST = "INTEGRATION_TEST"
    QUALITY_GATE = "QUALITY_GATE"
    DOCUMENTATION = "DOCUMENTATION"
    RELEASE_READINESS = "RELEASE_READINESS"
    SUMMARY = "SUMMARY"


class AutonomyLevel(IntEnum):
    """Section 6.3. Ordered so `agent.max_autonomy >= node.autonomy_required`
    is a meaningful comparison at the entry gate."""

    L0_OBSERVE = 0
    L1_PROPOSE = 1
    L2_EXECUTE_GATED = 2
    L3_EXECUTE_SCOPED = 3
    L4_AUTONOMOUS = 4


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    HALTED = "HALTED"
    REPLANNING = "REPLANNING"


class GateVerdict(StrEnum):
    PASS_ = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class GateConditionSpec(BaseModel):
    """One named condition inside a GateSpec, e.g. type="tests_pass"."""

    type: str
    params: dict = Field(default_factory=dict)


class GateSpec(BaseModel):
    """An ordered list of conditions; gates.py (P2) implements the thirteen
    condition types from Section 5.2 and aggregates their verdicts."""

    conditions: list[GateConditionSpec] = Field(default_factory=list)


class GateResult(BaseModel):
    node_id: str
    phase: Literal["entry", "exit"]
    condition: str
    verdict: GateVerdict
    message: str = ""


class PolicyVerdictType(StrEnum):
    ALLOW = "ALLOW"
    WARN = "WARN"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class PolicyVerdict(BaseModel):
    rule_id: str
    category: str
    verdict: PolicyVerdictType
    message: str = ""


class RetryPolicy(BaseModel):
    max_attempts: int = 3
    base_seconds: float = 2.0
    cap_seconds: float = 30.0


class FallbackStrategy(StrEnum):
    CHEAPER_MODEL = "CHEAPER_MODEL"
    DECOMPOSE = "DECOMPOSE"
    TEMPLATE = "TEMPLATE"
    WARN_AND_PROCEED = "WARN_AND_PROCEED"


class FallbackSpec(BaseModel):
    strategy: FallbackStrategy
    params: dict = Field(default_factory=dict)


class RollbackSpec(BaseModel):
    enabled: bool = True
    checkpoint_label: str | None = None


class ErrorClass(StrEnum):
    """Section 6.4 error classification driving RecoveryManager strategy."""

    TRANSIENT = "TRANSIENT"
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    QUALITY_FAILURE = "QUALITY_FAILURE"
    POLICY_DENY = "POLICY_DENY"
    CONTRACT_BREACH = "CONTRACT_BREACH"
    SYSTEMIC = "SYSTEMIC"


class ErrorRecord(BaseModel):
    error_class: ErrorClass
    message: str
    detail: str | None = None


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


class RunMetrics(BaseModel):
    """Populated incrementally by the engine; fully computed views over
    the event log are produced by observability/metrics.py in P4."""

    tokens: TokenUsage = Field(default_factory=TokenUsage)
    retries: int = 0
    rollbacks: int = 0
    replans: int = 0


class Artifact(BaseModel):
    artifact_id: str
    name: str
    kind: Literal["spec", "design", "code", "test", "doc", "report", "analysis"]
    content_hash: str
    payload: dict | str
    produced_by_node: str
    produced_by_agent: str
    run_id: str
    created_at: datetime
    version: int = 1
    supersedes: str | None = None


def compute_content_hash(payload: dict | str) -> str:
    """sha256 of the canonical serialisation of an artifact payload."""
    if isinstance(payload, str):
        canonical = payload
    else:
        canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Decision(BaseModel):
    decision_id: str
    node_id: str
    agent: str
    statement: str
    rationale: str
    alternatives: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    created_at: datetime


class NodeSpec(BaseModel):
    node_id: str
    stage: SDLCStage
    agent: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    join_policy: Literal["all", "any", "quorum"] = "all"
    join_quorum: int | None = None  # threshold for join_policy == "quorum" (Section 5.3)
    entry_gate: GateSpec = Field(default_factory=GateSpec)
    exit_gate: GateSpec = Field(default_factory=GateSpec)
    autonomy_required: AutonomyLevel = AutonomyLevel.L1_PROPOSE
    requires_approval: bool = False
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    fallback: FallbackSpec | None = None
    rollback: RollbackSpec | None = None
    optional: bool = False
    parallel_group: str | None = None


class NodeRun(BaseModel):
    node_id: str
    run_id: str
    status: NodeStatus = NodeStatus.PENDING
    attempt: int = 0
    input_hash: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    produced: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    gate_results: list[GateResult] = Field(default_factory=list)
    policy_verdicts: list[PolicyVerdict] = Field(default_factory=list)
    error: ErrorRecord | None = None
    trace_id: str = ""


class RunState(BaseModel):
    run_id: str
    scenario: str
    workflow: str
    status: RunStatus = RunStatus.RUNNING
    nodes: dict[str, NodeRun] = Field(default_factory=dict)
    created_at: datetime
    replan_count: int = 0
    metrics: RunMetrics = Field(default_factory=RunMetrics)
