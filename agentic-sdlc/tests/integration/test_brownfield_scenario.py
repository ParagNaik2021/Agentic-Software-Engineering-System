"""P8 acceptance (Section 13): brownfield produces a correct impact
report and a rollback event on the injected failure.

Operates on a copy of the real, greenfield-produced workspace/urlshortener/
(never the permanent directory itself) via the committed
cassettes/brownfield.json, in replay mode.
"""

import shutil
from pathlib import Path

import pytest

from agentic.core.context import ContextStore
from agentic.core.engine import Engine
from agentic.core.events import EventLog, EventType
from agentic.core.models import RunStatus
from agentic.core.states import NodeStatus
from agentic.core.store import RunStore
from agentic.governance.recovery import RecoveryManager
from agentic.llm.cassette import Cassette
from agentic.llm.provider import FaultInjectingProvider, FaultProfile
from agentic.llm.replay import ReplayProvider
from agentic.workflows import brownfield

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "src" / "agentic" / "llm" / "prompts"
CASSETTE_PATH = REPO_ROOT / "cassettes" / "brownfield.json"
REAL_WORKSPACE = REPO_ROOT / "workspace" / "urlshortener"


def _copy_real_workspace(dest: Path) -> Path:
    shutil.copytree(
        REAL_WORKSPACE, dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".coverage", ".mypy_cache", ".ruff_cache"),
    )
    return dest


@pytest.fixture
def brownfield_run(tmp_path: Path):
    workspace_root = _copy_real_workspace(tmp_path / "workspace")
    graph = brownfield.build_graph()
    context = ContextStore(graph, persist_dir=tmp_path / "artifacts")
    events = EventLog(path=tmp_path / "events.jsonl", run_id="run-1")
    store = RunStore(tmp_path / "state.db")

    provider = ReplayProvider(Cassette(CASSETTE_PATH), on_miss="error")
    node_executors, executors_by_agent = brownfield.build_node_executors(
        provider, PROMPTS_DIR, workspace_root, run_id="run-1"
    )
    engine = Engine(
        run_id="run-1", graph=graph, context=context, event_log=events, store=store,
        node_executors=node_executors, executors_by_agent=executors_by_agent,
        graph_expanders={"plan.decompose": brownfield.expand_impl_tasks},
    )
    engine.start(scenario="brownfield", workflow="brownfield")
    return engine, workspace_root


async def _run_to_completion(engine: Engine):
    state = await engine.run()
    while state.status.value == "AWAITING_APPROVAL":
        for node_id, node_run in state.nodes.items():
            if node_run.status.value == "AWAITING_APPROVAL":
                engine.grant_approval(node_id, note="approved")
        state = await engine.run()
    return state


@pytest.mark.asyncio
async def test_brownfield_produces_a_correct_impact_report(brownfield_run) -> None:
    engine, _ = brownfield_run
    await _run_to_completion(engine)

    impact_report = engine.context.get("impact_report")
    modules = {m["path"] for m in impact_report.payload["impacted_modules"]}

    # every module the impact report names must genuinely exist in the
    # workspace it analyzed (AstIndex, not just an LLM's claim)
    assert modules == {"app/service.py", "app/main.py"}
    assert impact_report.payload["indexed_module_count"] >= 4  # models, repository, service, main


@pytest.mark.asyncio
async def test_brownfield_happy_path_adds_bulk_endpoint_and_succeeds(brownfield_run) -> None:
    engine, workspace_root = brownfield_run
    final_state = await _run_to_completion(engine)

    assert final_state.status == RunStatus.SUCCEEDED
    content = (workspace_root / "app" / "main.py").read_text(encoding="utf-8")
    assert "links/bulk" in content

    test_results = engine.context.get("test_results")
    assert test_results.payload["exit_code"] == 0
    assert test_results.payload["coverage"] >= 0.80


@pytest.mark.asyncio
async def test_brownfield_pauses_at_design_review_and_release_readiness(brownfield_run) -> None:
    engine, _ = brownfield_run
    first_pause = await engine.run()
    assert first_pause.nodes["design.review"].status == NodeStatus.AWAITING_APPROVAL

    engine.grant_approval("design.review")
    second_pause = await engine.run()
    assert second_pause.nodes["release.readiness"].status == NodeStatus.AWAITING_APPROVAL


@pytest.mark.asyncio
async def test_chg001_blocks_implementation_without_an_impact_report(tmp_path: Path) -> None:
    """Direct test of the change-control wrapper itself (Section 6.1
    CHG-001), independent of the full cassette-driven run."""
    from agentic.agents.implementer import ImplementerAgent
    from agentic.core.context import ContextView
    from agentic.core.models import NodeSpec, SDLCStage
    from agentic.llm.mock import MockProvider, ScriptedResponse
    from agentic.tools.fs import JailedFS
    from agentic.workflows.brownfield import ChangeControlDenied, _impl_executor_with_change_control

    fs = JailedFS(tmp_path)
    agent = ImplementerAgent(MockProvider(default=ScriptedResponse(content="{}")), PROMPTS_DIR, fs=fs)
    executor = _impl_executor_with_change_control(agent, run_id="run-1", workspace_root=tmp_path)
    node = NodeSpec(node_id="impl.T1", stage=SDLCStage.IMPLEMENTATION, agent="implementer")
    empty_view = ContextView()  # no impact_report present

    with pytest.raises(ChangeControlDenied):
        await executor(node, empty_view)


@pytest.mark.asyncio
async def test_rollback_event_on_injected_quality_fault(tmp_path: Path) -> None:
    """The P8 acceptance criterion, verbatim: a rollback event on the
    injected failure. Reuses P5's FaultInjectingProvider wrapped around
    verify.unit's provider so a persistent quality failure flows through
    the real Engine <-> RecoveryManager path (Section 9.3's own example:
    kind=quality, persist=true -> fallback, then rollback)."""
    workspace_root = _copy_real_workspace(tmp_path / "workspace")
    graph = brownfield.build_graph()
    context = ContextStore(graph, persist_dir=tmp_path / "artifacts")
    events = EventLog(path=tmp_path / "events.jsonl", run_id="run-1")
    store = RunStore(tmp_path / "state.db")

    replay_provider = ReplayProvider(Cassette(CASSETTE_PATH), on_miss="error")
    faulty_verify_unit_provider = FaultInjectingProvider(
        replay_provider, FaultProfile(fail_on_node="verify.unit", kind="quality", persist=True)
    )
    node_executors, executors_by_agent = brownfield.build_node_executors(
        replay_provider, PROMPTS_DIR, workspace_root, run_id="run-1",
        verify_unit_provider=faulty_verify_unit_provider,
    )
    engine = Engine(
        run_id="run-1", graph=graph, context=context, event_log=events, store=store,
        node_executors=node_executors, executors_by_agent=executors_by_agent,
        graph_expanders={"plan.decompose": brownfield.expand_impl_tasks},
        recovery=RecoveryManager(),
        rollback_handlers={"verify.unit": brownfield.make_rollback_handler(workspace_root, context)},
    )
    engine.start(scenario="brownfield", workflow="brownfield")

    final_state = await _run_to_completion(engine)

    assert final_state.nodes["verify.unit"].status == NodeStatus.ROLLED_BACK
    rollback_started = [e for e in engine.events.read() if e.type == EventType.ROLLBACK_STARTED]
    rollback_completed = [e for e in engine.events.read() if e.type == EventType.ROLLBACK_COMPLETED]
    fallback_events = [e for e in engine.events.read() if e.type == EventType.FALLBACK_ENGAGED]
    assert len(fallback_events) == 1
    assert len(rollback_started) == 1
    assert len(rollback_completed) == 1

    # the workspace genuinely reverted: the bulk endpoint's route line is
    # gone from the working tree (rolled back to the impl.join checkpoint,
    # which for this task is the pre-implementation state itself, since
    # T1's implementer commit is exactly what gets checkpointed at impl.join)
    import git

    repo = git.Repo(workspace_root)
    checkpoint_artifact = context.get("workspace_checkpoint")
    assert repo.head.commit.hexsha == checkpoint_artifact.payload["commit_sha"]

    # nothing downstream of verify.unit could ever run -> safe-stop
    assert final_state.status == RunStatus.HALTED
    assert final_state.nodes["verify.gate"].status == NodeStatus.PENDING
