"""Ambiguous workflow (Section 11.3): the canonical Section 5.1 graph,
entered through a deliberately underspecified requirement.

It is `common.build_canonical_graph(with_clarification=True)` — the exact
graph greenfield builds, plus the req.clarify checkpoint — so every
governance property greenfield has holds here too. That sharing is the
point: this workflow used to declare its own seven-node graph ending at
summary, which silently dropped design.review and release.readiness and
therefore violated GOV-001 (design acceptance and release readiness always
require human approval). A scenario cannot omit a mandatory gate if it
does not own the graph definition.

What is actually scenario-specific, and all that is:

  * RAW_REQUIREMENT — underspecified on purpose;
  * DEFAULT_CLARIFICATION_ANSWER and the req.clarify executor that turns
    a human's answer into the clarification_answer artifact;
  * the `any`-join on plan.decompose (supplied by with_clarification),
    which lets planning and the whole design fan-out run on the ambiguity
    agent's proposed defaults while req.clarify is still at its human
    checkpoint — and means answering it later invalidates plan.decompose,
    design.arch, design.data and design.api together through the ordinary
    input_hash path (core/replan.py, Engine._request_replan_if_staled).
"""

from __future__ import annotations

from pathlib import Path

from agentic.core.context import ContextView
from agentic.core.engine import NodeExecutionResult, NodeExecutor
from agentic.core.graph import WorkflowGraph
from agentic.core.models import NodeSpec
from agentic.llm.provider import LLMProvider
from agentic.workflows import common

RAW_REQUIREMENT = "Make it faster and give us better analytics."

# The answer the recorded cassette (cassettes/ambiguous.json) expects —
# used as the CLI's default so `agentic run ambiguous --mode replay`
# works without an extra flag; override with a different answer only if
# you have re-recorded the cassette to match.
DEFAULT_CLARIFICATION_ANSWER = "p95 redirect latency under 50ms; add geographic and device breakdown"


def build_graph() -> WorkflowGraph:
    return common.build_canonical_graph(with_clarification=True)


expand_impl_tasks = common.expand_impl_tasks


def clarify_executor(run_id: str, answer_text: str) -> NodeExecutor:
    """Runs only once req.clarify is approved (Section 6.2's asynchronous
    approval flow); the artifact it then produces is what makes the
    human's answer visible to input_hash recomputation for every
    downstream node — the trigger for re-planning."""

    async def _executor(node: NodeSpec, view: ContextView) -> NodeExecutionResult:
        artifact = common.make_artifact(
            "clarification_answer", {"text": answer_text}, node.node_id, run_id, kind="spec"
        )
        return NodeExecutionResult(artifacts=[artifact])

    return _executor


def build_node_executors(
    provider: LLMProvider,
    prompts_dir: Path,
    workspace_root: Path,
    run_id: str,
    clarification_answer: str,
) -> tuple[dict[str, NodeExecutor], dict[str, NodeExecutor]]:
    """The canonical executor set plus req.clarify's — identical wiring to
    greenfield for every other node, by construction."""
    node_executors, executors_by_agent = common.build_canonical_node_executors(
        provider, prompts_dir, workspace_root, run_id,
        requirement_text=RAW_REQUIREMENT,
        summary_label="ambiguous: clarified requirement implemented and verified",
    )
    node_executors["req.clarify"] = clarify_executor(run_id, clarification_answer)
    return node_executors, executors_by_agent
