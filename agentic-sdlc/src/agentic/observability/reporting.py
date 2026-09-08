"""Run report rendering (Section 7.3): report.md and report.html — the
single artifact a reviewer opens first. Contains the run header and
outcome, per-node status, the execution timeline, artifacts with
lineage, decisions with rationale, every gate/policy evaluation,
approval records, recovery events, the metrics table, and a closing
engineering summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jinja2 import Template

from agentic.core.context import ContextStore
from agentic.core.events import Event, EventType
from agentic.core.graph import WorkflowGraph
from agentic.core.models import RunState
from agentic.governance.approvals import ApprovalRecord
from agentic.observability.audit import AuditReport
from agentic.observability.metrics import RunMetricsSnapshot
from agentic.observability.tracing import Span, build_spans_from_events

_STATUS_COLORS = {
    "SUCCEEDED": "#1a7f37",
    "FAILED": "#cf222e",
    "RUNNING": "#0969da",
    "PENDING": "#6e7781",
    "BLOCKED": "#9a6700",
    "READY": "#8250df",
    "AWAITING_APPROVAL": "#bf8700",
    "REJECTED": "#cf222e",
    "SKIPPED": "#6e7781",
    "INVALIDATED": "#8250df",
    "RETRYING": "#0969da",
    "FALLBACK": "#0969da",
    "ROLLING_BACK": "#cf222e",
    "ROLLED_BACK": "#9a6700",
    "HALTED": "#24292f",
}


@dataclass
class ReportData:
    run_state: RunState
    graph: WorkflowGraph
    events: list[Event]
    metrics: RunMetricsSnapshot
    context: ContextStore
    approvals: list[ApprovalRecord] = field(default_factory=list)
    audit: AuditReport | None = None
    engineering_summary: str = ""

    @property
    def spans(self) -> list[Span]:
        return build_spans_from_events(self.events)


def _fmt_dt(dt) -> str:
    return dt.isoformat() if dt is not None else "-"


def reconstruct_approvals_from_events(events: list[Event]) -> list[ApprovalRecord]:
    """A live ApprovalManager doesn't survive a CLI process boundary
    (`agentic report` runs fresh each time), but APPROVAL_REQUESTED/
    GRANTED/REJECTED events do — rebuild the record list from them so
    the report is correct without a live governance object."""
    records: dict[str, ApprovalRecord] = {}
    for event in events:
        if event.type == EventType.APPROVAL_REQUESTED and event.node_id:
            records[event.node_id] = ApprovalRecord(
                node_id=event.node_id, input_hash=event.payload.get("input_hash", ""),
                status="PENDING", requested_at=event.ts,
            )
        elif event.type == EventType.APPROVAL_GRANTED and event.node_id:
            record = records.get(event.node_id) or ApprovalRecord(
                node_id=event.node_id, input_hash=event.payload.get("input_hash", ""), status="PENDING",
            )
            record.status = "GRANTED"
            record.approver = event.actor.id
            record.note = event.payload.get("note", "")
            record.decided_at = event.ts
            records[event.node_id] = record
        elif event.type == EventType.APPROVAL_REJECTED and event.node_id:
            record = records.get(event.node_id) or ApprovalRecord(
                node_id=event.node_id, input_hash="", status="PENDING",
            )
            record.status = "REJECTED"
            record.approver = event.actor.id
            record.note = event.payload.get("note", "")
            record.decided_at = event.ts
            records[event.node_id] = record
    return list(records.values())


def _gate_events(events: list[Event]) -> list[Event]:
    return [e for e in events if e.type == EventType.GATE_EVALUATED]


def _policy_events(events: list[Event]) -> list[Event]:
    return [e for e in events if e.type == EventType.POLICY_EVALUATED]


def _recovery_events(events: list[Event]) -> list[Event]:
    recovery_types = {
        EventType.RETRY_SCHEDULED, EventType.FALLBACK_ENGAGED,
        EventType.ROLLBACK_STARTED, EventType.ROLLBACK_COMPLETED,
        EventType.SAFE_STOP_ENGAGED, EventType.NODE_INVALIDATED,
        EventType.REPLAN_TRIGGERED,
    }
    return [e for e in events if e.type in recovery_types]


def render_markdown(data: ReportData) -> str:
    rs = data.run_state
    lines: list[str] = []
    lines.append(f"# Run Report: {rs.run_id}")
    lines.append("")
    lines.append(f"- **Scenario:** {rs.scenario}")
    lines.append(f"- **Workflow:** {rs.workflow}")
    lines.append(f"- **Status:** {rs.status.value}")
    lines.append(f"- **Created:** {_fmt_dt(rs.created_at)}")
    lines.append(f"- **Re-plans:** {rs.replan_count}")
    if data.audit is not None:
        lines.append(f"- **Audit chain:** {'OK' if data.audit.ok else 'BROKEN at seq ' + str(data.audit.break_at_seq)}")
    lines.append("")

    lines.append("## Nodes")
    lines.append("")
    lines.append("| Node | Status | Attempts | Duration (ms) |")
    lines.append("|---|---|---|---|")
    for node_id in data.graph.node_ids:
        nr = rs.nodes.get(node_id)
        if nr is None:
            continue
        lines.append(f"| {node_id} | {nr.status.value} | {nr.attempt} | {nr.duration_ms or '-'} |")
    lines.append("")

    lines.append("## Execution Timeline")
    lines.append("")
    for span in sorted(data.spans, key=lambda s: s.start):
        end = _fmt_dt(span.end)
        lines.append(f"- `{span.name}` [{span.kind}] {_fmt_dt(span.start)} -> {end} ({span.status})")
    lines.append("")

    lines.append("## Artifacts")
    lines.append("")
    seen_artifacts = set()
    for node_id, nr in rs.nodes.items():
        for artifact_id in nr.produced:
            if artifact_id in seen_artifacts:
                continue
            seen_artifacts.add(artifact_id)
            artifact = data.context.get_by_id(artifact_id)
            if artifact is None:
                continue
            lineage = data.context.lineage(artifact_id)
            ancestors = ", ".join(a.name for a in lineage.artifacts if a.artifact_id != artifact_id) or "-"
            lines.append(f"- **{artifact.name}** (v{artifact.version}, node `{node_id}`) — lineage: {ancestors}")
    lines.append("")

    lines.append("## Decisions")
    lines.append("")
    for node_id, nr in rs.nodes.items():
        for decision_id in nr.decisions:
            decision = data.context.get_decision(decision_id)
            if decision is None:
                continue
            lines.append(f"- **{node_id}** ({decision.agent}): {decision.statement} — _{decision.rationale}_")
    lines.append("")

    lines.append("## Gate Evaluations")
    lines.append("")
    lines.append("| Node | Phase | Condition | Verdict | Message |")
    lines.append("|---|---|---|---|---|")
    for e in _gate_events(data.events):
        p = e.payload
        lines.append(
            f"| {e.node_id} | {p.get('phase','-')} | {p.get('condition','-')} | "
            f"{p.get('verdict','-')} | {p.get('message','')} |"
        )
    lines.append("")

    lines.append("## Policy Evaluations")
    lines.append("")
    lines.append("| Node | Rule | Category | Verdict | Message |")
    lines.append("|---|---|---|---|---|")
    for e in _policy_events(data.events):
        p = e.payload
        lines.append(
            f"| {e.node_id} | {p.get('rule_id','-')} | {p.get('category','-')} | "
            f"{p.get('verdict','-')} | {p.get('message','')} |"
        )
    lines.append("")

    lines.append("## Approvals")
    lines.append("")
    for record in data.approvals:
        lines.append(
            f"- `{record.node_id}`: **{record.status}** by {record.approver or '-'} "
            f"({_fmt_dt(record.requested_at)} -> {_fmt_dt(record.decided_at)}) — {record.note or ''}"
        )
    lines.append("")

    lines.append("## Recovery Events")
    lines.append("")
    for e in _recovery_events(data.events):
        lines.append(f"- `{e.type.value}` node=`{e.node_id or '-'}` {e.payload}")
    lines.append("")

    lines.append("## Metrics")
    lines.append("")
    m = data.metrics
    lines.append(f"- Node success rate (first attempt): {m.node_success_rate:.0%}")
    lines.append(f"- Retry frequency: {m.retry_frequency:.2f} per executed node")
    lines.append(f"- Rollback count: {m.rollback_count}")
    mttr = m.mttr
    mttr_str = f"{mttr.mean_seconds:.1f}s" if mttr.mean_seconds is not None else "n/a"
    lines.append(f"- MTTR: {mttr_str} (unrecovered failures: {len(mttr.unrecovered_failures)})")
    if m.end_to_end_latency_seconds is not None:
        lines.append(
            f"- End-to-end latency: {m.end_to_end_latency_seconds:.1f}s "
            f"({m.end_to_end_latency_excluding_approval_seconds:.1f}s excluding approval wait)"
        )
    lines.append(f"- Gate rejection rate: {m.gate_metrics.rejection_rate:.0%} ({m.gate_metrics.total} evaluations)")
    lines.append(f"- Policy violation rate: {m.policy_metrics.violation_rate:.0%} ({m.policy_metrics.total} evaluations)")
    lines.append(f"- Re-plans: {m.replan_metrics.count}")
    lines.append(f"- Autonomy denials: {m.autonomy_denials}")
    lines.append(f"- Tokens: {m.token_cost.total_tokens} (${m.token_cost.total_cost_usd:.4f})")
    lines.append("")

    lines.append("## Engineering Summary")
    lines.append("")
    lines.append(data.engineering_summary or "_Not yet generated (release_manager agent lands in P6)._")
    lines.append("")

    return "\n".join(lines)


_HTML_TEMPLATE = Template(
    """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Run Report: {{ rs.run_id }}</title>
<style>
  body { font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 2rem; color: #1f2328; }
  h1, h2 { border-bottom: 1px solid #d0d7de; padding-bottom: 0.3rem; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 1.5rem; }
  th, td { border: 1px solid #d0d7de; padding: 4px 8px; text-align: left; font-size: 0.9rem; }
  th { background: #f6f8fa; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; color: white; font-size: 0.8rem; }
  .timeline-row { display: flex; align-items: center; margin: 2px 0; font-size: 0.85rem; }
  .timeline-label { width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .timeline-track { position: relative; flex: 1; height: 16px; background: #f6f8fa; }
  .timeline-bar { position: absolute; height: 100%; border-radius: 3px; }
</style>
</head>
<body>
<h1>Run Report: {{ rs.run_id }}</h1>
<p>
  <b>Scenario:</b> {{ rs.scenario }} &nbsp;
  <b>Workflow:</b> {{ rs.workflow }} &nbsp;
  <b>Status:</b> <span class="badge" style="background:{{ colors.get(rs.status.value, '#666') }}">{{ rs.status.value }}</span> &nbsp;
  <b>Re-plans:</b> {{ rs.replan_count }}
  {% if audit %}&nbsp; <b>Audit:</b> {{ "OK" if audit.ok else "BROKEN at seq " ~ audit.break_at_seq }}{% endif %}
</p>

<h2>Nodes</h2>
<table>
<tr><th>Node</th><th>Status</th><th>Attempts</th><th>Duration (ms)</th></tr>
{% for node_id in node_ids %}
{% set nr = rs.nodes.get(node_id) %}
{% if nr %}
<tr>
  <td>{{ node_id }}</td>
  <td><span class="badge" style="background:{{ colors.get(nr.status.value, '#666') }}">{{ nr.status.value }}</span></td>
  <td>{{ nr.attempt }}</td>
  <td>{{ nr.duration_ms or '-' }}</td>
</tr>
{% endif %}
{% endfor %}
</table>

<h2>Execution Timeline</h2>
{% for span in spans %}
<div class="timeline-row">
  <div class="timeline-label">{{ span.name }} [{{ span.kind }}]</div>
  <div class="timeline-track">
    <div class="timeline-bar" style="left:{{ span.offset_pct }}%; width:{{ span.width_pct }}%; background:{{ '#cf222e' if span.status == 'error' else '#1a7f37' }};"></div>
  </div>
</div>
{% endfor %}

<h2>Artifacts</h2>
<ul>
{% for a in artifacts %}
<li id="artifact-{{ a.artifact.artifact_id }}"><b>{{ a.artifact.name }}</b> (v{{ a.artifact.version }}, node <code>{{ a.node_id }}</code>) &mdash; lineage: {{ a.ancestors }}</li>
{% endfor %}
</ul>

<h2>Decisions</h2>
<ul>
{% for d in decisions %}
<li><b>{{ d.node_id }}</b> ({{ d.decision.agent }}): {{ d.decision.statement }} &mdash; <i>{{ d.decision.rationale }}</i></li>
{% endfor %}
</ul>

<h2>Gate Evaluations</h2>
<table>
<tr><th>Node</th><th>Phase</th><th>Condition</th><th>Verdict</th><th>Message</th></tr>
{% for e in gate_events %}
<tr><td>{{ e.node_id }}</td><td>{{ e.payload.get('phase','-') }}</td><td>{{ e.payload.get('condition','-') }}</td>
<td>{{ e.payload.get('verdict','-') }}</td><td>{{ e.payload.get('message','') }}</td></tr>
{% endfor %}
</table>

<h2>Policy Evaluations</h2>
<table>
<tr><th>Node</th><th>Rule</th><th>Category</th><th>Verdict</th><th>Message</th></tr>
{% for e in policy_events %}
<tr><td>{{ e.node_id }}</td><td>{{ e.payload.get('rule_id','-') }}</td><td>{{ e.payload.get('category','-') }}</td>
<td>{{ e.payload.get('verdict','-') }}</td><td>{{ e.payload.get('message','') }}</td></tr>
{% endfor %}
</table>

<h2>Approvals</h2>
<ul>
{% for r in approvals %}
<li><code>{{ r.node_id }}</code>: <b>{{ r.status }}</b> by {{ r.approver or '-' }} ({{ r.note or '' }})</li>
{% endfor %}
</ul>

<h2>Recovery Events</h2>
<ul>
{% for e in recovery_events %}
<li><code>{{ e.type.value }}</code> node=<code>{{ e.node_id or '-' }}</code> {{ e.payload }}</li>
{% endfor %}
</ul>

<h2>Metrics</h2>
<table>
<tr><th>Metric</th><th>Value</th></tr>
<tr><td>Node success rate (first attempt)</td><td>{{ "%.0f"|format(metrics.node_success_rate * 100) }}%</td></tr>
<tr><td>Retry frequency</td><td>{{ "%.2f"|format(metrics.retry_frequency) }}</td></tr>
<tr><td>Rollback count</td><td>{{ metrics.rollback_count }}</td></tr>
<tr><td>MTTR</td><td>{{ "%.1fs"|format(metrics.mttr.mean_seconds) if metrics.mttr.mean_seconds is not none else "n/a" }} (unrecovered: {{ metrics.mttr.unrecovered_failures|length }})</td></tr>
<tr><td>Gate rejection rate</td><td>{{ "%.0f"|format(metrics.gate_metrics.rejection_rate * 100) }}%</td></tr>
<tr><td>Policy violation rate</td><td>{{ "%.0f"|format(metrics.policy_metrics.violation_rate * 100) }}%</td></tr>
<tr><td>Re-plans</td><td>{{ metrics.replan_metrics.count }}</td></tr>
<tr><td>Autonomy denials</td><td>{{ metrics.autonomy_denials }}</td></tr>
<tr><td>Tokens / cost</td><td>{{ metrics.token_cost.total_tokens }} (${{ "%.4f"|format(metrics.token_cost.total_cost_usd) }})</td></tr>
</table>

<h2>Engineering Summary</h2>
<p>{{ engineering_summary or "Not yet generated (release_manager agent lands in P6)." }}</p>

</body>
</html>
"""
)


def render_html(data: ReportData) -> str:
    spans = sorted(data.spans, key=lambda s: s.start)
    timeline_spans = []
    if spans:
        run_start = min(s.start for s in spans)
        run_end = max((s.end or s.start) for s in spans)
        total_seconds = max((run_end - run_start).total_seconds(), 0.001)
        for s in spans:
            offset_pct = (s.start - run_start).total_seconds() / total_seconds * 100
            span_end = s.end or s.start
            width_pct = max((span_end - s.start).total_seconds() / total_seconds * 100, 0.5)
            timeline_spans.append(
                type("_TimelineSpan", (), {
                    "name": s.name, "kind": s.kind, "status": s.status,
                    "offset_pct": round(offset_pct, 2), "width_pct": round(width_pct, 2),
                })()
            )

    artifacts = []
    seen = set()
    for node_id, nr in data.run_state.nodes.items():
        for artifact_id in nr.produced:
            if artifact_id in seen:
                continue
            seen.add(artifact_id)
            artifact = data.context.get_by_id(artifact_id)
            if artifact is None:
                continue
            lineage = data.context.lineage(artifact_id)
            ancestors = ", ".join(a.name for a in lineage.artifacts if a.artifact_id != artifact_id) or "-"
            artifacts.append({"artifact": artifact, "node_id": node_id, "ancestors": ancestors})

    decisions = []
    for node_id, nr in data.run_state.nodes.items():
        for decision_id in nr.decisions:
            decision = data.context.get_decision(decision_id)
            if decision is not None:
                decisions.append({"node_id": node_id, "decision": decision})

    return _HTML_TEMPLATE.render(
        rs=data.run_state,
        node_ids=data.graph.node_ids,
        colors=_STATUS_COLORS,
        spans=timeline_spans,
        artifacts=artifacts,
        decisions=decisions,
        gate_events=_gate_events(data.events),
        policy_events=_policy_events(data.events),
        approvals=data.approvals,
        recovery_events=_recovery_events(data.events),
        metrics=data.metrics,
        audit=data.audit,
        engineering_summary=data.engineering_summary,
    )
