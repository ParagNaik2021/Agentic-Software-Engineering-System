"""P6 acceptance: autonomy ceilings are enforced — the implementer
cannot write outside its task file scope, and the codebase_analyst
cannot write at all."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentic.agents.base import FileScopeViolation
from agentic.agents.codebase_analyst import CodebaseAnalystAgent
from agentic.agents.implementer import ImplementerAgent
from agentic.core.context import ContextView
from agentic.llm.mock import MockProvider, ScriptedResponse
from agentic.tools.fs import JailedFS

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "src" / "agentic" / "llm" / "prompts"


@pytest.mark.asyncio
async def test_implementer_writing_outside_file_scope_raises(tmp_path: Path) -> None:
    fixture = {
        "files": [{"path": "src/app/OUTSIDE_SCOPE.py", "content": "evil = True\n"}],
        "summary": "attempted out-of-scope write",
    }
    provider = MockProvider(default=ScriptedResponse(content=json.dumps(fixture)))
    fs = JailedFS(tmp_path)
    agent = ImplementerAgent(provider, PROMPTS_DIR, fs=fs)
    ctx = ContextView()
    plan = await agent.plan(ctx)

    with pytest.raises(FileScopeViolation):
        await agent.execute(
            ctx, plan, node_id="impl.T1", run_id="run-1",
            task_id="T1", file_scope=["src/app/models/link.py"],
        )

    # and the file must genuinely not have been written to disk
    assert not fs.exists("src/app/OUTSIDE_SCOPE.py")


@pytest.mark.asyncio
async def test_implementer_writing_within_file_scope_succeeds(tmp_path: Path) -> None:
    fixture = {
        "files": [{"path": "src/app/models/link.py", "content": "class Link:\n    pass\n"}],
        "summary": "in-scope write",
    }
    provider = MockProvider(default=ScriptedResponse(content=json.dumps(fixture)))
    fs = JailedFS(tmp_path)
    agent = ImplementerAgent(provider, PROMPTS_DIR, fs=fs)
    ctx = ContextView()
    plan = await agent.plan(ctx)

    result = await agent.execute(
        ctx, plan, node_id="impl.T1", run_id="run-1",
        task_id="T1", file_scope=["src/app/models/link.py"],
    )

    assert fs.exists("src/app/models/link.py")
    assert len(result.artifacts) == 1


@pytest.mark.asyncio
async def test_implementer_extra_execute_context_derives_scope_from_task_graph(tmp_path: Path) -> None:
    from agentic.core.models import Artifact, NodeSpec, SDLCStage, compute_content_hash

    fs = JailedFS(tmp_path)
    agent = ImplementerAgent(MockProvider(default=ScriptedResponse(content="{}")), PROMPTS_DIR, fs=fs)
    node = NodeSpec(node_id="impl.T1", stage=SDLCStage.IMPLEMENTATION)
    task_graph_payload = {"tasks": [{"task_id": "T1", "file_scope": ["src/app/models/link.py"]}]}
    view = ContextView(artifacts={
        "task_graph": Artifact(
            artifact_id="tg1", name="task_graph", kind="spec",
            content_hash=compute_content_hash(task_graph_payload), payload=task_graph_payload,
            produced_by_node="plan.decompose", produced_by_agent="planner", run_id="run-1",
            created_at=datetime.now(UTC),
        )
    })

    extra = agent.extra_execute_context(node, view)

    assert extra == {"task_id": "T1", "file_scope": ["src/app/models/link.py"]}


def test_codebase_analyst_has_no_filesystem_reference() -> None:
    """No JailedFS is ever passed to this agent, and its output schema
    (CodebaseAnalystOutput) has no field that could carry file content —
    there is no code path by which it could write, matching L0_OBSERVE's
    "no writes, no tools"."""
    agent = CodebaseAnalystAgent(MockProvider(default=ScriptedResponse(content="{}")), PROMPTS_DIR)
    assert not hasattr(agent, "fs")

    import inspect

    init_params = inspect.signature(CodebaseAnalystAgent.__init__).parameters
    assert "fs" not in init_params


def test_security_reviewer_has_no_filesystem_reference() -> None:
    from agentic.agents.security_reviewer import SecurityReviewerAgent

    agent = SecurityReviewerAgent(MockProvider(default=ScriptedResponse(content="{}")), PROMPTS_DIR)
    assert not hasattr(agent, "fs")
