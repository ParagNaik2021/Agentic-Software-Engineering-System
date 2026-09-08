"""Runs the greenfield workflow for real against the permanent
workspace/urlshortener/ directory (replay mode, committed cassette),
auto-granting both approvals, so P8's brownfield scenario has a
genuine, git-committed baseline to build on — Section 13 P7: "Commit
the generated workspace as the brownfield baseline."

This is the same engine/workflow code path tests/integration/
test_greenfield_scenario.py exercises against a tmp_path; this script
just points it at the real, permanent workspace instead, once.

Usage: python scripts/run_greenfield_demo.py
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
from agentic.workflows import greenfield

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "src" / "agentic" / "llm" / "prompts"
CASSETTE_PATH = REPO_ROOT / "cassettes" / "greenfield.json"
WORKSPACE_ROOT = REPO_ROOT / "workspace" / "urlshortener"
RUN_ID = "greenfield-demo"
RUN_DIR = REPO_ROOT / "runs" / RUN_ID


async def _run() -> None:
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    RUN_DIR.mkdir(parents=True)

    graph = greenfield.build_graph()
    context = ContextStore(graph, persist_dir=RUN_DIR / "artifacts")
    events = EventLog(path=RUN_DIR / "events.jsonl", run_id=RUN_ID)
    store = RunStore(RUN_DIR / "state.db")

    provider = ReplayProvider(Cassette(CASSETTE_PATH), on_miss="error")
    node_executors, executors_by_agent = greenfield.build_node_executors(
        provider, PROMPTS_DIR, WORKSPACE_ROOT, run_id=RUN_ID
    )
    engine = Engine(
        run_id=RUN_ID, graph=graph, context=context, event_log=events, store=store,
        node_executors=node_executors, executors_by_agent=executors_by_agent,
        graph_expanders={"plan.decompose": greenfield.expand_impl_tasks},
    )
    engine.start(scenario="greenfield", workflow="greenfield")

    state = await engine.run()
    while state.status.value == "AWAITING_APPROVAL":
        for node_id, node_run in state.nodes.items():
            if node_run.status.value == "AWAITING_APPROVAL":
                print(f"approving {node_id}")
                engine.grant_approval(node_id, note="approved by run_greenfield_demo.py")
        state = await engine.run()

    print(f"final status: {state.status.value}")
    if state.status.value != "SUCCEEDED":
        for node_id, node_run in state.nodes.items():
            if node_run.error:
                print(f"  {node_id}: {node_run.error}")
        raise RuntimeError("greenfield demo run did not succeed")

    audit = verify_run_audit(RUN_ID, events)
    print(f"audit chain ok: {audit.ok}")

    test_results = context.get("test_results")
    print(f"generated app tests: exit_code={test_results.payload['exit_code']} "
          f"coverage={test_results.payload['coverage']:.0%}")
    print(f"workspace: {WORKSPACE_ROOT}")


if __name__ == "__main__":
    asyncio.run(_run())
