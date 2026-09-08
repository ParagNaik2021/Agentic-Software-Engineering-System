"""P1: RunStore save/load round-trip and event-log-driven resume."""

from datetime import UTC, datetime
from pathlib import Path

from agentic.core.events import Actor, EventLog, EventType
from agentic.core.models import RunState, RunStatus
from agentic.core.store import RunStore

SYSTEM = Actor(kind="system", id="engine")


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "state.db")
    state = RunState(
        run_id="run-1",
        scenario="greenfield",
        workflow="greenfield",
        status=RunStatus.RUNNING,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    store.save(state)
    loaded = store.load("run-1")

    assert loaded == state


def test_load_missing_run_returns_none(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "state.db")
    assert store.load("does-not-exist") is None


def test_save_overwrites_existing_state(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "state.db")
    state = RunState(
        run_id="run-1",
        scenario="greenfield",
        workflow="greenfield",
        status=RunStatus.RUNNING,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    store.save(state)

    state.status = RunStatus.SUCCEEDED
    store.save(state)

    loaded = store.load("run-1")
    assert loaded is not None
    assert loaded.status == RunStatus.SUCCEEDED


def test_resume_rebuilds_from_event_log_in_a_fresh_process(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    log = EventLog(path=events_path, run_id="run-1")
    log.append(
        EventType.RUN_STARTED, SYSTEM, payload={"scenario": "greenfield", "workflow": "greenfield"}
    )
    log.append(
        EventType.NODE_STATE_CHANGED,
        SYSTEM,
        node_id="intake",
        payload={"from": "PENDING", "to": "RUNNING"},
    )

    # Simulate a fresh process: new EventLog and RunStore objects over the same files.
    fresh_log = EventLog(path=events_path, run_id="run-1")
    store = RunStore(tmp_path / "state.db")

    resumed = store.resume("run-1", fresh_log)

    assert resumed.status == RunStatus.RUNNING
    assert resumed.nodes["intake"].status.value == "RUNNING"
    assert store.load("run-1") == resumed
