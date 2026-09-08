"""Cassette record/serialise round trip."""

from pathlib import Path

from agentic.core.models import TokenUsage
from agentic.llm.cassette import Cassette, CassetteEntry


def _entry(h: str = "hash-1") -> CassetteEntry:
    return CassetteEntry(
        request_hash=h, content='{"x": 1}', parsed={"x": 1},
        tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        latency_ms=250, model="claude-sonnet-5",
    )


def test_put_then_get_round_trips(tmp_path: Path) -> None:
    cassette = Cassette(tmp_path / "c.json")
    cassette.put(_entry())

    fetched = cassette.get("hash-1")
    assert fetched is not None
    assert fetched.content == '{"x": 1}'
    assert fetched.tokens.total_tokens == 15


def test_missing_hash_returns_none(tmp_path: Path) -> None:
    cassette = Cassette(tmp_path / "c.json")
    assert cassette.get("nope") is None


def test_cassette_persists_to_disk_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    cassette = Cassette(path)
    cassette.put(_entry())

    reloaded = Cassette(path)
    assert len(reloaded) == 1
    assert "hash-1" in reloaded
    assert reloaded.get("hash-1").content == '{"x": 1}'
