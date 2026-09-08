"""Audit reporting (Sections 4.4 / 7.3): a thin, human-readable layer
over EventLog.verify_chain() — the hash-chain verification itself is
event-log machinery (core/events.py); this module renders the result for
`agentic verify-audit <run_id>` and for the run report.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentic.core.events import EventLog


@dataclass
class AuditReport:
    run_id: str
    ok: bool
    total_events: int
    break_at_seq: int | None
    reason: str | None


def verify_run_audit(run_id: str, event_log: EventLog) -> AuditReport:
    result = event_log.verify_chain()
    events = event_log.read()
    return AuditReport(
        run_id=run_id, ok=result.ok, total_events=len(events),
        break_at_seq=result.break_at_seq, reason=result.reason,
    )


def render_audit_report(report: AuditReport) -> str:
    if report.ok:
        return f"OK: {report.total_events} events verified for run {report.run_id}; hash chain intact."
    return (
        f"FAILED: hash chain broken for run {report.run_id} at seq {report.break_at_seq} "
        f"({report.reason}). {report.total_events} events read before the break was detected."
    )
