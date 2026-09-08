"""P5 acceptance: replay returns byte-identical responses for identical
request hashes."""

from pathlib import Path

import pytest

from agentic.core.models import TokenUsage
from agentic.llm.cassette import Cassette, CassetteEntry
from agentic.llm.provider import Message, build_request
from agentic.llm.replay import ReplayMiss, ReplayProvider


@pytest.mark.asyncio
async def test_replay_returns_byte_identical_response_for_identical_hash(tmp_path: Path) -> None:
    cassette = Cassette(tmp_path / "c.json")
    req = build_request(system="sys", messages=[Message(role="user", content="hi")], model="m")
    cassette.put(
        CassetteEntry(
            request_hash=req.request_hash, content='{"answer": 42}', parsed=None,
            tokens=TokenUsage(prompt_tokens=3, completion_tokens=4, total_tokens=7),
            latency_ms=123, model="m",
        )
    )
    provider = ReplayProvider(cassette)

    r1 = await provider.complete(req)
    r2 = await provider.complete(req)

    assert r1.content == r2.content == '{"answer": 42}'
    assert r1.tokens == r2.tokens
    assert r1.latency_ms == r2.latency_ms == 123
    assert r1.from_cassette is True and r2.from_cassette is True


@pytest.mark.asyncio
async def test_replay_miss_raises_by_default(tmp_path: Path) -> None:
    cassette = Cassette(tmp_path / "c.json")
    provider = ReplayProvider(cassette, on_miss="error")
    req = build_request(system="sys", messages=[Message(role="user", content="hi")], model="m")

    with pytest.raises(ReplayMiss):
        await provider.complete(req)


@pytest.mark.asyncio
async def test_replay_miss_falls_through_to_live_when_configured(tmp_path: Path) -> None:
    from agentic.llm.mock import MockProvider, ScriptedResponse

    cassette = Cassette(tmp_path / "c.json")
    req = build_request(system="sys", messages=[Message(role="user", content="hi")], model="m")
    fallback = MockProvider(default=ScriptedResponse(content="from live fallback"))
    provider = ReplayProvider(cassette, on_miss="live", live_provider=fallback)

    response = await provider.complete(req)

    assert response.content == "from live fallback"
    assert len(fallback.calls) == 1


def test_on_miss_live_requires_a_live_provider(tmp_path: Path) -> None:
    cassette = Cassette(tmp_path / "c.json")
    with pytest.raises(ValueError):
        ReplayProvider(cassette, on_miss="live", live_provider=None)


@pytest.mark.asyncio
async def test_different_request_hashes_are_independent(tmp_path: Path) -> None:
    cassette = Cassette(tmp_path / "c.json")
    req_a = build_request(system="sys-a", messages=[Message(role="user", content="hi")], model="m")
    req_b = build_request(system="sys-b", messages=[Message(role="user", content="hi")], model="m")
    cassette.put(CassetteEntry(request_hash=req_a.request_hash, content="A", model="m"))
    cassette.put(CassetteEntry(request_hash=req_b.request_hash, content="B", model="m"))
    provider = ReplayProvider(cassette)

    assert (await provider.complete(req_a)).content == "A"
    assert (await provider.complete(req_b)).content == "B"
