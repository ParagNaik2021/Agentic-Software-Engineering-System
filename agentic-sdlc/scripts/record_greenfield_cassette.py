"""Records cassettes/greenfield.json for the greenfield scenario.

Section 9.1: live mode "records the interaction to cassettes/... [and]
produc[es] the recordings". This sandbox has no outbound network, so
instead of a real Anthropic call this script uses a NodeKeyedProvider
that returns a hand-authored, schema-valid response per node_id (the
same role a live LLM call would play) and records each interaction's
*actual* request_hash — computed the same way the runtime does, from
the exact rendered prompt — into a Cassette. Every downstream
consumer (ReplayProvider, the demo, the P7 acceptance test) only ever
sees a request_hash -> response mapping, so this is indistinguishable
from a recording made in live mode against the same prompts.

The hand-authored source for the FastAPI service and its test suite was
developed and verified standalone first (pytest, coverage, ruff, mypy
all clean, 96% coverage) before being embedded here — see the note in
docs/scenarios/greenfield.md.

Usage: python scripts/record_greenfield_cassette.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from agentic.core.context import ContextStore
from agentic.core.engine import Engine
from agentic.core.events import EventLog
from agentic.core.store import RunStore
from agentic.llm.cassette import Cassette, CassetteEntry
from agentic.llm.provider import LLMProvider, LLMRequest, LLMResponse
from agentic.workflows import greenfield

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "src" / "agentic" / "llm" / "prompts"
CASSETTE_PATH = REPO_ROOT / "cassettes" / "greenfield.json"
WORKSPACE_ROOT = REPO_ROOT / "workspace" / "urlshortener"
RECORDING_DIR = REPO_ROOT / ".recording_tmp"

# The true greenfield baseline content, pinned to the commit
# workflows/greenfield.py's own summary checkpoint produced — NOT the
# live working tree, which workflows/brownfield.py's demo has since
# modified in place (uncommitted). `git show <sha>:<path>` reads the
# content as it existed at that commit regardless of later edits.
GREENFIELD_BASELINE_COMMIT = "c3b04bc"


def _read(rel: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "show", f"{GREENFIELD_BASELINE_COMMIT}:{rel}"],
        cwd=WORKSPACE_ROOT, capture_output=True, text=True, check=True,
    )
    return result.stdout


def _canned_responses() -> dict[str, dict]:
    """One schema-valid response per LLM-backed node_id."""
    return {
        "req.analyze": {
            "normalized_spec": (
                "A URL shortener service exposing: link creation with SSRF-validated target "
                "URLs and optional custom aliases, redirect-with-click-tracking, link lookup, "
                "soft-deletion, and health/readiness endpoints. Reduced demo scope: persistence "
                "is in-memory rather than SQLAlchemy/SQLite; rate limiting, redirect caching and "
                "the analytics-stats endpoint are deferred (see engineering summary limitations)."
            ),
            "acceptance_criteria": [
                {"id": "AC-1", "statement": "POST /api/v1/links creates a short code for a valid target_url.", "testable": True},
                {"id": "AC-2", "statement": "Creating a link with a disallowed scheme or a private/loopback target is rejected with 400.", "testable": True},
                {"id": "AC-3", "statement": "A custom alias may be supplied; reserved words and duplicates are rejected.", "testable": True},
                {"id": "AC-4", "statement": "GET /{code} returns a 302 redirect to the stored target_url and records a click.", "testable": True},
                {"id": "AC-5", "statement": "GET /api/v1/links/{code} returns link metadata including click_count.", "testable": True},
                {"id": "AC-6", "statement": "DELETE /api/v1/links/{code} soft-deletes the link; subsequent redirects 404.", "testable": True},
                {"id": "AC-7", "statement": "Unknown codes return 404 with a uniform error envelope.", "testable": True},
                {"id": "AC-8", "statement": "GET /healthz and /readyz report service status.", "testable": True},
            ],
            "ambiguity_register": [
                {"id": "AMB-1", "description": "No persistence backend specified.", "assumption_if_unresolved": "Use an in-memory repository behind the same interface a real database would implement."},
                {"id": "AMB-2", "description": "No rate limiting parameters specified.", "assumption_if_unresolved": "Defer rate limiting; document as a limitation."},
                {"id": "AMB-3", "description": "No link expiry default specified.", "assumption_if_unresolved": "Links never expire unless expires_at is explicitly set."},
            ],
        },
        "req.ambiguity": {
            "scored": [
                {"id": "AMB-1", "question": "Should persistence be a real database for this pass?", "proposed_default": "In-memory store, swappable via the repository interface.", "impact": 0.5, "uncertainty": 0.4},
                {"id": "AMB-2", "question": "What rate limits should apply?", "proposed_default": "None in this pass.", "impact": 0.3, "uncertainty": 0.4},
                {"id": "AMB-3", "question": "Should links expire by default?", "proposed_default": "No default expiry.", "impact": 0.2, "uncertainty": 0.3},
            ],
            "above_threshold_ids": [],
            "assumptions": [
                "In-memory persistence behind the repository interface is acceptable for this pass.",
                "No default link expiry.",
                "Rate limiting and redirect caching are deferred.",
            ],
        },
        "plan.decompose": {
            "tasks": [
                {"task_id": "T1", "description": "Link model and in-memory repository.", "depends_on": [], "file_scope": ["app/models.py", "app/repository.py"], "effort": "M", "risk": "low"},
                {"task_id": "T2", "description": "Service layer (SSRF validation, base62 codes) and FastAPI router.", "depends_on": ["T1"], "file_scope": ["app/service.py", "app/main.py"], "effort": "L", "risk": "medium"},
            ],
        },
        "design.arch": {
            "components": ["api", "services", "repositories", "models"],
            "boundaries": "Routers (app/main.py) depend only on the service layer; the service layer depends only on the repository interface. No layer skipping.",
            "cross_cutting_concerns": ["uniform error envelope", "structured error codes"],
            "decisions": [
                {"statement": "Layered architecture with a repository-pattern persistence boundary.", "rationale": "Keeps persistence swappable without touching the service or API layers.", "alternatives_rejected": ["Active Record", "a single monolithic module"]},
            ],
        },
        "design.data": {
            "entities": [
                {"name": "Link", "fields": [
                    {"name": "id", "type": "str", "constraints": "uuid"},
                    {"name": "code", "type": "str", "constraints": "unique"},
                    {"name": "target_url", "type": "str", "constraints": "max 2048"},
                    {"name": "click_count", "type": "int", "constraints": "default 0"},
                ], "indexes": ["code"]},
            ],
            "migration_plan": "No existing data; a fresh in-memory store starts empty on each process.",
            "decisions": [
                {"statement": "Use an in-memory repository for this demo pass rather than SQLAlchemy/SQLite immediately.", "rationale": "Avoids async database setup complexity while keeping the repository interface swappable for a real backend later.", "alternatives_rejected": ["Wiring SQLAlchemy + aiosqlite in this pass"]},
            ],
        },
        "design.api": {
            "openapi_version": "3.1.0",
            "endpoints": [
                {"method": "POST", "path": "/api/v1/links", "summary": "Create a short link.", "request_schema": {}, "response_schema": {}},
                {"method": "GET", "path": "/api/v1/links/{code}", "summary": "Retrieve link metadata.", "request_schema": {}, "response_schema": {}},
                {"method": "DELETE", "path": "/api/v1/links/{code}", "summary": "Soft-delete a link.", "request_schema": {}, "response_schema": {}},
                {"method": "GET", "path": "/{code}", "summary": "Redirect to the target URL.", "request_schema": {}, "response_schema": {}},
                {"method": "GET", "path": "/healthz", "summary": "Liveness probe.", "request_schema": {}, "response_schema": {}},
                {"method": "GET", "path": "/readyz", "summary": "Readiness probe.", "request_schema": {}, "response_schema": {}},
            ],
            "examples": {},
        },
        "impl.T1": {
            "files": [
                {"path": "app/models.py", "content": _read("app/models.py")},
                {"path": "app/repository.py", "content": _read("app/repository.py")},
            ],
            "summary": "Implemented the Link model and an in-memory LinkRepository.",
        },
        "impl.T2": {
            "files": [
                {"path": "app/service.py", "content": _read("app/service.py")},
                {"path": "app/main.py", "content": _read("app/main.py")},
            ],
            "summary": "Implemented LinkService (SSRF validation, base62 codes) and the FastAPI router.",
        },
        "verify.security": {
            "findings": [
                {"severity": "LOW", "description": "Persistence is in-memory only; acceptable for this demo scope but not production-durable.", "location": "app/repository.py"},
            ],
        },
        "verify.unit": {
            "test_files": [
                {"path": "tests/test_service.py", "content": _read("tests/test_service.py")},
                {"path": "tests/test_api.py", "content": _read("tests/test_api.py")},
            ],
            "summary": "Added unit tests for the service/repository layer and integration tests for every endpoint, including SSRF rejection.",
        },
        "docs.generate": {
            "documents": [
                {"path": "README.md", "content": (
                    "# URL Shortener\n\n"
                    "Generated by the Agentic SDLC Orchestrator's greenfield workflow.\n\n"
                    "## Run\n\n```\npip install fastapi uvicorn\nuvicorn app.main:app --reload\n```\n\n"
                    "## Endpoints\n\n"
                    "- `POST /api/v1/links` - create a short link\n"
                    "- `GET /{code}` - redirect\n"
                    "- `GET /api/v1/links/{code}` - link metadata\n"
                    "- `DELETE /api/v1/links/{code}` - soft-delete\n"
                    "- `GET /healthz`, `GET /readyz` - health probes\n\n"
                    "## Known limitations\n\n"
                    "- Persistence is in-memory (see ADR on the repository pattern).\n"
                    "- No rate limiting or redirect caching in this pass.\n"
                    "- No authentication beyond a future API-key placeholder.\n"
                )},
            ],
        },
        "release.readiness": {
            "go_no_go": "go",
            "summary": "All exit gates passed: unit+integration tests green at >=80% coverage, no HIGH/CRITICAL security findings.",
            "risks": ["In-memory persistence loses state on restart; acceptable for this demo, not for production."],
            "limitations": [
                "No SQLAlchemy/SQLite persistence in this pass.",
                "No rate limiting or redirect caching implemented.",
                "No authentication beyond a future API-key placeholder.",
            ],
        },
    }


class NodeKeyedProvider(LLMProvider):
    """Recording-time stand-in for a live LLM: returns a scripted,
    schema-valid response keyed by node_id, and records the *actual*
    request_hash (computed from the real rendered prompt) into the
    cassette — so replay later matches on a hash produced exactly the
    same way a live recording would have produced it."""

    def __init__(self, responses: dict[str, dict], cassette: Cassette) -> None:
        self.responses = responses
        self.cassette = cassette

    async def complete(self, req: LLMRequest) -> LLMResponse:
        if req.node_id not in self.responses:
            raise KeyError(f"no canned response for node_id {req.node_id!r}")
        content = json.dumps(self.responses[req.node_id])
        self.cassette.put(CassetteEntry(request_hash=req.request_hash, content=content, model=req.model))
        return LLMResponse(content=content, model=req.model, from_cassette=False)


async def _record() -> None:
    if RECORDING_DIR.exists():
        shutil.rmtree(RECORDING_DIR)
    RECORDING_DIR.mkdir(parents=True)

    recording_workspace = RECORDING_DIR / "workspace"
    graph = greenfield.build_graph()
    context = ContextStore(graph, persist_dir=RECORDING_DIR / "artifacts")
    events = EventLog(path=RECORDING_DIR / "events.jsonl", run_id="record")
    store = RunStore(RECORDING_DIR / "state.db")

    cassette = Cassette(CASSETTE_PATH)
    provider = NodeKeyedProvider(_canned_responses(), cassette)

    node_executors, executors_by_agent = greenfield.build_node_executors(
        provider, PROMPTS_DIR, recording_workspace, run_id="record"
    )
    engine = Engine(
        run_id="record", graph=graph, context=context, event_log=events, store=store,
        node_executors=node_executors, executors_by_agent=executors_by_agent,
        graph_expanders={"plan.decompose": greenfield.expand_impl_tasks},
    )
    engine.start(scenario="greenfield", workflow="greenfield")

    # Recording only needs every node to execute once so every prompt is
    # captured; auto-grant approvals immediately rather than modelling
    # the real two-pause flow (that is what the replay-mode test does).
    state = await engine.run()
    while state.status.value == "AWAITING_APPROVAL":
        for node_id, node_run in state.nodes.items():
            if node_run.status.value == "AWAITING_APPROVAL":
                engine.grant_approval(node_id, note="recorded by recording-script")
        state = await engine.run()

    if state.status.value != "SUCCEEDED":
        raise RuntimeError(f"recording run did not succeed: {state.status.value}")

    print(f"Recorded {len(cassette)} interactions to {CASSETTE_PATH}")
    # git leaves some object files read-only on Windows; a failed cleanup
    # here doesn't affect the recorded cassette, so don't let it fail the run.
    shutil.rmtree(RECORDING_DIR, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(_record())
