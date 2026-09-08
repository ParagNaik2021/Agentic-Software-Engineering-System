"""Cassette playback (Section 9.1): the default demo mode — no key, no
network, no cost, reproducible metrics. Miss behaviour is configurable:
"error" (strict) or "live" (fall through and record a new interaction).
"""

from __future__ import annotations

from typing import Literal

from agentic.llm.cassette import Cassette
from agentic.llm.provider import LLMProvider, LLMRequest, LLMResponse


class ReplayMiss(Exception):
    pass


class ReplayProvider(LLMProvider):
    def __init__(
        self,
        cassette: Cassette,
        on_miss: Literal["error", "live"] = "error",
        live_provider: LLMProvider | None = None,
    ) -> None:
        self.cassette = cassette
        self.on_miss = on_miss
        self.live_provider = live_provider
        if on_miss == "live" and live_provider is None:
            raise ValueError("on_miss='live' requires a live_provider")

    async def complete(self, req: LLMRequest) -> LLMResponse:
        entry = self.cassette.get(req.request_hash)
        if entry is None:
            if self.on_miss == "live":
                assert self.live_provider is not None
                return await self.live_provider.complete(req)
            raise ReplayMiss(
                f"no cassette entry for request_hash {req.request_hash} "
                f"(node={req.node_id!r}); re-record in --mode live or fix the prompt"
            )

        parsed = None
        if req.output_schema is not None and entry.parsed is not None:
            parsed = req.output_schema.model_validate(entry.parsed)

        return LLMResponse(
            content=entry.content, parsed=parsed, tokens=entry.tokens,
            latency_ms=entry.latency_ms, model=entry.model, from_cassette=True,
        )
