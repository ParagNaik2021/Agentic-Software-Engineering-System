"""LLMProvider interface and request/response contracts (Section 9.1).

request_hash is what makes replay mode meaningful: it is computed from
every input that could change the model's answer (system prompt,
messages, requested output schema, model) so a cassette lookup can key
on it and a stale cassette (e.g. after a prompt edit) misses cleanly
instead of silently returning the wrong answer.

Fault injection needs to know which node issued a call without changing
the ABC's single-argument `complete(req)` signature, so `node_id` rides
along on the request as routing metadata; it deliberately does not
participate in request_hash, since two nodes issuing the identical
prompt should still share a cassette entry.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentic.core.models import TokenUsage


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


def compute_request_hash(
    system: str, messages: list[Message], output_schema: type[BaseModel] | None, model: str
) -> str:
    schema_repr = output_schema.model_json_schema() if output_schema is not None else None
    canonical = json.dumps(
        {
            "system": system,
            "messages": [m.model_dump() for m in messages],
            "schema": schema_repr,
            "model": model,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LLMRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    system: str
    messages: list[Message]
    output_schema: type[BaseModel] | None = None
    model: str
    temperature: float = 0.2
    max_tokens: int = 4096
    request_hash: str
    node_id: str | None = None


def build_request(
    system: str,
    messages: list[Message],
    model: str,
    output_schema: type[BaseModel] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    node_id: str | None = None,
) -> LLMRequest:
    return LLMRequest(
        system=system,
        messages=messages,
        output_schema=output_schema,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        request_hash=compute_request_hash(system, messages, output_schema, model),
        node_id=node_id,
    )


class LLMResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    content: str
    parsed: BaseModel | None = None
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = 0
    model: str
    from_cassette: bool = False


class LLMProvider(ABC):
    @abstractmethod
    async def complete(self, req: LLMRequest) -> LLMResponse: ...


# ---------------------------------------------------------------------
# Structured output validation and repair (Section 9.2)
# ---------------------------------------------------------------------


class MalformedOutputError(Exception):
    def __init__(self, message: str, raw_content: str) -> None:
        self.raw_content = raw_content
        super().__init__(message)


class ValidatingProvider(LLMProvider):
    """Wraps any LLMProvider: validates response.content against
    req.output_schema, and on failure makes exactly one bounded repair
    attempt — resending the malformed output with the specific
    validation error and an instruction to return corrected JSON only.
    A second failure raises MalformedOutputError, which RecoveryManager
    classifies as MALFORMED_OUTPUT (governance/recovery.py)."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner

    async def complete(self, req: LLMRequest) -> LLMResponse:
        if req.output_schema is None:
            return await self.inner.complete(req)

        response = await self.inner.complete(req)
        parsed, error = self._try_parse(response.content, req.output_schema)
        if parsed is not None:
            response.parsed = parsed
            return response

        repair_req = build_request(
            system=req.system,
            messages=[
                *req.messages,
                Message(role="assistant", content=response.content),
                Message(
                    role="user",
                    content=(
                        f"That response failed schema validation: {error}. "
                        "Return corrected JSON only, matching the schema exactly."
                    ),
                ),
            ],
            model=req.model,
            output_schema=req.output_schema,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            node_id=req.node_id,
        )
        repaired = await self.inner.complete(repair_req)
        parsed, error = self._try_parse(repaired.content, req.output_schema)
        if parsed is not None:
            repaired.parsed = parsed
            return repaired

        raise MalformedOutputError(
            f"schema validation failed after one repair attempt: {error}",
            raw_content=repaired.content,
        )

    @staticmethod
    def _try_parse(content: str, schema: type[BaseModel]) -> tuple[BaseModel | None, str | None]:
        try:
            return schema.model_validate_json(content), None
        except Exception as exc:  # pydantic ValidationError, JSONDecodeError, ...
            return None, str(exc)


# ---------------------------------------------------------------------
# Fault injection (Section 9.3)
# ---------------------------------------------------------------------


class TransientProviderError(Exception):
    pass


class QualityFaultError(Exception):
    """Signals a fault of ErrorClass.QUALITY_FAILURE for the recovery
    path to classify, as distinct from a raw transient error."""


@dataclass
class FaultProfile:
    fail_on_node: str | None = None
    kind: Literal["transient", "malformed", "quality"] = "transient"
    count: int = 1
    persist: bool = False
    latency_ms: int = 0


class FaultInjectingProvider(LLMProvider):
    """Deterministic fault injection so retry/fallback/rollback/safe-stop
    can be demonstrated on demand rather than hoped for (Section 9.3).
    `count` faults are injected then calls pass through normally, unless
    `persist` is set, in which case the fault recurs indefinitely."""

    def __init__(self, inner: LLMProvider, profile: FaultProfile) -> None:
        self.inner = inner
        self.profile = profile
        self.triggered = 0

    def _should_fault(self, req: LLMRequest) -> bool:
        profile = self.profile
        if profile.fail_on_node is not None and profile.fail_on_node != req.node_id:
            return False
        return profile.persist or self.triggered < profile.count

    async def complete(self, req: LLMRequest) -> LLMResponse:
        if self._should_fault(req):
            self.triggered += 1
            if self.profile.kind == "transient":
                raise TransientProviderError(
                    f"injected transient fault for node {req.node_id!r} (attempt {self.triggered})"
                )
            if self.profile.kind == "quality":
                raise QualityFaultError(
                    f"injected quality fault for node {req.node_id!r} (attempt {self.triggered})"
                )
            # kind == "malformed": return a syntactically broken response
            # rather than raising, so ValidatingProvider's repair path is
            # what has to handle it.
            return LLMResponse(
                content="{not-valid-json", parsed=None, tokens=TokenUsage(),
                latency_ms=self.profile.latency_ms, model=req.model, from_cassette=False,
            )
        return await self.inner.complete(req)
