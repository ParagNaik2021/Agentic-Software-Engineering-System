"""P1 acceptance: event log round-trips to identical RunState; tampering
with an event fails verify_chain. Uses a fixed Clock for determinism."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentic.core.events import Actor, EventLog, EventType
from agentic.core.models import RunStatus
from agentic.core.states import NodeStatus


class FixedClock:
    def __init__(self, start: datetime) -> None:
        self._t = start

    def now(self) -> datetime:
        self._t = self._t + timedelta(seconds=1)
        return self._t


SYSTEM = Actor(kind="system", id="engine")
AGENT = Actor(kind="agent", id="implementer")


def _build_sample_log(path: Path) -> EventLog:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    log = EventLog(path=path, run_id="run-1", clock=clock)

    log.append(
        EventType.RUN_STARTED,
        SYSTEM,
        payload={"scenario": "greenfield", "workflow": "greenfield"},
    )
    log.append(
        EventType.NODE_STATE_CHANGED,
        SYSTEM,
        node_id="intake",
        payload={"from": "PENDING", "to": "RUNNING"},
    )
    log.append(
        EventType.NODE_STATE_CHANGED,
        AGENT,
        node_id="intake",
        payload={"from": "RUNNING", "to": "SUCCEEDED"},
    )
    log.append(
        EventType.ARTIFACT_PRODUCED,
        AGENT,
        node_id="intake",
        payload={"artifact_id": "artifact-1"},
    )
    log.append(
        EventType.DECISION_RECORDED,
        AGENT,
        node_id="intake",
        payload={"decision_id": "decision-1"},
    )
    log.append(EventType.RUN_COMPLETED, SYSTEM, payload={})
    return log


def test_event_log_round_trips_to_identical_run_state(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    _build_sample_log(log_path)

    state_a = EventLog(path=log_path, run_id="run-1").replay_to_state()
    # A fresh EventLog instance over the same file (simulating a new process)
    # must replay to an identical projection.
    state_b = EventLog(path=log_path, run_id="run-1").replay_to_state()

    assert state_a == state_b
    assert state_a.status == RunStatus.SUCCEEDED
    assert state_a.nodes["intake"].status == NodeStatus.SUCCEEDED
    assert state_a.nodes["intake"].produced == ["artifact-1"]
    assert state_a.nodes["intake"].decisions == ["decision-1"]


def test_verify_chain_passes_on_untampered_log(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    log = _build_sample_log(log_path)

    result = log.verify_chain()
    assert result.ok is True
    assert result.break_at_seq is None


def test_verify_chain_detects_tampering(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    _build_sample_log(log_path)

    lines = log_path.read_text(encoding="utf-8").splitlines()
    tampered_idx = 2
    record = json.loads(lines[tampered_idx])
    record["payload"] = {"from": "RUNNING", "to": "FAILED"}  # mutate mid-chain event
    lines[tampered_idx] = json.dumps(record)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tampered_log = EventLog(path=log_path, run_id="run-1")
    result = tampered_log.verify_chain()

    assert result.ok is False
    assert result.break_at_seq == tampered_idx
    assert result.reason is not None and "hash mismatch" in result.reason


def test_verify_chain_detects_broken_link_from_deleted_event(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    _build_sample_log(log_path)

    lines = log_path.read_text(encoding="utf-8").splitlines()
    del lines[2]  # remove a mid-chain event entirely, breaking prev_hash linkage
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tampered_log = EventLog(path=log_path, run_id="run-1")
    result = tampered_log.verify_chain()

    assert result.ok is False
    assert result.reason is not None
