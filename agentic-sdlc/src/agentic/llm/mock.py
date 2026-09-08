"""Scripted deterministic responses (Section 9.1), including
deliberately malformed ones — used for unit/integration tests and as the
inner provider fault injection wraps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentic.core.models import TokenUsage
from agentic.llm.provider import LLMProvider, LLMRequest, LLMResponse


@dataclass
class ScriptedResponse:
    content: str
    tokens: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0
    raise_error: Exception | None = None


class MockProvider(LLMProvider):
    """`script` maps request_hash -> a queue of responses popped in
    order, so a repair-attempt sequence (malformed, then valid) can be
    scripted precisely. `default` answers any hash with no script."""

    def __init__(
        self,
        script: dict[str, list[ScriptedResponse]] | None = None,
        default: ScriptedResponse | None = None,
    ) -> None:
        self.script: dict[str, list[ScriptedResponse]] = script or {}
        self.default = default
        self.calls: list[LLMRequest] = []

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls.append(req)
        queue = self.script.get(req.request_hash)
        if queue:
            scripted = queue.pop(0)
        elif self.default is not None:
            scripted = self.default
        else:
            raise KeyError(f"no scripted response for request_hash {req.request_hash}")

        if scripted.raise_error is not None:
            raise scripted.raise_error

        return LLMResponse(
            content=scripted.content, parsed=None, tokens=scripted.tokens,
            latency_ms=scripted.latency_ms, model=req.model, from_cassette=False,
        )
