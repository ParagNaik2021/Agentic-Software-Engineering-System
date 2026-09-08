"""LiveProvider without the optional 'anthropic' dependency installed:
must fail clearly rather than with an unrelated ImportError, and must
never be needed by any other test in this offline suite."""

import pytest

from agentic.llm.live import LiveProvider
from agentic.llm.provider import Message, build_request


@pytest.mark.asyncio
async def test_live_provider_raises_clear_error_without_anthropic_installed() -> None:
    provider = LiveProvider(api_key="sk-fake")
    req = build_request(system="sys", messages=[Message(role="user", content="hi")], model="m")

    with pytest.raises(RuntimeError, match="anthropic"):
        await provider.complete(req)
