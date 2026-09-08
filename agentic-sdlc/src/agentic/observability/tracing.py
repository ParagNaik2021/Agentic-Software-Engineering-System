"""Tracing (Section 7.1): each run gets a trace_id, each node execution a
span_id, with LLM calls and tool invocations as child spans. Spans are
reconstructed from the event log rather than held in a live registry, so
the same code path renders a Gantt-style timeline for a run in progress
or one replayed long after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from agentic.core.events import Event, EventType

SpanKind = Literal["node", "llm_call", "tool_call"]
SpanStatus = Literal["ok", "error", "open"]

_NODE_END_STATUSES = ("SUCCEEDED", "FAILED", "ROLLED_BACK", "HALTED")


def _millis(n: float) -> timedelta:
    return timedelta(milliseconds=n)


@dataclass
class Span:
    span_id: str
    trace_id: str
    name: str
    kind: SpanKind
    start: datetime
    parent_span_id: str | None = None
    end: datetime | None = None
    status: SpanStatus = "open"
    attributes: dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> int | None:
        if self.end is None:
            return None
        return int((self.end - self.start).total_seconds() * 1000)


class Tracer:
    """Live span registry for a single run, used by callers that want to
    start/end spans as they happen (e.g. a future LLM provider wrapping
    each call) rather than reconstructing them after the fact."""

    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        self._spans: dict[str, Span] = {}
        self._stack: list[str] = []

    def start_span(
        self,
        name: str,
        kind: SpanKind,
        start: datetime,
        parent_span_id: str | None = None,
        attributes: dict | None = None,
    ) -> Span:
        span_id = str(uuid4())
        parent = parent_span_id if parent_span_id is not None else (
            self._stack[-1] if self._stack else None
        )
        span = Span(
            span_id=span_id, trace_id=self.trace_id, name=name, kind=kind,
            start=start, parent_span_id=parent, attributes=attributes or {},
        )
        self._spans[span_id] = span
        self._stack.append(span_id)
        return span

    def end_span(self, span_id: str, end: datetime, status: SpanStatus = "ok") -> Span:
        span = self._spans[span_id]
        span.end = end
        span.status = status
        if self._stack and self._stack[-1] == span_id:
            self._stack.pop()
        return span

    def spans(self) -> list[Span]:
        return list(self._spans.values())


def build_spans_from_events(events: list[Event]) -> list[Span]:
    """Reconstruct node/llm_call/tool_call spans from an event log. A node
    span runs from its RUNNING transition to its next terminal
    transition; AGENT_INVOKED attaches the agent name; LLM_CALL and
    TOOL_INVOKED become child point-in-time spans (their own payload
    carries duration where available)."""
    open_node_spans: dict[str, Span] = {}
    completed: list[Span] = []

    for event in events:
        trace_id = event.trace_id or ""

        if event.type == EventType.NODE_STATE_CHANGED and event.node_id:
            to = event.payload.get("to")
            if to == "RUNNING":
                open_node_spans[event.node_id] = Span(
                    span_id=f"node:{event.node_id}:{event.seq}", trace_id=trace_id,
                    name=event.node_id, kind="node", start=event.ts, status="open",
                )
            elif to in _NODE_END_STATUSES and event.node_id in open_node_spans:
                span = open_node_spans.pop(event.node_id)
                span.end = event.ts
                span.status = "ok" if to == "SUCCEEDED" else "error"
                completed.append(span)

        elif event.type == EventType.AGENT_INVOKED and event.node_id:
            parent = open_node_spans.get(event.node_id)
            if parent is not None:
                parent.attributes["agent"] = event.actor.id

        elif event.type in (EventType.LLM_CALL, EventType.TOOL_INVOKED) and event.node_id:
            # Recorded once, after completion, so start == end unless the
            # payload carries its own duration/latency (added here so
            # duration_ms reflects the call's actual length, not zero).
            parent = open_node_spans.get(event.node_id)
            kind: SpanKind = "llm_call" if event.type == EventType.LLM_CALL else "tool_call"
            duration_ms = event.payload.get("duration_ms") or event.payload.get("latency_ms") or 0
            start = event.ts - _millis(duration_ms)
            completed.append(
                Span(
                    span_id=f"{kind}:{event.event_id}", trace_id=trace_id,
                    name=event.payload.get("name", kind), kind=kind, start=start, end=event.ts,
                    status="ok", attributes=dict(event.payload),
                    parent_span_id=parent.span_id if parent else None,
                )
            )

    completed.extend(open_node_spans.values())  # unterminated (e.g. run halted mid-node)
    return completed
