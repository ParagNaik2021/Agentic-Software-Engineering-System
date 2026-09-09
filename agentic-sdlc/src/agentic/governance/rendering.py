"""Human-readable rendering of a DecisionPackage (Section 6.2).

One renderer, three consumers: `agentic approvals show` (terminal text),
`agentic watch`'s GUI panel, and `agentic approvals export --format docx`
(python-docx). All three call `render_sections()` below and format its
output for their medium — the content and structure are decided once,
here, not reimplemented per consumer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from docx import Document

from agentic.governance.approvals import DecisionPackage

# Payload shapes here mirror exactly what each agent's
# _to_artifacts_and_decisions() produces (agents/requirements.py,
# planner.py, architect.py, data_model.py, api_contract.py) — this
# module has no schema of its own, it just knows the artifact names it
# can render specially and falls back to a generic view for anything else.


@dataclass
class Table:
    """A structured table within a rendered section."""

    headers: list[str]
    rows: list[list[str]]


@dataclass
class Section:
    """One heading plus a mix of paragraphs and tables, in display order."""

    title: str
    blocks: list[str | Table]


def _truthy_items(payload: object, key: str) -> list[dict]:
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return [item for item in payload[key] if isinstance(item, dict)]
    return []


def _render_normalized_spec(payload: dict) -> list[str | Table]:
    return [str(payload.get("text", ""))]


def _render_acceptance_criteria(payload: dict) -> list[str | Table]:
    items = _truthy_items(payload, "items")
    if not items:
        return ["(none recorded)"]
    headers = sorted({k for item in items for k in item})
    rows = [[str(item.get(h, "")) for h in headers] for item in items]
    return [Table(headers=headers, rows=rows)]


def _render_task_graph(payload: dict) -> list[str | Table]:
    tasks = _truthy_items(payload, "tasks")
    if not tasks:
        return ["(no tasks)"]
    rows = [
        [
            str(t.get("task_id", "")), str(t.get("description", "")),
            ", ".join(t.get("depends_on", []) or []), str(t.get("effort", "")),
            str(t.get("risk", "")),
        ]
        for t in tasks
    ]
    return [Table(headers=["Task", "Description", "Depends on", "Effort", "Risk"], rows=rows)]


def _render_architecture_design(payload: dict) -> list[str | Table]:
    blocks: list[str | Table] = []
    components = payload.get("components") if isinstance(payload, dict) else None
    if components:
        blocks.append("Components: " + ", ".join(str(c) for c in components))
    boundaries = payload.get("boundaries") if isinstance(payload, dict) else None
    if boundaries:
        blocks.append(f"Boundaries: {boundaries}")
    concerns = payload.get("cross_cutting_concerns") if isinstance(payload, dict) else None
    if concerns:
        blocks.append("Cross-cutting concerns: " + ", ".join(str(c) for c in concerns))
    return blocks or ["(no architecture summary recorded)"]


def _render_adr_records(payload: dict) -> list[str | Table]:
    decisions = _truthy_items(payload, "decisions")
    if not decisions:
        return ["(no ADRs recorded)"]
    rows = [
        [
            str(d.get("statement", "")), str(d.get("rationale", "")),
            "; ".join(d.get("alternatives_rejected", []) or []),
        ]
        for d in decisions
    ]
    return [Table(headers=["Decision", "Rationale", "Alternatives rejected"], rows=rows)]


def _render_data_model_design(payload: dict) -> list[str | Table]:
    entities = _truthy_items(payload, "entities")
    if not entities:
        return ["(no entities recorded)"]
    blocks: list[str | Table] = []
    for entity in entities:
        blocks.append(f"Entity: {entity.get('name', '(unnamed)')}")
        fields = entity.get("fields") or []
        rows = [[str(f.get("name", "")), str(f.get("type", "")), str(f.get("constraints", ""))] for f in fields]
        blocks.append(Table(headers=["Field", "Type", "Constraints"], rows=rows))
    return blocks


def _render_migration_plan(payload: dict) -> list[str | Table]:
    return [str(payload.get("plan", "")) or "(no migration required)"]


def _render_openapi_schema(payload: dict) -> list[str | Table]:
    endpoints = _truthy_items(payload, "endpoints")
    if not endpoints:
        return ["(no endpoints recorded)"]
    rows = [[str(e.get("method", "")), str(e.get("path", "")), str(e.get("summary", ""))] for e in endpoints]
    return [Table(headers=["Method", "Path", "Summary"], rows=rows)]


def _render_lint_report(payload: dict) -> list[str | Table]:
    return [
        f"ruff exit code: {payload.get('ruff_exit_code', 'n/a')}",
        f"mypy exit code: {payload.get('mypy_exit_code', 'n/a')}",
    ]


def _render_security_findings(payload: dict) -> list[str | Table]:
    findings = _truthy_items(payload, "findings")
    if not findings:
        return ["No security findings."]
    rows = [[str(f.get("severity", "")), str(f.get("description", "")), str(f.get("location", ""))] for f in findings]
    return [Table(headers=["Severity", "Description", "Location"], rows=rows)]


def _render_test_results(payload: dict) -> list[str | Table]:
    exit_code = payload.get("exit_code")
    blocks: list[str | Table] = [f"Exit code: {exit_code} ({'pass' if exit_code == 0 else 'fail'})"]
    if "coverage" in payload:
        blocks.append(f"Coverage: {payload['coverage']:.0%}")
    return blocks


def _render_integration_test_results(payload: dict) -> list[str | Table]:
    exit_code = payload.get("exit_code")
    return [f"Exit code: {exit_code} ({'pass' if exit_code == 0 else 'fail'})"]


def _render_release_readiness_report(payload: dict) -> list[str | Table]:
    blocks: list[str | Table] = [f"Go/No-Go: {str(payload.get('go_no_go', 'unknown')).upper()}"]
    risks = payload.get("risks") or []
    blocks.append("Risks: " + (", ".join(str(r) for r in risks) if risks else "(none)"))
    limitations = payload.get("limitations") or []
    blocks.append("Limitations: " + (", ".join(str(m) for m in limitations) if limitations else "(none)"))
    return blocks


def _render_engineering_summary(payload: dict) -> list[str | Table]:
    return [str(payload.get("summary", "")) or "(no summary)"]


_ARTIFACT_RENDERERS = {
    "normalized_spec": ("Normalized Requirement", _render_normalized_spec),
    "acceptance_criteria": ("Acceptance Criteria", _render_acceptance_criteria),
    "task_graph": ("Task Decomposition", _render_task_graph),
    "architecture_design": ("Architecture Design", _render_architecture_design),
    "adr_records": ("Architecture Decision Records", _render_adr_records),
    "data_model_design": ("Data Model", _render_data_model_design),
    "migration_plan": ("Migration Plan", _render_migration_plan),
    "openapi_schema": ("API Contract", _render_openapi_schema),
    "lint_report": ("Static Analysis", _render_lint_report),
    "security_findings": ("Security Findings", _render_security_findings),
    "test_results": ("Unit Test Results", _render_test_results),
    "integration_test_results": ("Integration Test Results", _render_integration_test_results),
    "release_readiness_report": ("Release Readiness", _render_release_readiness_report),
    "engineering_summary": ("Engineering Summary", _render_engineering_summary),
}

# Display order for the artifact names above; anything present in the
# package but not listed here (or not one of the special-cased names)
# falls through to a generic section, in artifact list order.
_SECTION_ORDER = list(_ARTIFACT_RENDERERS)


def _generic_section(name: str, payload: object) -> Section:
    """Fallback for any artifact not one of the five named sections above
    (e.g. ambiguity_assessment, clarification_questions) — still avoids a
    raw dict/list repr: a list of dicts becomes a table, a list of
    scalars becomes a joined line, same as the special-cased renderers."""
    if not isinstance(payload, dict):
        return Section(title=name.replace("_", " ").title(), blocks=[str(payload)])

    blocks: list[str | Table] = []
    for key, value in payload.items():
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            headers = sorted({k for item in value for k in item})
            rows = [[str(item.get(h, "")) for h in headers] for item in value]
            blocks.append(f"{key}:")
            blocks.append(Table(headers=headers, rows=rows))
        elif isinstance(value, list):
            blocks.append(f"{key}: " + (", ".join(str(v) for v in value) if value else "(none)"))
        elif isinstance(value, dict):
            blocks.append(f"{key}: " + (", ".join(f"{k}={v}" for k, v in value.items()) if value else "(none)"))
        else:
            blocks.append(f"{key}: {value}")
    return Section(title=name.replace("_", " ").title(), blocks=blocks or ["(empty)"])


# code/test/doc artifacts store the full generated file as their payload
# (a raw string, not structured data) — dumping hundreds of lines of
# source into a decision package is exactly the "raw dump" this renderer
# exists to avoid, and it isn't what a design/release reviewer is there
# to read anyway (that's what the workspace/git diff is for). Summarize
# instead of rendering fully.
_SOURCE_FILE_KINDS = {"code", "test", "doc"}


def _source_file_summary(name: str, payload: object) -> Section:
    text = payload if isinstance(payload, str) else str(payload)
    line_count = text.count("\n") + 1
    return Section(
        title=name.replace("_", " ").title(),
        blocks=[f"({line_count} lines — see workspace for full content)"],
    )


def render_sections(package: DecisionPackage) -> list[Section]:
    """The single source of truth for what a decision package looks like
    to a human — every consumer formats this list, none re-derives it."""
    by_name = {a.name: a for a in package.artifacts}
    sections: list[Section] = []

    for name in _SECTION_ORDER:
        artifact = by_name.pop(name, None)
        if artifact is None:
            continue
        title, render = _ARTIFACT_RENDERERS[name]
        payload = artifact.payload if isinstance(artifact.payload, dict) else {}
        sections.append(Section(title=title, blocks=render(payload)))

    for name, artifact in by_name.items():
        if artifact.kind in _SOURCE_FILE_KINDS:
            sections.append(_source_file_summary(name, artifact.payload))
        else:
            sections.append(_generic_section(name, artifact.payload))

    if package.decisions:
        rows = [[d.agent, d.statement, d.rationale] for d in package.decisions]
        sections.append(
            Section(title="Decisions", blocks=[Table(headers=["Agent", "Statement", "Rationale"], rows=rows)])
        )

    if package.policy_verdicts:
        rows = [[v.rule_id, v.category, v.verdict.value, v.message] for v in package.policy_verdicts]
        sections.append(
            Section(
                title="Policy Evaluations",
                blocks=[Table(headers=["Rule", "Category", "Verdict", "Message"], rows=rows)],
            )
        )

    sections.append(
        Section(
            title="Checkpoint",
            blocks=[
                f"Stage: {package.stage}",
                f"input_hash: {package.input_hash}",
                f"Consequence of rejection: {package.consequence_of_rejection}",
            ],
        )
    )
    return sections


def render_text(package: DecisionPackage) -> str:
    """Plain-text rendering for the terminal and the watch GUI panel."""
    lines = [f"Decision package: {package.node_id}", "=" * (len(package.node_id) + 19), ""]
    for section in render_sections(package):
        lines.append(f"## {section.title}")
        for block in section.blocks:
            if isinstance(block, Table):
                if block.rows:
                    widths = [
                        max(len(block.headers[i]), max((len(r[i]) for r in block.rows), default=0))
                        for i in range(len(block.headers))
                    ]
                    lines.append("  " + " | ".join(h.ljust(w) for h, w in zip(block.headers, widths, strict=True)))
                    lines.append("  " + "-+-".join("-" * w for w in widths))
                    for row in block.rows:
                        lines.append("  " + " | ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)))
                else:
                    lines.append("  (none)")
            else:
                lines.append(f"  {block}")
        lines.append("")
    return "\n".join(lines)


def render_docx(package: DecisionPackage, out_path: Path) -> None:
    """Word-document rendering of the exact same Section/Table list
    render_text() formats as plain text — one content model, two output
    formats, so `approvals show` and `approvals export --format docx`
    can never drift apart on what a decision package actually contains."""
    doc = Document()
    doc.add_heading(f"Decision package: {package.node_id}", level=0)

    for section in render_sections(package):
        doc.add_heading(section.title, level=1)
        for block in section.blocks:
            if isinstance(block, Table):
                if not block.rows:
                    doc.add_paragraph("(none)")
                    continue
                table = doc.add_table(rows=1, cols=len(block.headers))
                table.style = "Light Grid Accent 1"
                for cell, header in zip(table.rows[0].cells, block.headers, strict=True):
                    cell.text = header
                for row in block.rows:
                    cells = table.add_row().cells
                    for cell, value in zip(cells, row, strict=True):
                        cell.text = value
            else:
                doc.add_paragraph(str(block))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
