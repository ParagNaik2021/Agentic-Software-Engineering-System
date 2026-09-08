"""Runs the brownfield workflow for real against the permanent
workspace/urlshortener/ directory (replay mode, committed cassette),
auto-granting both approvals — the happy-path companion to
run_greenfield_demo.py. The workspace already has the bulk-endpoint
code applied on disk (development changes ahead of this run); this run
is what makes the orchestrator's own decisions/impact-report/checkpoint
trail exist for it, and commits the result as the final baseline.

Usage: python scripts/run_brownfield_demo.py
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from agentic.core.context import ContextStore
from agentic.core.engine import Engine
from agentic.core.events import EventLog
from agentic.core.store import RunStore
from agentic.llm.cassette import Cassette
from agentic.llm.replay import ReplayProvider
from agentic.observability.audit import verify_run_audit
from agentic.workflows import brownfield

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "src" / "agentic" / "llm" / "prompts"
CASSETTE_PATH = REPO_ROOT / "cassettes" / "brownfield.json"
WORKSPACE_ROOT = REPO_ROOT / "workspace" / "urlshortener"
RUN_ID = "brownfield-demo"
RUN_DIR = REPO_ROOT / "runs" / RUN_ID


async def _run() -> None:
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    RUN_DIR.mkdir(parents=True)

    graph = brownfield.build_graph()
    context = ContextStore(graph, persist_dir=RUN_DIR / "artifacts")
    events = EventLog(path=RUN_DIR / "events.jsonl", run_id=RUN_ID)
    store = RunStore(RUN_DIR / "state.db")

    provider = ReplayProvider(Cassette(CASSETTE_PATH), on_miss="error")
    node_executors, executors_by_agent = brownfield.build_node_executors(
        provider, PROMPTS_DIR, WORKSPACE_ROOT, run_id=RUN_ID
    )
    engine = Engine(
        run_id=RUN_ID, graph=graph, context=context, event_log=events, store=store,
        node_executors=node_executors, executors_by_agent=executors_by_agent,
        graph_expanders={"plan.decompose": brownfield.expand_impl_tasks},
    )
    engine.start(scenario="brownfield", workflow="brownfield")

    state = await engine.run()
    while state.status.value == "AWAITING_APPROVAL":
        for node_id, node_run in state.nodes.items():
            if node_run.status.value == "AWAITING_APPROVAL":
                print(f"approving {node_id}")
                engine.grant_approval(node_id, note="approved by run_brownfield_demo.py")
        state = await engine.run()

    print(f"final status: {state.status.value}")
    if state.status.value != "SUCCEEDED":
        for node_id, node_run in state.nodes.items():
            if node_run.error:
                print(f"  {node_id}: {node_run.error}")
        raise RuntimeError("brownfield demo run did not succeed")

    audit = verify_run_audit(RUN_ID, events)
    print(f"audit chain ok: {audit.ok}")

    impact_report = context.get("impact_report")
    print(f"impact report modules: {[m['path'] for m in impact_report.payload['impacted_modules']]}")

    test_results = context.get("test_results")
    print(f"generated app tests: exit_code={test_results.payload['exit_code']} "
          f"coverage={test_results.payload['coverage']:.0%}")
    print(f"workspace: {WORKSPACE_ROOT}")


if __name__ == "__main__":
    asyncio.run(_run())
