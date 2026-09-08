"""P4 acceptance: metrics computed from a synthetic event log match
hand-calculated expected values, including MTTR with an unrecovered
failure present."""

from datetime import UTC, datetime, timedelta

from agentic.core.events import Actor, Event, EventType
from agentic.core.models import RunState, RunStatus
from agentic.observability.metrics import MetricsCollector, run_success_rate

BASE = datetime(2026, 1, 1, tzinfo=UTC)
SYSTEM = Actor(kind="system", id="engine")
AGENT_ARCH = Actor(kind="agent", id="architect")
AGENT_IMPL = Actor(kind="agent", id="implementer")
HUMAN = Actor(kind="human", id="approver")


def _t(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def _event(seq: int, ts: float, type_: EventType, actor: Actor, node_id: str | None = None,
           payload: dict | None = None) -> Event:
    return Event(
        event_id=f"e{seq}", run_id="run-1", seq=seq, ts=_t(ts), type=type_,
        node_id=node_id, trace_id=None, span_id=None, actor=actor,
        payload=payload or {}, prev_hash="0" * 64, hash="0" * 64,
    )


def _build_synthetic_log() -> list[Event]:
    events = []
    seq = 0

    def add(ts, type_, actor, node_id=None, payload=None):
        nonlocal seq
        events.append(_event(seq, ts, type_, actor, node_id, payload))
        seq += 1

    add(0, EventType.RUN_STARTED, SYSTEM, payload={"scenario": "s", "workflow": "w", "node_ids": ["A", "B", "C"]})

    # Node A: succeeds on the first attempt (0 -> 2s)
    add(0, EventType.NODE_STATE_CHANGED, SYSTEM, "A", {"from": "READY", "to": "RUNNING"})
    add(2, EventType.NODE_STATE_CHANGED, SYSTEM, "A", {"from": "RUNNING", "to": "SUCCEEDED"})
    add(2, EventType.GATE_EVALUATED, SYSTEM, "A", {"condition": "tests_pass", "verdict": "PASS", "phase": "exit"})
    add(2, EventType.LLM_CALL, AGENT_ARCH, "A", {"total_tokens": 100, "cost_usd": 0.01})

    # Node B: fails once (2 -> 3s), retries, succeeds on the second attempt (5 -> 8s)
    # recovered MTTR pair: fail at t=3 -> succeed at t=8 => 5.0s
    add(2, EventType.NODE_STATE_CHANGED, SYSTEM, "B", {"from": "READY", "to": "RUNNING"})
    add(3, EventType.NODE_STATE_CHANGED, SYSTEM, "B", {"from": "RUNNING", "to": "FAILED"})
    add(3, EventType.RETRY_SCHEDULED, SYSTEM, "B", {"attempt": 2})
    add(5, EventType.NODE_STATE_CHANGED, SYSTEM, "B", {"from": "RETRYING", "to": "RUNNING"})
    add(8, EventType.NODE_STATE_CHANGED, SYSTEM, "B", {"from": "RUNNING", "to": "SUCCEEDED"})
    add(8, EventType.GATE_EVALUATED, SYSTEM, "B", {"condition": "tests_pass", "verdict": "PASS", "phase": "exit"})
    add(8, EventType.LLM_CALL, AGENT_IMPL, "B", {"total_tokens": 200, "cost_usd": 0.02})

    # Node C: fails at t=9 and is never recovered (unrecovered failure), then rolled back
    add(8, EventType.NODE_STATE_CHANGED, SYSTEM, "C", {"from": "READY", "to": "RUNNING"})
    add(9, EventType.NODE_STATE_CHANGED, SYSTEM, "C", {"from": "RUNNING", "to": "FAILED"})
    add(9, EventType.GATE_EVALUATED, SYSTEM, "C", {"condition": "coverage_threshold", "verdict": "FAIL", "phase": "exit"})
    add(9.5, EventType.ROLLBACK_STARTED, SYSTEM, "C", {})

    # An autonomy denial on some entry gate evaluation
    add(9.6, EventType.GATE_EVALUATED, SYSTEM, "C", {"condition": "autonomy_permitted", "verdict": "FAIL", "phase": "entry"})

    # An approval requested and granted 5s later
    add(10, EventType.APPROVAL_REQUESTED, SYSTEM, "design", {"input_hash": "h1"})
    add(15, EventType.APPROVAL_GRANTED, HUMAN, "design", {"input_hash": "h1"})

    # Policy evaluations: 1 ALLOW, 1 DENY, 1 REQUIRE_APPROVAL, 1 ALLOW
    add(16, EventType.POLICY_EVALUATED, SYSTEM, "A", {"rule_id": "SEC-001", "category": "security", "verdict": "ALLOW"})
    add(16, EventType.POLICY_EVALUATED, SYSTEM, "A", {"rule_id": "SEC-002", "category": "security", "verdict": "DENY"})
    add(16, EventType.POLICY_EVALUATED, SYSTEM, "design", {"rule_id": "GOV-001", "category": "governance", "verdict": "REQUIRE_APPROVAL"})
    add(16, EventType.POLICY_EVALUATED, SYSTEM, "A", {"rule_id": "CMP-001", "category": "compliance", "verdict": "ALLOW"})

    # A re-plan invalidating 2 downstream nodes
    add(17, EventType.REPLAN_TRIGGERED, SYSTEM, None, {"changed_node": "A", "invalidated": ["X", "Y"]})

    add(20, EventType.RUN_COMPLETED, SYSTEM, payload={})

    return events


def test_node_metrics_and_success_rate() -> None:
    events = _build_synthetic_log()
    snapshot = MetricsCollector().compute(events)

    by_id = {m.node_id: m for m in snapshot.node_metrics}
    assert by_id["A"].attempts == 1
    assert by_id["A"].succeeded_first_attempt is True
    assert by_id["B"].attempts == 2
    assert by_id["B"].succeeded_first_attempt is False
    assert by_id["B"].retries == 1
    assert by_id["C"].attempts == 1
    assert by_id["C"].succeeded_first_attempt is False
    assert by_id["C"].rollbacks == 1

    assert snapshot.node_success_rate == 1 / 3


def test_retry_frequency_and_rollback_count() -> None:
    events = _build_synthetic_log()
    snapshot = MetricsCollector().compute(events)

    assert snapshot.retry_frequency == 1 / 3
    assert snapshot.retries_by_node == {"B": 1}
    assert snapshot.rollback_count == 1
    assert snapshot.rollbacks_by_node == {"C": 1}


def test_mttr_with_unrecovered_failure_present() -> None:
    events = _build_synthetic_log()
    snapshot = MetricsCollector().compute(events)

    assert snapshot.mttr.mean_seconds == 5.0
    assert snapshot.mttr.recovered_count == 1
    assert snapshot.mttr.unrecovered_failures == ["C"]


def test_end_to_end_latency_with_and_without_approval_wait() -> None:
    events = _build_synthetic_log()
    snapshot = MetricsCollector().compute(events)

    assert snapshot.end_to_end_latency_seconds == 20.0
    assert snapshot.approval_wait_seconds == 5.0
    assert snapshot.end_to_end_latency_excluding_approval_seconds == 15.0


def test_approval_latency() -> None:
    events = _build_synthetic_log()
    snapshot = MetricsCollector().compute(events)

    assert len(snapshot.approval_latencies) == 1
    assert snapshot.approval_latencies[0].node_id == "design"
    assert snapshot.approval_latencies[0].seconds == 5.0


def test_gate_rejection_rate() -> None:
    events = _build_synthetic_log()
    snapshot = MetricsCollector().compute(events)

    gm = snapshot.gate_metrics
    assert gm.total == 4
    assert gm.pass_count == 2
    assert gm.fail_count == 2
    assert gm.rejection_rate == 0.5


def test_policy_violation_rate_and_categories() -> None:
    events = _build_synthetic_log()
    snapshot = MetricsCollector().compute(events)

    pm = snapshot.policy_metrics
    assert pm.total == 4
    assert pm.violation_rate == 0.5
    assert pm.by_category["security"] == {"ALLOW": 1, "DENY": 1}
    assert pm.by_category["governance"] == {"REQUIRE_APPROVAL": 1}
    assert pm.by_category["compliance"] == {"ALLOW": 1}


def test_replan_metrics() -> None:
    events = _build_synthetic_log()
    snapshot = MetricsCollector().compute(events)

    assert snapshot.replan_metrics.count == 1
    assert snapshot.replan_metrics.invalidated_per_replan == [2]


def test_autonomy_denials() -> None:
    events = _build_synthetic_log()
    snapshot = MetricsCollector().compute(events)

    assert snapshot.autonomy_denials == 1


def test_token_cost() -> None:
    events = _build_synthetic_log()
    snapshot = MetricsCollector().compute(events)

    tc = snapshot.token_cost
    assert tc.total_tokens == 300
    assert round(tc.total_cost_usd, 4) == 0.03
    assert tc.by_node == {"A": 100, "B": 200}
    assert tc.by_agent == {"architect": 100, "implementer": 200}


def test_output_quality() -> None:
    events = _build_synthetic_log()
    snapshot = MetricsCollector().compute(events)

    oq = snapshot.output_quality
    assert oq.tests_pass_count == 2
    assert oq.tests_fail_count == 0
    assert oq.coverage_pass_count == 0
    assert oq.coverage_fail_count == 1


def test_stage_latency_grouping() -> None:
    events = _build_synthetic_log()
    snapshot = MetricsCollector().compute(events, node_stage={"A": "IMPLEMENTATION", "B": "IMPLEMENTATION", "C": "UNIT_TEST"})

    assert snapshot.stage_latency["IMPLEMENTATION"]["max"] == 3.0  # B's 5->8 window
    assert snapshot.stage_latency["UNIT_TEST"]["max"] == 1.0  # C's 8->9 window


def test_stage_latency_empty_without_mapping() -> None:
    events = _build_synthetic_log()
    snapshot = MetricsCollector().compute(events, node_stage=None)
    assert snapshot.stage_latency == {}


def test_run_success_rate_across_history() -> None:
    states = [
        RunState(run_id="r1", scenario="s", workflow="w", status=RunStatus.SUCCEEDED, created_at=BASE),
        RunState(run_id="r2", scenario="s", workflow="w", status=RunStatus.FAILED, created_at=BASE),
        RunState(run_id="r3", scenario="s", workflow="w", status=RunStatus.SUCCEEDED, created_at=BASE),
    ]
    assert run_success_rate(states) == 2 / 3


def test_run_success_rate_empty_history() -> None:
    assert run_success_rate([]) == 0.0
