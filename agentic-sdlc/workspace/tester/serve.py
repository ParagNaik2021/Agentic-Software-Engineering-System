"""Runs the generated app with the manual tester UI mounted at /tester.

This lives in workspace/tester/, a sibling of workspace/urlshortener/ —
deliberately NOT inside urlshortener/ itself, for two independent reasons:

1. app/main.py is regenerated from scratch by ImplementerAgent on every
   `agentic run greenfield` (or brownfield), replaying a fixed cassette,
   so anything hand-added inside app/main.py itself would be silently
   overwritten by the next run. This launcher instead imports the
   already-built `app` object and augments it in place, which survives
   regeneration because nothing in the pipeline writes or deletes this
   file.
2. tools/ast_index.py's AstIndex does `root.rglob("*.py")` over
   workspace/urlshortener/ to build the codebase-analyst agent's
   workspace_modules list for the brownfield workflow's impact analysis
   — that list feeds directly into the LLM prompt's canonical hash. A
   stray .py file inside urlshortener/ (this one included, the first
   time it lived there) changes that list, changes the hash, and breaks
   replay against the committed cassette. Keeping this directory
   entirely outside urlshortener/ keeps that tree exactly what the
   agents themselves produced.

Serving tester.html from the same app/same origin means its fetch()
calls can use plain relative paths (/api/v1/links, /{code}, ...) with no
CORS configuration needed.

Usage: python serve.py [--port 8000]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "urlshortener"
sys.path.insert(0, str(APP_DIR))

from fastapi.responses import FileResponse  # noqa: E402

from app.main import app  # noqa: E402

TESTER_HTML = Path(__file__).parent / "tester.html"


@app.get("/tester", include_in_schema=False)
def _tester_page() -> FileResponse:
    return FileResponse(TESTER_HTML)


# app/main.py's own GET /{code} redirect route is a single-segment
# catch-all, registered before this module ever runs. Starlette matches
# routes in registration order, so appending here (the decorator's
# default) would let /{code} shadow /tester. Move it to the front instead.
app.router.routes.insert(0, app.router.routes.pop())


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    print(f"tester UI: http://{args.host}:{args.port}/tester")
    uvicorn.run(app, host=args.host, port=args.port)
