"""Ambiguous workflow (Section 11.3): the scenario built specifically to
prove execution is non-linear and stateful, not a DAG that always runs
in the same order.

plan.decompose depends on [req.ambiguity, req.clarify] with
join_policy="any", so it runs immediately once req.ambiguity succeeds —
using the ambiguity agent's proposed defaults — without waiting for a
human to answer req.clarify. Only once the human's answer changes the
"clarification_answer" artifact does the *existing* replan machinery
(core/replan.py, wired since P2) invalidate plan.decompose, design.arch
and summary — every one of them structurally downstream of req.clarify
— and they re-execute against the answered spec.

Scoped short deliberately: this graph ends at summary rather than
running the full implementation/verification pipeline, since the P8
acceptance criterion this workflow exists for is specifically about
clarification -> invalidation -> re-execution, not about generating
more code (workflows/greenfield.py and workflows/brownfield.py already
demonstrate the full pipeline).
"""

from __future__ import annotations

from pathlib import Path

from agentic.agents.ambiguity import AmbiguityAgent
from agentic.agents.architect import ArchitectAgent
from agentic.agents.base import make_node_executor
from agentic.agents.planner import PlannerAgent
from agentic.agents.requirements import RequirementsAgent
from agentic.core.context import ContextView
from agentic.core.engine import NodeExecutionResult, NodeExecutor
from agentic.core.graph import WorkflowGraph
from agentic.core.models import NodeSpec, SDLCStage
from agentic.llm.provider import LLMProvider
from agentic.workflows import common

RAW_REQUIREMENT = "Make it faster and give us better analytics."

# The answer the recorded cassette (cassettes/ambiguous.json) expects —
# used as the CLI's default so `agentic run ambiguous --mode replay`
# works without an extra flag; override with a different answer only if
# you have re-recorded the cassette to match.
DEFAULT_CLARIFICATION_ANSWER = "p95 redirect latency under 50ms; add geographic and device breakdown"


def build_graph() -> WorkflowGraph:
    graph = WorkflowGraph(root="intake")
    graph.add_node(NodeSpec(node_id="intake", stage=SDLCStage.INTAKE))
    graph.add_node(
        NodeSpec(node_id="req.analyze", stage=SDLCStage.REQUIREMENTS, agent="requirements", depends_on=["intake"])
    )
    graph.add_node(
        NodeSpec(node_id="req.ambiguity", stage=SDLCStage.REQUIREMENTS, agent="ambiguity", depends_on=["req.analyze"])
    )
    graph.add_node(
        NodeSpec(
            node_id="req.clarify", stage=SDLCStage.CLARIFICATION, depends_on=["req.ambiguity"],
            requires_approval=True,
        )
    )
    graph.add_node(
        NodeSpec(
            node_id="plan.decompose", stage=SDLCStage.DECOMPOSITION, agent="planner",
            depends_on=["req.ambiguity", "req.clarify"], join_policy="any",
        )
    )
    graph.add_node(
        NodeSpec(
            node_id="design.arch", stage=SDLCStage.ARCHITECTURE, agent="architect", depends_on=["plan.decompose"]
        )
    )
    graph.add_node(NodeSpec(node_id="summary", stage=SDLCStage.SUMMARY, depends_on=["design.arch"]))
    graph.validate()
    return graph


def _intake_executor(run_id: str) -> NodeExecutor:
    async def _executor(node: NodeSpec, view: ContextView) -> NodeExecutionResult:
        artifact = common.make_artifact(
            "raw_requirement", {"text": RAW_REQUIREMENT}, node.node_id, run_id, kind="spec"
        )
        return NodeExecutionResult(artifacts=[artifact])

    return _executor


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
    provider: LLMProvider, prompts_dir: Path, run_id: str, clarification_answer: str
) -> dict[str, NodeExecutor]:
    requirements_agent = RequirementsAgent(provider, prompts_dir)
    ambiguity_agent = AmbiguityAgent(provider, prompts_dir)
    planner_agent = PlannerAgent(provider, prompts_dir)
    architect_agent = ArchitectAgent(provider, prompts_dir)

    return {
        "intake": _intake_executor(run_id),
        "req.analyze": make_node_executor(requirements_agent, run_id),
        "req.ambiguity": make_node_executor(ambiguity_agent, run_id),
        "req.clarify": clarify_executor(run_id, clarification_answer),
        "plan.decompose": make_node_executor(planner_agent, run_id),
        "design.arch": make_node_executor(architect_agent, run_id),
    }
