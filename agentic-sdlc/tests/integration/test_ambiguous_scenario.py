"""P8 acceptance (Section 13): the ambiguous scenario pauses for
clarification, and the answer demonstrably invalidates and re-executes
downstream nodes with REPLAN_TRIGGERED in the log.

Runs offline against the committed cassettes/ambiguous.json.
"""

from pathlib import Path

import pytest

from agentic.core.context import ContextStore
from agentic.core.engine import Engine
from agentic.core.events import EventLog, EventType
from agentic.core.models import RunStatus
from agentic.core.states import NodeStatus
from agentic.core.store import RunStore
from agentic.llm.cassette import Cassette
from agentic.llm.replay import ReplayProvider
from agentic.workflows import ambiguous

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "src" / "agentic" / "llm" / "prompts"
CASSETTE_PATH = REPO_ROOT / "cassettes" / "ambiguous.json"
CLARIFICATION_ANSWER = ambiguous.DEFAULT_CLARIFICATION_ANSWER


@pytest.fixture
def run(tmp_path: Path):
    graph = ambiguous.build_graph()
    context = ContextStore(graph, persist_dir=tmp_path / "artifacts")
    events = EventLog(path=tmp_path / "events.jsonl", run_id="run-1")
    store = RunStore(tmp_path / "state.db")

    provider = ReplayProvider(Cassette(CASSETTE_PATH), on_miss="error")
    node_executors = ambiguous.build_node_executors(provider, PROMPTS_DIR, "run-1", CLARIFICATION_ANSWER)
    engine = Engine(
        run_id="run-1", graph=graph, context=context, event_log=events, store=store,
        node_executors=node_executors,
    )
    engine.start(scenario="ambiguous", workflow="ambiguous")
    return engine


@pytest.mark.asyncio
async def test_ambiguity_register_has_six_or_more_entries_with_three_above_threshold(run) -> None:
    engine = run
    await engine.run()

    ambiguity_register = engine.context.get("ambiguity_register")
    assert len(ambiguity_register.payload["items"]) >= 6

    assessment = engine.context.get("ambiguity_assessment")
    assert len(assessment.payload["assumptions"]) >= 1

    questions = engine.context.get("clarification_questions")
    assert len(questions.payload["questions"]) == 3


@pytest.mark.asyncio
async def test_plan_and_design_proceed_immediately_with_defaults_before_clarification(run) -> None:
    """Section 11.3: "This is representative of what engineers actually
    receive" — the run does NOT block on the human; it produces a first,
    default-driven pass while req.clarify sits open."""
    engine = run
    first_pass = await engine.run()

    assert first_pass.nodes["req.clarify"].status == NodeStatus.AWAITING_APPROVAL
    assert first_pass.nodes["plan.decompose"].status == NodeStatus.SUCCEEDED
    assert first_pass.nodes["design.arch"].status == NodeStatus.SUCCEEDED
    assert first_pass.nodes["summary"].status == NodeStatus.SUCCEEDED

    task_graph = engine.context.get("task_graph")
    assert "200ms" in task_graph.payload["tasks"][0]["description"] or "basic" in task_graph.payload["tasks"][0]["description"].lower()


@pytest.mark.asyncio
async def test_answering_clarification_triggers_replan_and_invalidates_downstream(run) -> None:
    engine = run
    await engine.run()

    pre_replan_task_graph_id = engine.context.get("task_graph").artifact_id

    engine.grant_approval("req.clarify", note=CLARIFICATION_ANSWER)
    engine.trigger_replan("req.clarify")
    final_state = await engine.run()

    assert final_state.status == RunStatus.SUCCEEDED

    replan_events = [e for e in engine.events.read() if e.type == EventType.REPLAN_TRIGGERED]
    assert len(replan_events) == 1
    assert replan_events[0].payload["changed_node"] == "req.clarify"
    invalidated = set(replan_events[0].payload["invalidated"])
    assert invalidated == {"plan.decompose", "design.arch", "summary"}

    invalidated_events = [e for e in engine.events.read() if e.type == EventType.NODE_INVALIDATED]
    assert {e.node_id for e in invalidated_events} == {"plan.decompose", "design.arch", "summary"}

    # every invalidated node actually re-executed (attempt count increased)
    for node_id in ("plan.decompose", "design.arch", "summary"):
        assert final_state.nodes[node_id].status == NodeStatus.SUCCEEDED
        assert final_state.nodes[node_id].attempt == 2

    new_task_graph = engine.context.get("task_graph")
    assert new_task_graph.artifact_id != pre_replan_task_graph_id


@pytest.mark.asyncio
async def test_final_design_reflects_the_answered_clarification_not_the_default(run) -> None:
    engine = run
    await engine.run()
    engine.grant_approval("req.clarify", note=CLARIFICATION_ANSWER)
    engine.trigger_replan("req.clarify")
    await engine.run()

    task_graph = engine.context.get("task_graph")
    descriptions = " ".join(t["description"] for t in task_graph.payload["tasks"])
    assert "50ms" in descriptions
    assert "geographic" in descriptions.lower() or "device" in descriptions.lower()

    adr = engine.context.get("adr_records")
    assert any("50ms" in d["statement"] for d in adr.payload["decisions"])


@pytest.mark.asyncio
async def test_below_threshold_ambiguities_are_recorded_as_assumptions_not_silent_guesses(run) -> None:
    engine = run
    await engine.run()

    assessment = engine.context.get("ambiguity_assessment")
    assert len(assessment.payload["assumptions"]) == 3  # AMB-3, AMB-5, AMB-6 in the recorded cassette
