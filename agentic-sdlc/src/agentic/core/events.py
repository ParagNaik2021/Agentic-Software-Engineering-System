"""Append-only, hash-chained event log (Section 4.4).

The event log is the source of truth: RunState is a projection rebuilt
by replaying events (replay_to_state). Each event carries the hash of
its predecessor, so verify_chain can detect tampering anywhere in the
history — this is what makes the log audit-grade rather than merely a
log.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

from agentic.core.models import (
    GateResult,
    NodeRun,
    PolicyVerdict,
    RunMetrics,
    RunState,
    RunStatus,
)
from agentic.core.states import NodeStatus

GENESIS_HASH = "0" * 64


class EventType(StrEnum):
    RUN_STARTED = "RUN_STARTED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_HALTED = "RUN_HALTED"
    NODE_STATE_CHANGED = "NODE_STATE_CHANGED"
    GATE_EVALUATED = "GATE_EVALUATED"
    POLICY_EVALUATED = "POLICY_EVALUATED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    ARTIFACT_PRODUCED = "ARTIFACT_PRODUCED"
    ARTIFACT_SUPERSEDED = "ARTIFACT_SUPERSEDED"
    DECISION_RECORDED = "DECISION_RECORDED"
    AGENT_INVOKED = "AGENT_INVOKED"
    LLM_CALL = "LLM_CALL"
    TOOL_INVOKED = "TOOL_INVOKED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    FALLBACK_ENGAGED = "FALLBACK_ENGAGED"
    ROLLBACK_STARTED = "ROLLBACK_STARTED"
    ROLLBACK_COMPLETED = "ROLLBACK_COMPLETED"
    REPLAN_TRIGGERED = "REPLAN_TRIGGERED"
    NODE_INVALIDATED = "NODE_INVALIDATED"
    SAFE_STOP_ENGAGED = "SAFE_STOP_ENGAGED"


class Actor(BaseModel):
    kind: Literal["agent", "human", "system"]
    id: str


class Event(BaseModel):
    event_id: str
    run_id: str
    seq: int
    ts: datetime
    type: EventType
    node_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    actor: Actor
    payload: dict = Field(default_factory=dict)
    prev_hash: str
    hash: str


class ChainVerification(BaseModel):
    ok: bool
    break_at_seq: int | None = None
    reason: str | None = None


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def _prehash_payload(
    event_id: str,
    run_id: str,
    seq: int,
    ts_iso: str,
    type_value: str,
    node_id: str | None,
    trace_id: str | None,
    span_id: str | None,
    actor: dict,
    payload: dict,
    prev_hash: str,
) -> dict:
    return {
        "event_id": event_id,
        "run_id": run_id,
        "seq": seq,
        "ts": ts_iso,
        "type": type_value,
        "node_id": node_id,
        "trace_id": trace_id,
        "span_id": span_id,
        "actor": actor,
        "payload": payload,
        "prev_hash": prev_hash,
    }


def _compute_hash(prehash: dict) -> str:
    canonical = json.dumps(prehash, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EventLog:
    def __init__(self, path: Path, run_id: str, clock: Clock | None = None) -> None:
        self.path = path
        self.run_id = run_id
        self.clock: Clock = clock or SystemClock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: list[Event] | None = None

    def _last(self) -> Event | None:
        events = self.read()
        return events[-1] if events else None

    def append(
        self,
        type: EventType,
        actor: Actor,
        payload: dict | None = None,
        node_id: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> Event:
        last = self._last()
        seq = last.seq + 1 if last else 0
        prev_hash = last.hash if last else GENESIS_HASH
        ts_iso = self.clock.now().isoformat()
        event_id = str(uuid4())
        actor_dict = actor.model_dump()
        payload = payload or {}

        prehash = _prehash_payload(
            event_id, self.run_id, seq, ts_iso, type.value,
            node_id, trace_id, span_id, actor_dict, payload, prev_hash,
        )
        event_hash = _compute_hash(prehash)
        record = {**prehash, "hash": event_hash}

        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        self._cache = None
        return Event.model_validate(record)

    def read(self) -> list[Event]:
        if self._cache is not None:
            return self._cache
        if not self.path.exists():
            self._cache = []
            return self._cache
        events: list[Event] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                events.append(Event.model_validate(json.loads(line)))
        self._cache = events
        return events

    def verify_chain(self) -> ChainVerification:
        events = self.read()
        expected_prev = GENESIS_HASH
        for event in events:
            if event.prev_hash != expected_prev:
                return ChainVerification(
                    ok=False, break_at_seq=event.seq, reason="prev_hash mismatch"
                )
            prehash = _prehash_payload(
                event.event_id, event.run_id, event.seq, event.ts.isoformat(),
                event.type.value, event.node_id, event.trace_id, event.span_id,
                event.actor.model_dump(), event.payload, event.prev_hash,
            )
            recomputed = _compute_hash(prehash)
            if recomputed != event.hash:
                return ChainVerification(
                    ok=False,
                    break_at_seq=event.seq,
                    reason="hash mismatch (event content tampered)",
                )
            expected_prev = event.hash
        return ChainVerification(ok=True)

    def replay_to_state(self) -> RunState:
        """Fold the event stream into a RunState projection."""
        events = self.read()
        state: RunState | None = None

        for event in events:
            if event.type == EventType.RUN_STARTED:
                node_ids: list[str] = event.payload.get("node_ids", [])
                state = RunState(
                    run_id=event.run_id,
                    scenario=event.payload.get("scenario", ""),
                    workflow=event.payload.get("workflow", ""),
                    status=RunStatus.RUNNING,
                    nodes={
                        nid: NodeRun(node_id=nid, run_id=event.run_id, status=NodeStatus.PENDING)
                        for nid in node_ids
                    },
                    created_at=event.ts,
                    replan_count=0,
                    metrics=RunMetrics(),
                )
                continue
            if state is None:
                continue  # events preceding RUN_STARTED are not expected

            if event.type == EventType.NODE_STATE_CHANGED and event.node_id:
                nid = event.node_id
                to_status = NodeStatus(event.payload["to"])
                node = state.nodes.get(nid)
                if node is None:
                    node = NodeRun(
                        node_id=nid,
                        run_id=state.run_id,
                        status=to_status,
                        trace_id=event.trace_id or "",
                    )
                else:
                    node.status = to_status
                if "attempt" in event.payload:
                    node.attempt = event.payload["attempt"]
                if "input_hash" in event.payload:
                    node.input_hash = event.payload["input_hash"]
                if to_status == NodeStatus.RUNNING and node.started_at is None:
                    node.started_at = event.ts
                if to_status in (
                    NodeStatus.SUCCEEDED,
                    NodeStatus.FAILED,
                    NodeStatus.ROLLED_BACK,
                    NodeStatus.HALTED,
                ):
                    node.ended_at = event.ts
                    if node.started_at is not None:
                        node.duration_ms = int(
                            (node.ended_at - node.started_at).total_seconds() * 1000
                        )
                state.nodes[nid] = node

            elif event.type == EventType.ARTIFACT_PRODUCED and event.node_id:
                node = state.nodes.get(event.node_id)
                if node is not None:
                    node.produced.append(event.payload.get("artifact_id", ""))

            elif event.type == EventType.DECISION_RECORDED and event.node_id:
                node = state.nodes.get(event.node_id)
                if node is not None:
                    node.decisions.append(event.payload.get("decision_id", ""))

            elif event.type == EventType.GATE_EVALUATED and event.node_id:
                node = state.nodes.get(event.node_id)
                if node is not None:
                    node.gate_results.append(GateResult.model_validate(event.payload))

            elif event.type == EventType.POLICY_EVALUATED and event.node_id:
                node = state.nodes.get(event.node_id)
                if node is not None:
                    node.policy_verdicts.append(PolicyVerdict.model_validate(event.payload))

            elif event.type == EventType.REPLAN_TRIGGERED:
                state.replan_count += 1
                state.metrics.replans += 1

            elif event.type == EventType.RETRY_SCHEDULED:
                state.metrics.retries += 1

            elif event.type == EventType.ROLLBACK_STARTED:
                state.metrics.rollbacks += 1

            elif event.type == EventType.RUN_COMPLETED:
                state.status = RunStatus.SUCCEEDED

            elif event.type in (EventType.RUN_HALTED, EventType.SAFE_STOP_ENGAGED):
                state.status = RunStatus.HALTED

            elif event.type == EventType.APPROVAL_REQUESTED:
                state.status = RunStatus.AWAITING_APPROVAL

            elif event.type == EventType.APPROVAL_GRANTED:
                if state.status == RunStatus.AWAITING_APPROVAL:
                    state.status = RunStatus.RUNNING

        if state is None:
            raise ValueError("event log contains no RUN_STARTED event")
        return state
