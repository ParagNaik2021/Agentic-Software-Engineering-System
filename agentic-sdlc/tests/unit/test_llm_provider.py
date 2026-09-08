"""LLMRequest/Response contracts: request_hash determinism (Section 9.1)."""

from pydantic import BaseModel

from agentic.llm.provider import Message, build_request, compute_request_hash


class _Schema(BaseModel):
    x: int


def test_identical_requests_hash_identically() -> None:
    messages = [Message(role="user", content="hello")]
    h1 = compute_request_hash("sys", messages, _Schema, "model-a")
    h2 = compute_request_hash("sys", messages, _Schema, "model-a")
    assert h1 == h2


def test_different_system_prompt_changes_hash() -> None:
    messages = [Message(role="user", content="hello")]
    h1 = compute_request_hash("sys-1", messages, _Schema, "model-a")
    h2 = compute_request_hash("sys-2", messages, _Schema, "model-a")
    assert h1 != h2


def test_different_messages_change_hash() -> None:
    h1 = compute_request_hash("sys", [Message(role="user", content="a")], None, "model-a")
    h2 = compute_request_hash("sys", [Message(role="user", content="b")], None, "model-a")
    assert h1 != h2


def test_different_schema_changes_hash() -> None:
    class _OtherSchema(BaseModel):
        y: str

    messages = [Message(role="user", content="hello")]
    h1 = compute_request_hash("sys", messages, _Schema, "model-a")
    h2 = compute_request_hash("sys", messages, _OtherSchema, "model-a")
    assert h1 != h2


def test_different_model_changes_hash() -> None:
    messages = [Message(role="user", content="hello")]
    h1 = compute_request_hash("sys", messages, None, "model-a")
    h2 = compute_request_hash("sys", messages, None, "model-b")
    assert h1 != h2


def test_build_request_populates_request_hash() -> None:
    req = build_request(system="sys", messages=[Message(role="user", content="hi")], model="m")
    assert req.request_hash == compute_request_hash("sys", req.messages, None, "m")


def test_node_id_does_not_affect_request_hash() -> None:
    messages = [Message(role="user", content="hello")]
    req_a = build_request(system="sys", messages=messages, model="m", node_id="node-a")
    req_b = build_request(system="sys", messages=messages, model="m", node_id="node-b")
    assert req_a.request_hash == req_b.request_hash
