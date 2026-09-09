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

No API key needed for any of the walkthroughs below — everything runs in
`--mode replay`, which replays a recorded LLM cassette instead of calling
a real model.

## Two ways to run a scenario

Every workflow can be driven either from the terminal (explicit commands,
scriptable, good for CI) or through a pop-up GUI (no manual
approve/resume typing, better for a live walkthrough). Both talk to the
same run — pick whichever fits the moment; you can even start a run with
one and finish it with the other.

- **Option A — GUI:** append `--watch` to `agentic run`. A window opens
  that polls the run, and whenever it pauses — at a human approval gate
  or a clarification question — shows the relevant content with
  Approve/Reject buttons or a text-answer box, instead of you typing
  `approvals show` / `approve` / `resume` by hand.
- **Option B — CLI:** run without `--watch` and follow the printed
  commands at each pause yourself.

---

## I. Greenfield Scenario (new system from a clear requirement)

Builds the URL Shortener service from scratch: link creation, redirect
with click tracking, soft-delete, health checks.

### Option A — GUI

```
python -m agentic.cli run greenfield --mode replay --watch
```

The window pauses twice — at `design.review` (review the architecture,
data model and API contract before any code is written) and at
`release.readiness` (review test/coverage/security results before the
run is allowed to finish). Approve each from the popup.

### Option B — CLI

From the repo root (`agentic-sdlc/`):

```
python -m agentic.cli run greenfield --mode replay
```

This runs until it hits the first approval gate and prints the run id
plus next steps:

```
Awaiting approval: design.review
  agentic approvals show <run_id> design.review
  agentic approve <run_id> design.review
  agentic resume <run_id>
```

Follow those, then it runs implementation, verification and docs, and
stops again at `release.readiness`:

```
agentic approve <run_id> release.readiness
agentic resume <run_id>
```

### After it finishes

Final status prints `SUCCEEDED` with every node green. The generated
service auto-starts as a detached background process:

```
Service started: http://localhost:8000
Tester UI:        http://localhost:8000/tester
  pid 12345 — to stop: kill 12345
```

If auto-start didn't fire (e.g. you passed `--no-serve`, or the process
that ran `agentic run` has since exited), start it manually from the
repo root:

```
python serve.py --port 8000
```

Then open:

```
http://127.0.0.1:8000/tester
```

Open the Tester UI to create a short link, test a redirect, and see the
short-link column render a clickable result — no manual `cd`/`uvicorn`
needed. The port defaults to **8000**; pass `--port` to use a different
one. If that port is already held by a previous agentic-managed
instance it's stopped and replaced automatically; if it's held by
something else, a free port is picked instead and the printed message
reflects whichever port actually got used. Pass `--no-serve` to skip
auto-start entirely (e.g. for CI).

Verify the tamper-evident audit chain and generate a human-readable
report:

```
python -m agentic.cli verify-audit <run_id>
python -m agentic.cli report <run_id> --format md
```

---

## II. Brownfield Scenario (enhancement to an existing codebase)

Adds custom-alias support (reserved-word + duplicate protection) and a
bulk-creation endpoint on top of the service greenfield generated.

**Precondition:** requires a completed greenfield run first —
`workspace/urlshortener/` must exist and be a clean git working tree
(`make demo`, i.e. Scenario I, produces this). Brownfield's
`analysis.impact` node reasons over that existing codebase; it isn't a
fresh build.

```
cd workspace\urlshortener
git status
```

Confirm the tree is clean, then from the repo root:

### Option A — GUI

```
python -m agentic.cli run brownfield --mode replay --watch
```

Same two pause points as greenfield (`design.review`,
`release.readiness`), plus you'll see `analysis.impact` run first and
produce a real impact report over the existing `app/` modules before any
design work starts.

### Option B — CLI

```
python -m agentic.cli run brownfield --mode replay
```

```
agentic approve <run_id> design.review
agentic resume <run_id>
agentic approve <run_id> release.readiness
agentic resume <run_id>
```

### After it finishes

Same auto-start behaviour as greenfield. If it didn't fire, start it
manually the same way:

```
python serve.py --port 8000
```

```
http://127.0.0.1:8000/tester
```

The Tester UI will now also show the custom-alias field and a Bulk
Create section — these are detected at page load from the running
service's own OpenAPI schema, so they only appear once
`/api/v1/links/bulk` actually exists (i.e. never on a plain greenfield
instance).

Manual verification if you'd rather use curl:

```
curl -X POST http://localhost:8000/api/v1/links -H "Content-Type: application/json" -d "{\"target_url\": \"https://example.com\", \"custom_alias\": \"mylink\"}"
curl -X POST http://localhost:8000/api/v1/links/bulk -H "Content-Type: application/json" -d "{\"items\": [{\"target_url\": \"https://example.com/a\"}, {\"target_url\": \"https://example.com/b\"}]}"
```

```
python -m agentic.cli verify-audit <run_id>
```

---

## III. Ambiguous Scenario (underspecified requirement + dynamic re-planning)

Requirement: *"Make it faster and give us better analytics."* — no
target metric, no named dimensions. This scenario exists to demonstrate
the orchestrator's critical differentiator: non-linear, stateful
execution that reacts to new information mid-run rather than following
a fixed pipeline.

**Precondition:** none — this scenario stands alone.

### Option A — GUI

```
python -m agentic.cli run ambiguous --mode replay --watch
```

The window pauses at `req.clarify` with a distinct panel (not
Approve/Reject) showing each raised ambiguity, its proposed default, and
a text box for your answer:

```
[AMB-1] What p95 redirect latency target should we hit?
        proposed default: 200ms.
[AMB-2] Which path should be optimized: redirect or creation?
        proposed default: Redirect.
[AMB-4] What analytics dimensions matter (geography, device, referrer)?
        proposed default: Daily click counts only.
```

Design work runs speculatively against these defaults while you decide,
so you'll see `design.review` become available even before you submit an
answer. Type your answer and click **Submit**:

- If your answer **matches** the defaults, nothing needs to re-run —
  proceed to approve `design.review` and `release.readiness` as usual.
- If your answer **contradicts** a default, the affected nodes
  (`plan.decompose` and the design fan-out) invalidate and re-run
  automatically — watch the node table for `SUCCEEDED → INVALIDATED →
  PENDING → RUNNING (pass 2)`, and note that any existing `design.review`
  approval is voided and must be re-granted against the corrected
  design.

### Option B — CLI

```
python -m agentic.cli run ambiguous --mode replay
```

Answer the clarification (use the defaults, or your own text):

```
agentic approve <run_id> req.clarify --note "p95 redirect latency under 50ms; add geographic and device breakdown"
agentic resume <run_id>
```

Check whether that answer triggered a re-plan:

```
Select-String -Path runs\<run_id>\events.jsonl -Pattern "REPLAN_TRIGGERED|NODE_INVALIDATED"
```

Continue through the remaining gates:

```
agentic approve <run_id> design.review
agentic resume <run_id>
agentic approve <run_id> release.readiness
agentic resume <run_id>
```

### After it finishes

Same auto-start and Tester UI as the other two scenarios. If it didn't
fire, start it manually:

```
python serve.py --port 8000
```

```
http://127.0.0.1:8000/tester
```

Verify the audit chain:

```
python -m agentic.cli verify-audit <run_id>
```

Full walkthroughs of all three scenarios, including real report excerpts
and what specifically to look for in each, are in
[docs/scenarios/](docs/scenarios/).

---

## Reviewing a design or release decision without reading raw JSON

`agentic approvals show <run_id> <node_id>` renders the decision package
as readable markdown sections (Normalized Requirement, Task
Decomposition, Architecture, Data Model, API Contract for `design.review`;
test/coverage/security/go-no-go summary for `release.readiness`) rather
than dumping artifact JSON. To review it outside the terminal — for
example to attach to an interview writeup — export it as a Word doc:

```
agentic approvals export <run_id> <node_id> --format docx
```

## If you reject a checkpoint

Rejecting `design.review` or `release.readiness` halts the run safely —
your rejection note is preserved in the audit log, and nothing downstream
runs against a rejected design. Halting is the correct default: a human
rejection is a judgment call, not something the system should silently
auto-retry.

To deliberately re-open the relevant design work with your feedback
incorporated:

```
agentic replan <run_id> --node <rejected_node> --from-rejection
```

This re-runs the upstream design nodes with your rejection note injected
into their context, and requires a fresh approval once they complete —
it will not auto-approve anything on your behalf.

## The CLI

```
agentic run <workflow> [--mode replay|live] [--watch] [--clarification-answer TEXT] [--port N] [--no-serve]
agentic resume <run_id> [--mode replay|live] [--port N] [--no-serve]
agentic watch <run_id>
agentic approve <run_id> <node_id> [--note TEXT]
agentic reject <run_id> <node_id> [--note TEXT]
agentic replan <run_id> --node <node_id> [--input TEXT] [--from-rejection]
agentic halt <run_id>
agentic report <run_id> [--format md|html|json]
agentic lineage <run_id> --artifact <name>
agentic verify-audit <run_id>
agentic approvals list <run_id>
agentic approvals show <run_id> <node_id>
agentic approvals export <run_id> <node_id> --format docx
```

`<workflow>` is one of `greenfield`, `brownfield`, `ambiguous`. `--mode
live` calls a real model and requires `AGENTIC_ANTHROPIC_API_KEY` to be
set; every command defaults to `--mode replay`, which needs no key and is
what every walkthrough above uses. `agentic watch <run_id>` can also be
run as a separate command at any time to attach the GUI to a run already
in progress, rather than only via `run --watch`.

## Everyday development

```
make test    # pytest — 477 tests, ~4 min
make lint    # ruff check + mypy --strict
make format  # ruff format + ruff check --fix
```

## Documentation map

- [docs/architecture.md](docs/architecture.md) — component overview and
  the runtime view (what process(es) exist while and after a run, the
  auto-started app, default port and `--port`/`--no-serve`).
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
  gui/                 the --watch approval/clarification popup window
  cli.py, runtime.py    the `agentic` command-line control plane
cassettes/             recorded LLM interactions for --mode replay
runs/                  per-run event log, state, artifacts and reports;
                        gitignored (runs/*/) — run scripts/run_*_demo.py
                        to (re)generate runs/greenfield-demo and
                        runs/brownfield-demo locally
workspace/urlshortener/ the generated target application
  tester/tester.html    browser UI for exercising the running service —
                        create/redirect/bulk-create, feature-detected
                        against the live OpenAPI schema
scripts/               cassette recording + demo-run generation scripts
tests/                 unit / integration / contract suites
docs/                  see the documentation map above
```
