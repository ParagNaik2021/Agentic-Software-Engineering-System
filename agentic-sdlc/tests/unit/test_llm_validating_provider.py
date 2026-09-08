"""P5 acceptance: a malformed mock response triggers exactly one repair
attempt then raises MalformedOutputError."""

import pytest
from pydantic import BaseModel

from agentic.llm.mock import MockProvider, ScriptedResponse
from agentic.llm.provider import MalformedOutputError, Message, ValidatingProvider, build_request


class _Schema(BaseModel):
    x: int


@pytest.mark.asyncio
async def test_malformed_response_triggers_one_repair_then_raises() -> None:
    req = build_request(
        system="sys", messages=[Message(role="user", content="give me x")],
        model="m", output_schema=_Schema,
    )
    mock = MockProvider(
        script={req.request_hash: [ScriptedResponse(content="{not valid json")]},
        default=ScriptedResponse(content="{still not valid json"),
    )
    provider = ValidatingProvider(mock)

    with pytest.raises(MalformedOutputError) as excinfo:
        await provider.complete(req)

    assert len(mock.calls) == 2  # original attempt + exactly one repair attempt
    assert "still not valid json" in excinfo.value.raw_content


@pytest.mark.asyncio
async def test_repair_attempt_that_succeeds_returns_parsed_output() -> None:
    req = build_request(
        system="sys", messages=[Message(role="user", content="give me x")],
        model="m", output_schema=_Schema,
    )
    mock = MockProvider(
        script={req.request_hash: [ScriptedResponse(content="{not valid json")]},
        default=ScriptedResponse(content='{"x": 42}'),
    )
    provider = ValidatingProvider(mock)

    response = await provider.complete(req)

    assert len(mock.calls) == 2
    assert isinstance(response.parsed, _Schema)
    assert response.parsed.x == 42


@pytest.mark.asyncio
async def test_valid_first_response_needs_no_repair() -> None:
    req = build_request(
        system="sys", messages=[Message(role="user", content="give me x")],
        model="m", output_schema=_Schema,
    )
    mock = MockProvider(default=ScriptedResponse(content='{"x": 7}'))
    provider = ValidatingProvider(mock)

    response = await provider.complete(req)

    assert len(mock.calls) == 1
    assert response.parsed.x == 7


@pytest.mark.asyncio
async def test_no_output_schema_skips_validation_entirely() -> None:
    req = build_request(system="sys", messages=[Message(role="user", content="hi")], model="m")
    mock = MockProvider(default=ScriptedResponse(content="plain text, not json at all"))
    provider = ValidatingProvider(mock)

    response = await provider.complete(req)

    assert len(mock.calls) == 1
    assert response.content == "plain text, not json at all"
    assert response.parsed is None


@pytest.mark.asyncio
async def test_repair_prompt_includes_the_validation_error_and_original_content() -> None:
    req = build_request(
        system="sys", messages=[Message(role="user", content="give me x")],
        model="m", output_schema=_Schema,
    )
    mock = MockProvider(
        script={req.request_hash: [ScriptedResponse(content="{not valid json")]},
        default=ScriptedResponse(content='{"x": 1}'),
    )
    provider = ValidatingProvider(mock)

    await provider.complete(req)

    repair_call = mock.calls[1]
    contents = [m.content for m in repair_call.messages]
    assert any("{not valid json" in c for c in contents)
    assert any("failed schema validation" in c for c in contents)
