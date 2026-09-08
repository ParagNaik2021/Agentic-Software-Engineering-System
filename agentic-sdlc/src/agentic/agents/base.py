"""Agent ABC (Section 8.1): every agent implements the same three-phase
contract — plan/execute/validate — so exit gates can reason about output
quality uniformly across agents regardless of what each one actually
does.

execute() is a concrete template method: it renders the agent's prompt,
calls the LLM through a ValidatingProvider (so schema validation and the
one-shot repair from Section 9.2 always apply), and hands the parsed,
schema-valid output to the subclass's _to_artifacts_and_decisions hook —
the only place agent-specific logic lives. Subclasses that mutate the
workspace (implementer, test_engineer, technical_writer) also enforce
their declared file scope there, which is what makes "autonomy ceilings
are enforced" a property of the code rather than the prompt.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

from agentic.core.context import ContextView
from agentic.core.models import Artifact as CoreArtifact
from agentic.core.models import AutonomyLevel, Decision, NodeSpec, TokenUsage
from agentic.governance.autonomy import AGENT_CEILINGS
from agentic.llm.provider import LLMProvider, Message, ValidatingProvider, build_request
from agentic.llm.schemas import render_prompt


@dataclass
class AgentPlan:
    steps: list[str]
    tools: list[str] = field(default_factory=list)


@dataclass
class ToolCall:
    tool: str
    args: dict = field(default_factory=dict)
    result_summary: str = ""


@dataclass
class AgentResult:
    artifacts: list[CoreArtifact] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tokens: TokenUsage = field(default_factory=TokenUsage)


@dataclass
class ValidationReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def within_scope(path: str, scope: list[str]) -> bool:
    """True if `path` (relative) falls inside a declared file scope
    entry — either an exact filename or a directory prefix (entries
    ending in '/')."""
    norm_path = path.replace("\\", "/")
    for allowed in scope:
        allowed_norm = allowed.replace("\\", "/")
        if allowed_norm.endswith("/"):
            if norm_path.startswith(allowed_norm):
                return True
        elif norm_path == allowed_norm:
            return True
    return False


class FileScopeViolation(Exception):
    pass


class ContractBreach(Exception):
    """Raised when an agent's own validate() rejects its result —
    Section 6.4's CONTRACT_BREACH error class."""


class Agent(ABC):
    name: ClassVar[str]
    max_autonomy: ClassVar[AutonomyLevel]
    input_contract: ClassVar[list[str]]
    output_contract: ClassVar[list[str]]
    output_schema: ClassVar[type[BaseModel]]
    prompt_template: ClassVar[str]

    def __init__(
        self, provider: LLMProvider, prompts_dir: Path, model: str = "claude-sonnet-5"
    ) -> None:
        declared = AGENT_CEILINGS.get(self.name)
        if declared is not None and declared != self.max_autonomy:
            raise ValueError(
                f"{self.name}.max_autonomy ({self.max_autonomy.name}) does not match the "
                f"governance autonomy matrix ({declared.name}) — the two must never drift apart"
            )
        self.provider = provider if isinstance(provider, ValidatingProvider) else ValidatingProvider(provider)
        self.prompts_dir = prompts_dir
        self.model = model

    async def plan(self, ctx: ContextView) -> AgentPlan:
        return AgentPlan(
            steps=[f"invoke {self.name} via the LLM provider", "convert structured output into artifacts/decisions"],
            tools=["llm"],
        )

    async def execute(self, ctx: ContextView, plan: AgentPlan, **context: object) -> AgentResult:
        system = render_prompt(self.name, self.prompts_dir)
        user_content = self._build_user_message(ctx, **context)
        request = build_request(
            system=system,
            messages=[Message(role="user", content=user_content)],
            model=self.model,
            output_schema=self.output_schema,
            node_id=context.get("node_id"),  # type: ignore[arg-type]
        )
        response = await self.provider.complete(request)
        assert response.parsed is not None, (
            "ValidatingProvider guarantees a schema-valid parsed response or raises "
            "MalformedOutputError; this should be unreachable"
        )
        artifacts, decisions = self._to_artifacts_and_decisions(response.parsed, ctx, **context)
        return AgentResult(artifacts=artifacts, decisions=decisions, tokens=response.tokens)

    async def validate(self, result: AgentResult, ctx: ContextView) -> ValidationReport:
        produced_names = {a.name for a in result.artifacts}
        missing = [n for n in self.output_contract if n not in produced_names]
        if missing:
            return ValidationReport(ok=False, errors=[f"missing declared output artifacts: {missing}"])
        return ValidationReport(ok=True)

    def extra_execute_context(self, node: NodeSpec, view: ContextView) -> dict:
        """Hook for agents that need per-node context beyond node_id/
        run_id (e.g. the implementer's file_scope for this specific
        impl.<task_id> node). Default: nothing extra."""
        return {}

    @abstractmethod
    def _build_user_message(self, ctx: ContextView, **context: object) -> str: ...

    @abstractmethod
    def _to_artifacts_and_decisions(
        self, parsed: BaseModel, ctx: ContextView, **context: object
    ) -> tuple[list[CoreArtifact], list[Decision]]: ...


def make_node_executor(agent: Agent, run_id: str):
    """Agent registry wiring: adapts an Agent onto the engine's
    NodeExecutor signature (node, view) -> NodeExecutionResult, running
    plan -> execute -> validate and raising ContractBreach if the
    agent's own validation rejects its result."""
    from agentic.core.engine import NodeExecutionResult

    async def _executor(node: NodeSpec, view: ContextView) -> NodeExecutionResult:
        extra = agent.extra_execute_context(node, view)
        plan = await agent.plan(view)
        result = await agent.execute(view, plan, node_id=node.node_id, run_id=run_id, **extra)
        report = await agent.validate(result, view)
        if not report.ok:
            raise ContractBreach(f"{agent.name} failed self-validation: {report.errors}")
        return NodeExecutionResult(artifacts=result.artifacts, decisions=result.decisions)

    return _executor
