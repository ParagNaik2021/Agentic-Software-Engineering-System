# Agentic SDLC Orchestration System

A stateful, governed, non-linear orchestration engine that drives
requirements through to release readiness using specialised agents,
generating a production-shaped URL Shortener service as its target
application.

## Setup

Requires Python 3.11+.

```
pip install -e ".[dev,target]"
```

That installs the orchestrator (`agentic` CLI), its dev tooling (pytest,
ruff, mypy), and the generated target application's own runtime
dependencies (FastAPI etc., needed because `agentic run` actually
`pip install`s and test-runs the code it generates into
`workspace/urlshortener/`).

## Quickstart: run the demo

No API key needed — this replays a recorded LLM cassette instead of
calling a real model:

```
make demo
```

This runs `agentic run greenfield --mode replay`, which executes until it
hits the first human approval checkpoint and stops:

```
Awaiting approval: design.review
  agentic approvals show <run_id> design.review
  agentic approve <run_id> design.review
  agentic resume <run_id>
```

Follow the three printed commands, then repeat once more for
`release.readiness`:

```
agentic approve <run_id> design.review
agentic resume <run_id>
agentic approve <run_id> release.readiness
agentic resume <run_id>
```

The final `agentic resume` prints every node as `SUCCEEDED`. Generate a
human-readable report of what just happened:

```
agentic report <run_id> --format md
```

`make demo-brownfield` and `make demo-ambiguous` run the other two
workflows the same way (brownfield needs `make demo-greenfield` — i.e.
`make demo` — to have run first, since it extends that run's generated
workspace). Walkthroughs of all three, including real report excerpts and
what to notice in each, are in [docs/scenarios/](docs/scenarios/).

## The CLI

```
agentic run <workflow> [--mode replay|live] [--clarification-answer TEXT]
agentic resume <run_id> [--mode replay|live]
agentic approve <run_id> <node_id> [--note TEXT]
agentic reject <run_id> <node_id> [--note TEXT]
agentic replan <run_id> --node <node_id> --input TEXT
agentic halt <run_id>
agentic report <run_id> [--format md|html|json]
agentic lineage <run_id> --artifact <name>
agentic verify-audit <run_id>
agentic approvals list <run_id>
agentic approvals show <run_id> <node_id>
```

`<workflow>` is one of `greenfield`, `brownfield`, `ambiguous`. `--mode
live` calls a real model and requires `AGENTIC_ANTHROPIC_API_KEY` to be
set; every command defaults to `--mode replay`, which needs no key and is
what both scripted demo runs use.

## Everyday development

```
make test    # pytest — 477 tests, ~4 min
make lint    # ruff check + mypy --strict
make format  # ruff format + ruff check --fix
```

## Documentation map

- **Architecture** — see the project's architecture document (maintained
  separately from this repository's `docs/`).
- [docs/orchestration-model.md](docs/orchestration-model.md) — how the
  scheduler, gates, recovery and re-planning actually work, mechanism by
  mechanism.
- [docs/scenarios/](docs/scenarios/) — real, reproduced walkthroughs of
  all three workflows (greenfield, brownfield, ambiguous), each quoting
  actual report/event data from a run.
- [docs/testing.md](docs/testing.md) — what's covered by which test layer,
  and what only manual two-process testing caught.
- [docs/risks-and-tradeoffs.md](docs/risks-and-tradeoffs.md) — the
  deliberate trade-offs, the known gaps, and the assumptions this project
  makes.
- [docs/engineering-summary.md](docs/engineering-summary.md) — the
  top-level plan, rationale, and what was built, phase by phase.

## Repository layout

```
src/agentic/          the orchestrator itself
  core/                graph, engine, context store, event log, replan
  agents/              agent base class + the requirements/design/impl/
                        verification/release-manager agent family
  llm/                 provider protocol, cassette replay, live provider,
                        fault injection
  governance/          approvals, gates, policy rules
  observability/       metrics, audit, reporting
  workflows/           the three workflow graphs (greenfield/brownfield/
                        ambiguous)
  tools/               sandboxed fs/git/shell primitives agents call into
  cli.py, runtime.py    the `agentic` command-line control plane
cassettes/             recorded LLM interactions for --mode replay
runs/                  per-run event log, state, artifacts and reports;
                        gitignored (runs/*/) — run scripts/run_*_demo.py
                        to (re)generate runs/greenfield-demo and
                        runs/brownfield-demo locally
workspace/urlshortener/ the generated target application
scripts/               cassette recording + demo-run generation scripts
tests/                 unit / integration / contract suites
docs/                  see the documentation map above
```
