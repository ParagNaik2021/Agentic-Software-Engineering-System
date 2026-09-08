"""P7 acceptance (Section 13): `make demo-greenfield` runs offline to
SUCCEEDED, pausing twice for approval. The generated service starts,
serves all endpoints, and its own test suite passes at >=80% coverage.
report.html shows the parallel design group and both approvals.

Runs entirely in replay mode against the committed
cassettes/greenfield.json — no network, no API key. The workspace is a
tmp_path (hermetic, repeatable); scripts/run_greenfield_demo.py performs
the equivalent run against the permanent workspace/urlshortener/ to
leave a real, git-committed baseline for the brownfield scenario (P8).
"""

import importlib
import sys
from pathlib import Path

import pytest

from agentic.core.context import ContextStore
from agentic.core.engine import Engine
from agentic.core.events import EventLog
from agentic.core.models import RunStatus
from agentic.core.states import NodeStatus
from agentic.core.store import RunStore
from agentic.governance.approvals import ApprovalManager
from agentic.llm.cassette import Cassette
from agentic.llm.replay import ReplayProvider
from agentic.observability.audit import verify_run_audit
from agentic.observability.metrics import MetricsCollector
from agentic.observability.reporting import ReportData, render_html
from agentic.workflows import greenfield

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "src" / "agentic" / "llm" / "prompts"
CASSETTE_PATH = REPO_ROOT / "cassettes" / "greenfield.json"


@pytest.fixture
def run(tmp_path: Path):
    workspace_root = tmp_path / "workspace"
    graph = greenfield.build_graph()
    context = ContextStore(graph, persist_dir=tmp_path / "artifacts")
    events = EventLog(path=tmp_path / "events.jsonl", run_id="run-1")
    store = RunStore(tmp_path / "state.db")

    provider = ReplayProvider(Cassette(CASSETTE_PATH), on_miss="error")
    node_executors, executors_by_agent = greenfield.build_node_executors(
        provider, PROMPTS_DIR, workspace_root, run_id="run-1"
    )
    engine = Engine(
        run_id="run-1", graph=graph, context=context, event_log=events, store=store,
        node_executors=node_executors, executors_by_agent=executors_by_agent,
        graph_expanders={"plan.decompose": greenfield.expand_impl_tasks},
    )
    engine.start(scenario="greenfield", workflow="greenfield")
    return engine, workspace_root


@pytest.mark.asyncio
async def test_greenfield_run_pauses_twice_then_succeeds(run) -> None:
    engine, _ = run

    first_pause = await engine.run()
    assert first_pause.status == RunStatus.AWAITING_APPROVAL
    assert first_pause.nodes["design.review"].status == NodeStatus.AWAITING_APPROVAL
    assert first_pause.nodes["release.readiness"].status == NodeStatus.PENDING

    engine.grant_approval("design.review", note="approved: layered architecture is sound")
    second_pause = await engine.run()
    assert second_pause.status == RunStatus.AWAITING_APPROVAL
    assert second_pause.nodes["release.readiness"].status == NodeStatus.AWAITING_APPROVAL
    assert second_pause.nodes["design.review"].status == NodeStatus.SUCCEEDED

    engine.grant_approval("release.readiness", note="approved: go")
    final_state = await engine.run()

    assert final_state.status == RunStatus.SUCCEEDED
    for node_id, node_run in final_state.nodes.items():
        assert node_run.status == NodeStatus.SUCCEEDED, f"{node_id} did not succeed: {node_run.error}"


@pytest.mark.asyncio
async def test_greenfield_shows_dynamic_impl_expansion_and_parallel_design(run) -> None:
    engine, _ = run
    await engine.run()
    engine.grant_approval("design.review")
    await engine.run()
    engine.grant_approval("release.readiness")
    final_state = await engine.run()

    assert "impl.T1" in engine.graph
    assert "impl.T2" in engine.graph
    assert set(engine.graph["impl.join"].depends_on) == {"impl.T1", "impl.T2"}
    assert final_state.nodes["impl.T1"].status == NodeStatus.SUCCEEDED
    assert final_state.nodes["impl.T2"].status == NodeStatus.SUCCEEDED

    for node_id in ("design.arch", "design.data", "design.api"):
        assert final_state.nodes[node_id].status == NodeStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_generated_service_passes_its_own_tests_at_80_percent_coverage(run) -> None:
    engine, workspace_root = run
    await engine.run()
    engine.grant_approval("design.review")
    await engine.run()
    engine.grant_approval("release.readiness")
    final_state = await engine.run()
    assert final_state.status == RunStatus.SUCCEEDED

    test_results_artifact = engine.context.get("test_results")
    assert test_results_artifact.payload["exit_code"] == 0, test_results_artifact.payload["stdout"]
    assert test_results_artifact.payload["coverage"] >= 0.80

    assert (workspace_root / "app" / "main.py").exists()
    assert (workspace_root / "app" / "service.py").exists()
    assert (workspace_root / "tests" / "test_api.py").exists()


@pytest.mark.asyncio
async def test_generated_service_actually_starts_and_serves_endpoints(run) -> None:
    """Independent confirmation beyond the internal pytest run: import
    the generated app fresh in-process and hit it through an ASGI
    client."""
    engine, workspace_root = run
    await engine.run()
    engine.grant_approval("design.review")
    await engine.run()
    engine.grant_approval("release.readiness")
    await engine.run()

    sys.path.insert(0, str(workspace_root))
    for mod_name in list(sys.modules):
        if mod_name == "app" or mod_name.startswith("app."):
            del sys.modules[mod_name]
    try:
        main_module = importlib.import_module("app.main")
        from httpx import ASGITransport, AsyncClient

        app = main_module.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/healthz")
            assert health.status_code == 200

            created = await client.post(
                "/api/v1/links", json={"target_url": "https://example.com/from-orchestrator"}
            )
            assert created.status_code == 201
            code = created.json()["code"]

            redirect = await client.get(f"/{code}", follow_redirects=False)
            assert redirect.status_code == 302
            assert redirect.headers["location"] == "https://example.com/from-orchestrator"

            ssrf_rejected = await client.post("/api/v1/links", json={"target_url": "http://127.0.0.1/admin"})
            assert ssrf_rejected.status_code == 400
    finally:
        sys.path.remove(str(workspace_root))
        for mod_name in list(sys.modules):
            if mod_name == "app" or mod_name.startswith("app."):
                del sys.modules[mod_name]


@pytest.mark.asyncio
async def test_workspace_is_committed_as_a_brownfield_baseline(run) -> None:
    import git

    engine, workspace_root = run
    await engine.run()
    engine.grant_approval("design.review")
    await engine.run()
    engine.grant_approval("release.readiness")
    await engine.run()

    repo = git.Repo(workspace_root)
    assert repo.head.is_valid()
    assert not repo.is_dirty(untracked_files=True)
    tracked_files = repo.git.ls_tree("-r", "--name-only", "HEAD")
    assert "app/main.py" in tracked_files
    assert "app/service.py" in tracked_files
    assert "tests/test_api.py" in tracked_files
    assert "__pycache__" not in tracked_files
    assert ".coverage" not in tracked_files


@pytest.mark.asyncio
async def test_audit_chain_is_unbroken_across_the_full_run(run) -> None:
    engine, _ = run
    await engine.run()
    engine.grant_approval("design.review")
    await engine.run()
    engine.grant_approval("release.readiness")
    await engine.run()

    report = verify_run_audit("run-1", engine.events)
    assert report.ok is True


@pytest.mark.asyncio
async def test_report_html_shows_parallel_design_group_and_both_approvals(run) -> None:
    engine, _ = run
    await engine.run()
    engine.grant_approval("design.review", note="approved: layered architecture is sound")
    await engine.run()
    engine.grant_approval("release.readiness", note="approved: go")
    final_state = await engine.run()

    all_events = engine.events.read()
    metrics = MetricsCollector().compute(all_events)
    approvals = ApprovalManager()
    approvals.request("design.review", "h1")
    approvals.grant("design.review", "h1", approver="alice", note="approved: layered architecture is sound")
    approvals.request("release.readiness", "h2")
    approvals.grant("release.readiness", "h2", approver="alice", note="approved: go")

    data = ReportData(
        run_state=final_state, graph=engine.graph, events=all_events, metrics=metrics,
        context=engine.context, approvals=approvals.all(),
        engineering_summary="Greenfield URL shortener delivered; see release_readiness_report.",
    )
    html = render_html(data)

    assert "design.arch" in html and "design.data" in html and "design.api" in html
    assert html.count("GRANTED") == 2
    assert "design.review" in html
    assert "release.readiness" in html
    assert "SUCCEEDED" in html
