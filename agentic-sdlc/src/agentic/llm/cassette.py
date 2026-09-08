"""Cassette record/serialise (Section 9.1): a JSON file under cassettes/
mapping request_hash -> the recorded interaction, so replay mode needs
no network, no key and no cost.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from agentic.core.models import TokenUsage


class CassetteEntry(BaseModel):
    request_hash: str
    content: str
    parsed: dict | None = None
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = 0
    model: str


class Cassette:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, CassetteEntry] = {}
        if path.exists():
            self._load()

    def _load(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._entries = {h: CassetteEntry.model_validate(raw) for h, raw in data.items()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {h: e.model_dump() for h, e in self._entries.items()}
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str), encoding="utf-8")

    def get(self, request_hash: str) -> CassetteEntry | None:
        return self._entries.get(request_hash)

    def put(self, entry: CassetteEntry) -> None:
        self._entries[entry.request_hash] = entry
        self._save()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, request_hash: str) -> bool:
        return request_hash in self._entries
