"""Greenfield workflow (Section 5.1's canonical graph, Section 11.1).

The graph and executor wiring both live in workflows/common.py, shared
with workflows/ambiguous.py so the two cannot drift apart — see
common.build_canonical_graph's docstring for why that sharing is a
governance requirement (GOV-001) and not just de-duplication.

req.clarify and analysis.impact are absent from this graph: per Section
5.1 they are "skipped when ambiguity score < threshold" and "active only
in brownfield" respectively, and the ambiguity agent's cassette response
for this scenario keeps every ambiguity below threshold, so no
clarification branch is ever reachable here. ambiguous.py is the same
graph with that branch switched on.
"""

from __future__ import annotations

from pathlib import Path

from agentic.core.engine import NodeExecutor
from agentic.core.graph import WorkflowGraph
from agentic.llm.provider import LLMProvider
from agentic.workflows import common

RAW_REQUIREMENT = (
    "Build a URL shortener service with core APIs, click analytics, and "
    "reliability features suitable for production."
)


def build_graph() -> WorkflowGraph:
    return common.build_canonical_graph()


expand_impl_tasks = common.expand_impl_tasks


def build_node_executors(
    provider: LLMProvider, prompts_dir: Path, workspace_root: Path, run_id: str
) -> tuple[dict[str, NodeExecutor], dict[str, NodeExecutor]]:
    return common.build_canonical_node_executors(
        provider, prompts_dir, workspace_root, run_id,
        requirement_text=RAW_REQUIREMENT,
        summary_label="greenfield baseline: complete, verified workspace",
    )
