"""Records cassettes/ambiguous.json for the ambiguous scenario (Section
11.3) — see record_greenfield_cassette.py's docstring for the
NodeKeyedProvider recording approach this reuses.

plan.decompose and design.arch each need *two* entries: their prompts
genuinely differ before and after the clarification answer exists (the
whole point of this scenario), so they hash to two different
request_hashes naturally — no special-casing needed, just two calls to
the recording engine with the context in each state.

Usage: python scripts/record_ambiguous_cassette.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from agentic.core.context import ContextStore
from agentic.core.engine import Engine
from agentic.core.events import EventLog
from agentic.core.store import RunStore
from agentic.llm.cassette import Cassette, CassetteEntry
from agentic.llm.provider import LLMProvider, LLMRequest, LLMResponse
from agentic.workflows import ambiguous

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "src" / "agentic" / "llm" / "prompts"
CASSETTE_PATH = REPO_ROOT / "cassettes" / "ambiguous.json"
RECORDING_DIR = REPO_ROOT / ".recording_tmp_ambiguous"

CLARIFICATION_ANSWER = ambiguous.DEFAULT_CLARIFICATION_ANSWER

_BEFORE = "__before_clarification__"
_AFTER = "__after_clarification__"


def _responses_before() -> dict[str, dict]:
    return {
        "req.analyze": {
            "normalized_spec": "Make the redirect path faster and provide better click analytics. No target metric or dimensions specified.",
            "acceptance_criteria": [
                {"id": "AC-1", "statement": "Redirect latency is measurably improved.", "testable": True},
                {"id": "AC-2", "statement": "Click analytics provide more than a raw count.", "testable": True},
            ],
            "ambiguity_register": [
                {"id": "AMB-1", "description": "No target latency specified.", "assumption_if_unresolved": "Target p95 under 200ms."},
                {"id": "AMB-2", "description": "Unclear which path must be faster (redirect vs create).", "assumption_if_unresolved": "Assume the redirect path."},
                {"id": "AMB-3", "description": "Unclear if throughput or latency is the concern.", "assumption_if_unresolved": "Assume latency."},
                {"id": "AMB-4", "description": "No analytics dimensions specified.", "assumption_if_unresolved": "Add daily click-count aggregation only."},
                {"id": "AMB-5", "description": "No time range specified for analytics.", "assumption_if_unresolved": "Last 30 days."},
                {"id": "AMB-6", "description": "Unclear what infrastructure changes are acceptable.", "assumption_if_unresolved": "In-process changes only, no new infra."},
            ],
        },
        "req.ambiguity": {
            "scored": [
                {"id": "AMB-1", "question": "What p95 redirect latency target should we hit?", "proposed_default": "200ms.", "impact": 0.9, "uncertainty": 0.8},
                {"id": "AMB-2", "question": "Which path should be optimized: redirect or creation?", "proposed_default": "Redirect.", "impact": 0.7, "uncertainty": 0.8},
                {"id": "AMB-3", "question": "Is this about latency or throughput?", "proposed_default": "Latency.", "impact": 0.3, "uncertainty": 0.4},
                {"id": "AMB-4", "question": "What analytics dimensions matter (geography, device, referrer)?", "proposed_default": "Daily click counts only.", "impact": 0.8, "uncertainty": 0.7},
                {"id": "AMB-5", "question": "What time range should analytics cover?", "proposed_default": "Last 30 days.", "impact": 0.3, "uncertainty": 0.3},
                {"id": "AMB-6", "question": "Are new infrastructure components acceptable?", "proposed_default": "No, in-process only.", "impact": 0.2, "uncertainty": 0.3},
            ],
            "above_threshold_ids": ["AMB-1", "AMB-2", "AMB-4"],
            "assumptions": [
                "This is a latency concern, not throughput.",
                "Analytics cover the last 30 days.",
                "No new infrastructure components; in-process changes only.",
            ],
        },
        "plan.decompose": {
            "tasks": [
                {"task_id": "T1", "description": "Add basic in-process redirect caching (default 200ms target).", "depends_on": [], "file_scope": ["app/service.py"], "effort": "M", "risk": "low"},
                {"task_id": "T2", "description": "Add daily click-count aggregation.", "depends_on": [], "file_scope": ["app/service.py"], "effort": "S", "risk": "low"},
            ],
        },
        "design.arch": {
            "components": ["services"],
            "boundaries": "Caching and aggregation live in the service layer.",
            "cross_cutting_concerns": [],
            "decisions": [
                {
                    "statement": "Add an in-process LRU redirect cache and a daily click-count aggregate, sized against the proposed 200ms default.",
                    "rationale": "No clarification received yet; proceeding on the ambiguity agent's proposed defaults so work is not blocked.",
                    "alternatives_rejected": ["waiting indefinitely for a human answer before doing any work"],
                },
            ],
        },
    }


def _responses_after() -> dict[str, dict]:
    return {
        "plan.decompose": {
            "tasks": [
                {"task_id": "T1", "description": "Add redirect-path caching and index tuning to meet p95 < 50ms.", "depends_on": [], "file_scope": ["app/service.py"], "effort": "L", "risk": "medium"},
                {"task_id": "T2", "description": "Add geographic and device-type breakdown to click analytics.", "depends_on": [], "file_scope": ["app/service.py"], "effort": "M", "risk": "low"},
            ],
        },
        "design.arch": {
            "components": ["services"],
            "boundaries": "Caching, index tuning and the extended analytics aggregation live in the service layer.",
            "cross_cutting_concerns": ["geo/device lookup on click events"],
            "decisions": [
                {
                    "statement": "Size the redirect cache and index tuning for p95 < 50ms; extend click analytics with geographic and device dimensions.",
                    "rationale": "Directly addresses the answered clarification rather than the earlier generic default.",
                    "alternatives_rejected": ["keeping the 200ms default target", "daily-count-only analytics"],
                },
            ],
        },
    }


class NodeKeyedProvider(LLMProvider):
    """Keyed by (node_id, phase) so plan.decompose/design.arch can have
    two distinct scripted responses depending on whether the
    clarification has been answered yet."""

    def __init__(self, cassette: Cassette) -> None:
        self.cassette = cassette
        self.phase = _BEFORE
        self._responses = {_BEFORE: _responses_before(), _AFTER: _responses_after()}

    async def complete(self, req: LLMRequest) -> LLMResponse:
        table = self._responses[self.phase]
        if req.node_id not in table:
            # req.analyze/req.ambiguity only exist in the "before" table
            # and are not re-invoked after clarification
            table = self._responses[_BEFORE]
        content = json.dumps(table[req.node_id])
        self.cassette.put(CassetteEntry(request_hash=req.request_hash, content=content, model=req.model))
        return LLMResponse(content=content, model=req.model, from_cassette=False)


async def _record() -> None:
    if RECORDING_DIR.exists():
        shutil.rmtree(RECORDING_DIR, ignore_errors=True)
    RECORDING_DIR.mkdir(parents=True)

    graph = ambiguous.build_graph()
    context = ContextStore(graph, persist_dir=RECORDING_DIR / "artifacts")
    events = EventLog(path=RECORDING_DIR / "events.jsonl", run_id="record")
    store = RunStore(RECORDING_DIR / "state.db")

    cassette = Cassette(CASSETTE_PATH)
    provider = NodeKeyedProvider(cassette)
    node_executors = ambiguous.build_node_executors(provider, PROMPTS_DIR, "record", CLARIFICATION_ANSWER)

    engine = Engine(
        run_id="record", graph=graph, context=context, event_log=events, store=store,
        node_executors=node_executors,
    )
    engine.start(scenario="ambiguous", workflow="ambiguous")

    # Phase 1: everything up to and including the first pass through
    # plan.decompose/design.arch/summary, using proposed defaults.
    state = await engine.run()
    assert state.nodes["design.arch"].status.value == "SUCCEEDED", state.nodes["design.arch"].error
    assert state.nodes["req.clarify"].status.value == "AWAITING_APPROVAL"

    # Phase 2: the human answers; re-plan; re-execute with the new prompts.
    provider.phase = _AFTER
    engine.grant_approval("req.clarify", note=CLARIFICATION_ANSWER)
    engine.trigger_replan("req.clarify")
    state = await engine.run()

    if state.status.value != "SUCCEEDED":
        for node_id, node_run in state.nodes.items():
            if node_run.error:
                print(f"  {node_id}: {node_run.error}")
        raise RuntimeError(f"recording run did not succeed: {state.status.value}")

    print(f"Recorded {len(cassette)} interactions to {CASSETTE_PATH}")
    shutil.rmtree(RECORDING_DIR, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(_record())
