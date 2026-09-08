"""Span reconstruction from an event stream (Section 7.1)."""

from datetime import UTC, datetime, timedelta

from agentic.core.events import Actor, Event, EventType
from agentic.observability.tracing import Tracer, build_spans_from_events

BASE = datetime(2026, 1, 1, tzinfo=UTC)
SYSTEM = Actor(kind="system", id="engine")
AGENT = Actor(kind="agent", id="architect")


def _t(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def _event(seq, ts, type_, actor, node_id=None, payload=None) -> Event:
    return Event(
        event_id=f"e{seq}", run_id="run-1", seq=seq, ts=_t(ts), type=type_,
        node_id=node_id, trace_id="trace-1", span_id=None, actor=actor,
        payload=payload or {}, prev_hash="0" * 64, hash="0" * 64,
    )


def test_node_span_runs_from_running_to_terminal() -> None:
    events = [
        _event(0, 0, EventType.NODE_STATE_CHANGED, SYSTEM, "A", {"to": "RUNNING"}),
        _event(1, 5, EventType.NODE_STATE_CHANGED, SYSTEM, "A", {"to": "SUCCEEDED"}),
    ]
    spans = build_spans_from_events(events)
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "A"
    assert span.kind == "node"
    assert span.status == "ok"
    assert span.duration_ms == 5000


def test_failed_node_span_has_error_status() -> None:
    events = [
        _event(0, 0, EventType.NODE_STATE_CHANGED, SYSTEM, "A", {"to": "RUNNING"}),
        _event(1, 3, EventType.NODE_STATE_CHANGED, SYSTEM, "A", {"to": "FAILED"}),
    ]
    spans = build_spans_from_events(events)
    assert spans[0].status == "error"


def test_agent_invoked_attaches_agent_attribute() -> None:
    events = [
        _event(0, 0, EventType.NODE_STATE_CHANGED, SYSTEM, "A", {"to": "RUNNING"}),
        _event(1, 1, EventType.AGENT_INVOKED, AGENT, "A", {}),
        _event(2, 5, EventType.NODE_STATE_CHANGED, SYSTEM, "A", {"to": "SUCCEEDED"}),
    ]
    spans = build_spans_from_events(events)
    assert spans[0].attributes["agent"] == "architect"


def test_unterminated_span_is_still_returned_as_open() -> None:
    events = [
        _event(0, 0, EventType.NODE_STATE_CHANGED, SYSTEM, "A", {"to": "RUNNING"}),
    ]
    spans = build_spans_from_events(events)
    assert len(spans) == 1
    assert spans[0].status == "open"
    assert spans[0].end is None
    assert spans[0].duration_ms is None


def test_parallel_nodes_produce_overlapping_spans() -> None:
    events = [
        _event(0, 0, EventType.NODE_STATE_CHANGED, SYSTEM, "A", {"to": "RUNNING"}),
        _event(1, 0, EventType.NODE_STATE_CHANGED, SYSTEM, "B", {"to": "RUNNING"}),
        _event(2, 3, EventType.NODE_STATE_CHANGED, SYSTEM, "A", {"to": "SUCCEEDED"}),
        _event(3, 4, EventType.NODE_STATE_CHANGED, SYSTEM, "B", {"to": "SUCCEEDED"}),
    ]
    spans = {s.name: s for s in build_spans_from_events(events)}
    assert spans["A"].start == spans["B"].start
    assert spans["A"].end != spans["B"].end


def test_llm_call_becomes_a_child_span_of_the_open_node() -> None:
    events = [
        _event(0, 0, EventType.NODE_STATE_CHANGED, SYSTEM, "A", {"to": "RUNNING"}),
        _event(1, 1, EventType.LLM_CALL, AGENT, "A", {"total_tokens": 50, "latency_ms": 200}),
        _event(2, 5, EventType.NODE_STATE_CHANGED, SYSTEM, "A", {"to": "SUCCEEDED"}),
    ]
    spans = build_spans_from_events(events)
    llm_spans = [s for s in spans if s.kind == "llm_call"]
    node_spans = [s for s in spans if s.kind == "node"]
    assert len(llm_spans) == 1
    assert llm_spans[0].parent_span_id == node_spans[0].span_id
    assert llm_spans[0].duration_ms == 200


def test_tracer_nests_spans_via_stack() -> None:
    tracer = Tracer(trace_id="t1")
    node_span = tracer.start_span("A", "node", start=_t(0))
    child_span = tracer.start_span("llm", "llm_call", start=_t(1))
    assert child_span.parent_span_id == node_span.span_id

    tracer.end_span(child_span.span_id, end=_t(2))
    tracer.end_span(node_span.span_id, end=_t(3))

    assert tracer.spans()[0].duration_ms == 3000
