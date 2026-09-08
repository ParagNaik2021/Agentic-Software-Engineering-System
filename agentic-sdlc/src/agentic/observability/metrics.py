"""Reliability metrics (Section 7.2): all fourteen, computed purely from
an event stream so they are reproducible from a completed run without
instrumenting anything at read time.

Thirteen are per-run (MetricsCollector.compute). The fourteenth, run
success rate, is necessarily cross-run — it is a fleet-level function
(run_success_rate) over RunState history, since one EventLog is scoped
to a single run_id.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, Field

from agentic.core.events import Event, EventType
from agentic.core.models import RunState, RunStatus

_NODE_END_STATUSES = ("SUCCEEDED", "FAILED", "ROLLED_BACK", "HALTED")


class NodeMetric(BaseModel):
    node_id: str
    attempts: int
    succeeded_first_attempt: bool
    retries: int
    rollbacks: int


class MTTRResult(BaseModel):
    mean_seconds: float | None
    recovered_count: int
    unrecovered_failures: list[str] = Field(default_factory=list)


class GateMetric(BaseModel):
    total: int = 0
    pass_count: int = 0
    warn_count: int = 0
    fail_count: int = 0
    rejection_rate: float = 0.0


class PolicyMetric(BaseModel):
    total: int = 0
    by_category: dict[str, dict[str, int]] = Field(default_factory=dict)
    violation_rate: float = 0.0


class ApprovalLatency(BaseModel):
    node_id: str
    seconds: float


class ReplanMetric(BaseModel):
    count: int = 0
    invalidated_per_replan: list[int] = Field(default_factory=list)


class TokenCostMetric(BaseModel):
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    by_node: dict[str, int] = Field(default_factory=dict)
    by_agent: dict[str, int] = Field(default_factory=dict)


class OutputQualityMetric(BaseModel):
    tests_pass_count: int = 0
    tests_fail_count: int = 0
    coverage_pass_count: int = 0
    coverage_fail_count: int = 0
    security_pass_count: int = 0
    security_fail_count: int = 0


class RunMetricsSnapshot(BaseModel):
    node_success_rate: float
    node_metrics: list[NodeMetric] = Field(default_factory=list)
    retry_frequency: float = 0.0
    retries_by_node: dict[str, int] = Field(default_factory=dict)
    rollback_count: int = 0
    rollbacks_by_node: dict[str, int] = Field(default_factory=dict)
    mttr: MTTRResult
    end_to_end_latency_seconds: float | None = None
    end_to_end_latency_excluding_approval_seconds: float | None = None
    approval_wait_seconds: float = 0.0
    stage_latency: dict[str, dict[str, float]] = Field(default_factory=dict)
    gate_metrics: GateMetric = Field(default_factory=GateMetric)
    policy_metrics: PolicyMetric = Field(default_factory=PolicyMetric)
    approval_latencies: list[ApprovalLatency] = Field(default_factory=list)
    replan_metrics: ReplanMetric = Field(default_factory=ReplanMetric)
    autonomy_denials: int = 0
    token_cost: TokenCostMetric = Field(default_factory=TokenCostMetric)
    output_quality: OutputQualityMetric = Field(default_factory=OutputQualityMetric)


@dataclass
class _NodeTrack:
    attempts: int = 0
    first_attempt_succeeded: bool | None = None
    retries: int = 0
    rollbacks: int = 0
    recovered_pairs: list[tuple[datetime, datetime]] = field(default_factory=list)
    last_fail_ts: datetime | None = None


def _track_nodes(events: list[Event]) -> dict[str, _NodeTrack]:
    tracks: dict[str, _NodeTrack] = {}
    for event in events:
        if event.type == EventType.RETRY_SCHEDULED and event.node_id:
            tracks.setdefault(event.node_id, _NodeTrack()).retries += 1
            continue
        if event.type == EventType.ROLLBACK_STARTED and event.node_id:
            tracks.setdefault(event.node_id, _NodeTrack()).rollbacks += 1
            continue
        if event.type != EventType.NODE_STATE_CHANGED or not event.node_id:
            continue
        track = tracks.setdefault(event.node_id, _NodeTrack())
        to = event.payload.get("to")
        if to == "RUNNING":
            track.attempts += 1
        elif to == "SUCCEEDED":
            if track.first_attempt_succeeded is None:
                track.first_attempt_succeeded = track.attempts == 1
            if track.last_fail_ts is not None:
                track.recovered_pairs.append((track.last_fail_ts, event.ts))
                track.last_fail_ts = None
        elif to == "FAILED":
            if track.first_attempt_succeeded is None:
                track.first_attempt_succeeded = False
            track.last_fail_ts = event.ts
    return tracks


def _node_durations(events: list[Event]) -> dict[str, list[float]]:
    starts: dict[str, datetime] = {}
    durations: dict[str, list[float]] = defaultdict(list)
    for event in events:
        if event.type != EventType.NODE_STATE_CHANGED or not event.node_id:
            continue
        to = event.payload.get("to")
        if to == "RUNNING":
            starts[event.node_id] = event.ts
        elif to in _NODE_END_STATUSES and event.node_id in starts:
            durations[event.node_id].append((event.ts - starts.pop(event.node_id)).total_seconds())
    return durations


def compute_node_metrics(tracks: dict[str, _NodeTrack]) -> tuple[float, list[NodeMetric]]:
    metrics = [
        NodeMetric(
            node_id=nid, attempts=t.attempts,
            succeeded_first_attempt=bool(t.first_attempt_succeeded),
            retries=t.retries, rollbacks=t.rollbacks,
        )
        for nid, t in tracks.items()
    ]
    executed = [t for t in tracks.values() if t.attempts > 0]
    rate = (sum(1 for t in executed if t.first_attempt_succeeded) / len(executed)) if executed else 0.0
    return rate, metrics


def compute_mttr(tracks: dict[str, _NodeTrack]) -> MTTRResult:
    durations: list[float] = []
    unrecovered: list[str] = []
    for nid, t in tracks.items():
        durations.extend((succ - fail).total_seconds() for fail, succ in t.recovered_pairs)
        if t.last_fail_ts is not None:
            unrecovered.append(nid)
    mean = sum(durations) / len(durations) if durations else None
    return MTTRResult(mean_seconds=mean, recovered_count=len(durations), unrecovered_failures=sorted(unrecovered))


def compute_approval_latencies(events: list[Event]) -> list[ApprovalLatency]:
    pending: dict[str, datetime] = {}
    results: list[ApprovalLatency] = []
    for event in events:
        if event.type == EventType.APPROVAL_REQUESTED and event.node_id:
            pending[event.node_id] = event.ts
        elif event.type == EventType.APPROVAL_GRANTED and event.node_id and event.node_id in pending:
            requested_ts = pending.pop(event.node_id)
            results.append(
                ApprovalLatency(node_id=event.node_id, seconds=(event.ts - requested_ts).total_seconds())
            )
    return results


def compute_end_to_end_latency(
    events: list[Event],
) -> tuple[float | None, float | None, float]:
    start_ts = end_ts = None
    for event in events:
        if event.type == EventType.RUN_STARTED:
            start_ts = event.ts
        elif event.type == EventType.RUN_COMPLETED:
            end_ts = event.ts
    approval_wait = sum(a.seconds for a in compute_approval_latencies(events))
    if start_ts is None or end_ts is None:
        return None, None, approval_wait
    total = (end_ts - start_ts).total_seconds()
    return total, total - approval_wait, approval_wait


def compute_stage_latency(
    events: list[Event], node_stage: dict[str, str] | None
) -> dict[str, dict[str, float]]:
    if not node_stage:
        return {}
    durations = _node_durations(events)
    by_stage: dict[str, list[float]] = defaultdict(list)
    for nid, times in durations.items():
        stage = node_stage.get(nid)
        if stage:
            by_stage[stage].extend(times)
    return {
        stage: {"p50": statistics.median(times), "max": max(times)}
        for stage, times in by_stage.items()
        if times
    }


def compute_gate_metrics(events: list[Event]) -> GateMetric:
    total = pass_c = warn_c = fail_c = 0
    for event in events:
        if event.type != EventType.GATE_EVALUATED:
            continue
        total += 1
        verdict = event.payload.get("verdict")
        if verdict == "PASS":
            pass_c += 1
        elif verdict == "WARN":
            warn_c += 1
        elif verdict == "FAIL":
            fail_c += 1
    rate = (warn_c + fail_c) / total if total else 0.0
    return GateMetric(total=total, pass_count=pass_c, warn_count=warn_c, fail_count=fail_c, rejection_rate=rate)


def compute_policy_metrics(events: list[Event]) -> PolicyMetric:
    total = 0
    escalations = 0
    by_category: dict[str, dict[str, int]] = {}
    for event in events:
        if event.type != EventType.POLICY_EVALUATED:
            continue
        total += 1
        category = event.payload.get("category", "unknown")
        verdict = event.payload.get("verdict", "ALLOW")
        by_category.setdefault(category, {}).setdefault(verdict, 0)
        by_category[category][verdict] += 1
        if verdict in ("DENY", "REQUIRE_APPROVAL"):
            escalations += 1
    rate = escalations / total if total else 0.0
    return PolicyMetric(total=total, by_category=by_category, violation_rate=rate)


def compute_replan_metrics(events: list[Event]) -> ReplanMetric:
    count = 0
    invalidated: list[int] = []
    for event in events:
        if event.type == EventType.REPLAN_TRIGGERED:
            count += 1
            invalidated.append(len(event.payload.get("invalidated", [])))
    return ReplanMetric(count=count, invalidated_per_replan=invalidated)


def compute_autonomy_denials(events: list[Event]) -> int:
    return sum(
        1 for e in events
        if e.type == EventType.GATE_EVALUATED
        and e.payload.get("condition") == "autonomy_permitted"
        and e.payload.get("verdict") == "FAIL"
    )


def compute_token_cost(events: list[Event]) -> TokenCostMetric:
    total_tokens = 0
    total_cost = 0.0
    by_node: dict[str, int] = defaultdict(int)
    by_agent: dict[str, int] = defaultdict(int)
    for event in events:
        if event.type != EventType.LLM_CALL:
            continue
        tokens = int(event.payload.get("total_tokens", 0))
        cost = float(event.payload.get("cost_usd", 0.0))
        total_tokens += tokens
        total_cost += cost
        if event.node_id:
            by_node[event.node_id] += tokens
        by_agent[event.actor.id] += tokens
    return TokenCostMetric(
        total_tokens=total_tokens, total_cost_usd=total_cost, by_node=dict(by_node), by_agent=dict(by_agent)
    )


_QUALITY_CONDITIONS = {"tests_pass", "coverage_threshold", "no_high_findings"}


def compute_output_quality(events: list[Event]) -> OutputQualityMetric:
    counts = {
        "tests_pass": {"PASS": 0, "FAIL": 0},
        "coverage_threshold": {"PASS": 0, "FAIL": 0},
        "no_high_findings": {"PASS": 0, "FAIL": 0},
    }
    for event in events:
        if event.type != EventType.GATE_EVALUATED:
            continue
        condition = event.payload.get("condition")
        verdict = event.payload.get("verdict")
        if condition in _QUALITY_CONDITIONS and verdict in ("PASS", "FAIL"):
            counts[condition][verdict] += 1
    return OutputQualityMetric(
        tests_pass_count=counts["tests_pass"]["PASS"],
        tests_fail_count=counts["tests_pass"]["FAIL"],
        coverage_pass_count=counts["coverage_threshold"]["PASS"],
        coverage_fail_count=counts["coverage_threshold"]["FAIL"],
        security_pass_count=counts["no_high_findings"]["PASS"],
        security_fail_count=counts["no_high_findings"]["FAIL"],
    )


class MetricsCollector:
    def compute(
        self, events: list[Event], node_stage: dict[str, str] | None = None
    ) -> RunMetricsSnapshot:
        tracks = _track_nodes(events)
        node_success_rate, node_metrics = compute_node_metrics(tracks)
        total_retries = sum(t.retries for t in tracks.values())
        executed = sum(1 for t in tracks.values() if t.attempts > 0)
        retry_frequency = total_retries / executed if executed else 0.0
        rollback_count = sum(t.rollbacks for t in tracks.values())

        total_latency, latency_excl_approval, approval_wait = compute_end_to_end_latency(events)

        return RunMetricsSnapshot(
            node_success_rate=node_success_rate,
            node_metrics=node_metrics,
            retry_frequency=retry_frequency,
            retries_by_node={nid: t.retries for nid, t in tracks.items() if t.retries},
            rollback_count=rollback_count,
            rollbacks_by_node={nid: t.rollbacks for nid, t in tracks.items() if t.rollbacks},
            mttr=compute_mttr(tracks),
            end_to_end_latency_seconds=total_latency,
            end_to_end_latency_excluding_approval_seconds=latency_excl_approval,
            approval_wait_seconds=approval_wait,
            stage_latency=compute_stage_latency(events, node_stage),
            gate_metrics=compute_gate_metrics(events),
            policy_metrics=compute_policy_metrics(events),
            approval_latencies=compute_approval_latencies(events),
            replan_metrics=compute_replan_metrics(events),
            autonomy_denials=compute_autonomy_denials(events),
            token_cost=compute_token_cost(events),
            output_quality=compute_output_quality(events),
        )


def run_success_rate(run_states: list[RunState]) -> float:
    """The 14th, cross-run metric: SUCCEEDED runs / total runs across
    run history. Necessarily cross-run since one EventLog is scoped to a
    single run_id."""
    if not run_states:
        return 0.0
    return sum(1 for s in run_states if s.status == RunStatus.SUCCEEDED) / len(run_states)
