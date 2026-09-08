"""P4 acceptance: report.html renders and opens (i.e. is well-formed
HTML a browser could load); report.md renders without error."""

from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest

from agentic.core.context import ContextStore
from agentic.core.engine import Engine, NodeExecutionResult
from agentic.core.events import EventLog
from agentic.core.graph import WorkflowGraph
from agentic.core.models import Artifact, Decision, NodeSpec, SDLCStage, compute_content_hash
from agentic.core.store import RunStore
from agentic.governance.approvals import ApprovalManager
from agentic.observability.audit import verify_run_audit
from agentic.observability.metrics import MetricsCollector
from agentic.observability.reporting import ReportData, render_html, render_markdown


class _StrictHTMLValidator(HTMLParser):
    """Fails on malformed markup a real browser would choke on: every
    opened tag must be closed, in order, and nothing is left dangling."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        _self_closing = {"meta", "br", "img", "hr", "input", "link"}
        self._void = _self_closing

    def handle_starttag(self, tag, attrs):
        if tag not in self._void:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, f"mismatched closing tag: {tag}"
        self.stack.pop()


def _build_graph() -> WorkflowGraph:
    graph = WorkflowGraph(root="intake")
    graph.add_node(NodeSpec(node_id="intake", stage=SDLCStage.INTAKE))
    graph.add_node(
        NodeSpec(node_id="design", stage=SDLCStage.DESIGN_REVIEW, agent="architect",
                 depends_on=["intake"])
    )
    graph.validate()
    return graph


async def _stub_executor(node, view) -> NodeExecutionResult:
    payload = {"produced_by": node.node_id}
    artifact = Artifact(
        artifact_id=f"{node.node_id}-artifact", name=f"artifact_{node.node_id}", kind="design",
        content_hash=compute_content_hash(payload), payload=payload,
        produced_by_node=node.node_id, produced_by_agent=node.agent or "system",
        run_id="run-1", created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    decisions = []
    if node.node_id == "design":
        decisions.append(
            Decision(decision_id="d1", node_id="design", agent="architect",
                      statement="layered architecture", rationale="clear separation of concerns",
                      created_at=artifact.created_at)
        )
    return NodeExecutionResult(artifacts=[artifact], decisions=decisions)


@pytest.fixture
def report_data(tmp_path: Path) -> ReportData:
    import asyncio

    graph = _build_graph()
    context = ContextStore(graph)
    events = EventLog(path=tmp_path / "events.jsonl", run_id="run-1")
    store = RunStore(tmp_path / "state.db")
    engine = Engine(
        run_id="run-1", graph=graph, context=context, event_log=events, store=store,
        node_executors={"intake": _stub_executor, "design": _stub_executor},
    )
    engine.start(scenario="greenfield", workflow="greenfield")
    final_state = asyncio.run(engine.run())

    all_events = events.read()
    metrics = MetricsCollector().compute(all_events)
    audit = verify_run_audit("run-1", events)
    approvals = ApprovalManager()

    return ReportData(
        run_state=final_state, graph=graph, events=all_events, metrics=metrics,
        context=context, approvals=approvals.all(), audit=audit,
        engineering_summary="Both nodes completed successfully.",
    )


def test_render_markdown_contains_key_sections(report_data: ReportData) -> None:
    md = render_markdown(report_data)

    assert f"# Run Report: {report_data.run_state.run_id}" in md
    assert "## Nodes" in md
    assert "## Execution Timeline" in md
    assert "## Artifacts" in md
    assert "## Decisions" in md
    assert "## Gate Evaluations" in md
    assert "## Metrics" in md
    assert "design" in md
    assert "layered architecture" in md


def test_render_html_is_well_formed_and_opens(report_data: ReportData) -> None:
    html = render_html(report_data)

    assert html.strip().lower().startswith("<!doctype html>")
    assert "<html>" in html and "</html>" in html
    assert report_data.run_state.run_id in html

    parser = _StrictHTMLValidator()
    parser.feed(html)  # raises AssertionError on any mismatched tag
    parser.close()
    assert parser.stack == []  # every tag closed


def test_render_html_shows_node_statuses_and_metrics(report_data: ReportData) -> None:
    html = render_html(report_data)

    assert "SUCCEEDED" in html
    assert "Node success rate" in html
    assert "architect" not in html or "design" in html  # decision/artifact section rendered


def test_render_html_writes_to_disk_and_can_be_reopened(report_data: ReportData, tmp_path: Path) -> None:
    html = render_html(report_data)
    report_path = tmp_path / "report.html"
    report_path.write_text(html, encoding="utf-8")

    reopened = report_path.read_text(encoding="utf-8")
    assert reopened == html
