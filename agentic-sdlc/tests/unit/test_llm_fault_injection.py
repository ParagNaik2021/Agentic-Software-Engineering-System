"""P5 acceptance: fault injection produces the configured failure at the
configured node (Section 9.3)."""

import pytest
from pydantic import BaseModel

from agentic.llm.mock import MockProvider, ScriptedResponse
from agentic.llm.provider import (
    FaultInjectingProvider,
    FaultProfile,
    Message,
    QualityFaultError,
    TransientProviderError,
    ValidatingProvider,
    build_request,
)


class _Schema(BaseModel):
    x: int


def _req(node_id: str | None):
    return build_request(
        system="sys", messages=[Message(role="user", content="hi")], model="m", node_id=node_id
    )


@pytest.mark.asyncio
async def test_transient_fault_triggers_configured_count_then_passes_through() -> None:
    inner = MockProvider(default=ScriptedResponse(content="ok"))
    provider = FaultInjectingProvider(
        inner, FaultProfile(fail_on_node="impl.core_service", kind="transient", count=2)
    )

    with pytest.raises(TransientProviderError):
        await provider.complete(_req("impl.core_service"))
    with pytest.raises(TransientProviderError):
        await provider.complete(_req("impl.core_service"))

    response = await provider.complete(_req("impl.core_service"))  # 3rd call: fault budget spent
    assert response.content == "ok"
    assert len(inner.calls) == 1  # only the successful call reached the inner provider


@pytest.mark.asyncio
async def test_fault_does_not_trigger_on_a_different_node() -> None:
    inner = MockProvider(default=ScriptedResponse(content="ok"))
    provider = FaultInjectingProvider(
        inner, FaultProfile(fail_on_node="impl.core_service", kind="transient", count=5)
    )

    response = await provider.complete(_req("verify.unit"))

    assert response.content == "ok"
    assert provider.triggered == 0


@pytest.mark.asyncio
async def test_fault_with_no_node_filter_applies_everywhere() -> None:
    inner = MockProvider(default=ScriptedResponse(content="ok"))
    provider = FaultInjectingProvider(inner, FaultProfile(fail_on_node=None, kind="transient", count=1))

    with pytest.raises(TransientProviderError):
        await provider.complete(_req("any.node"))
    response = await provider.complete(_req("any.other.node"))
    assert response.content == "ok"


@pytest.mark.asyncio
async def test_persistent_quality_fault_never_lets_calls_through() -> None:
    inner = MockProvider(default=ScriptedResponse(content="ok"))
    provider = FaultInjectingProvider(
        inner, FaultProfile(fail_on_node="verify.unit", kind="quality", persist=True)
    )

    for _ in range(5):
        with pytest.raises(QualityFaultError):
            await provider.complete(_req("verify.unit"))
    assert len(inner.calls) == 0


@pytest.mark.asyncio
async def test_malformed_fault_feeds_the_repair_path_instead_of_raising() -> None:
    inner = MockProvider(default=ScriptedResponse(content='{"x": 1}'))
    fault_provider = FaultInjectingProvider(
        inner, FaultProfile(fail_on_node="req.analyze", kind="malformed", count=1)
    )
    validating = ValidatingProvider(fault_provider)

    req = build_request(
        system="sys", messages=[Message(role="user", content="hi")], model="m",
        output_schema=_Schema, node_id="req.analyze",
    )
    response = await validating.complete(req)

    # first call hit the injected malformed fault; the repair attempt's
    # fault budget (count=1) is spent, so it reaches the inner mock and succeeds
    assert response.parsed.x == 1
    assert len(inner.calls) == 1
