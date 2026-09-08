"""Audit report rendering wraps EventLog.verify_chain()."""

import json
from pathlib import Path

from agentic.core.events import Actor, EventLog, EventType
from agentic.observability.audit import render_audit_report, verify_run_audit

SYSTEM = Actor(kind="system", id="engine")


def test_audit_report_ok_on_untampered_log(tmp_path: Path) -> None:
    log = EventLog(path=tmp_path / "events.jsonl", run_id="run-1")
    log.append(EventType.RUN_STARTED, SYSTEM, payload={"scenario": "s", "workflow": "w"})
    log.append(EventType.RUN_COMPLETED, SYSTEM, payload={})

    report = verify_run_audit("run-1", log)

    assert report.ok is True
    assert report.total_events == 2
    assert "OK" in render_audit_report(report)


def test_audit_report_fails_on_tampered_log(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    log = EventLog(path=log_path, run_id="run-1")
    log.append(EventType.RUN_STARTED, SYSTEM, payload={"scenario": "s", "workflow": "w"})
    log.append(EventType.RUN_COMPLETED, SYSTEM, payload={})

    lines = log_path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["payload"] = {"scenario": "tampered", "workflow": "w"}
    lines[0] = json.dumps(record)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tampered_log = EventLog(path=log_path, run_id="run-1")
    report = verify_run_audit("run-1", tampered_log)

    assert report.ok is False
    assert report.break_at_seq == 0
    rendered = render_audit_report(report)
    assert "FAILED" in rendered
    assert "run-1" in rendered
