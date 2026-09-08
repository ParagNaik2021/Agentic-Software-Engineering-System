"""Real API calls (Section 9.1) — records every interaction to a
cassette keyed by request_hash, since live mode is what produces the
recordings replay mode plays back. The `anthropic` package is imported
lazily so replay/mock (and this whole codebase's test suite, which runs
with no network) never need it installed.
"""

from __future__ import annotations

import time

from agentic.core.models import TokenUsage
from agentic.llm.cassette import Cassette, CassetteEntry
from agentic.llm.provider import LLMProvider, LLMRequest, LLMResponse


class LiveProvider(LLMProvider):
    def __init__(self, api_key: str, cassette: Cassette | None = None) -> None:
        self.api_key = api_key
        self.cassette = cassette

    async def complete(self, req: LLMRequest) -> LLMResponse:
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError(
                "the 'anthropic' package is required for --mode live "
                "(pip install -e '.[live]')"
            ) from exc

        client = anthropic.AsyncAnthropic(api_key=self.api_key)
        start = time.monotonic()
        response = await client.messages.create(
            model=req.model,
            system=req.system,
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            messages=[{"role": m.role, "content": m.content} for m in req.messages],
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        content = "".join(block.text for block in response.content if hasattr(block, "text"))
        parsed = req.output_schema.model_validate_json(content) if req.output_schema else None
        tokens = TokenUsage(
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            total_tokens=response.usage.input_tokens + response.usage.output_tokens,
        )

        if self.cassette is not None:
            self.cassette.put(
                CassetteEntry(
                    request_hash=req.request_hash, content=content,
                    parsed=parsed.model_dump() if parsed is not None else None,
                    tokens=tokens, latency_ms=latency_ms, model=req.model,
                )
            )

        return LLMResponse(
            content=content, parsed=parsed, tokens=tokens, latency_ms=latency_ms,
            model=req.model, from_cassette=False,
        )
