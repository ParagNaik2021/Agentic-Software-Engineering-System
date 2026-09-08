# Risks and trade-offs

## Deliberate trade-offs

**Event sourcing + a `RunState` projection, instead of just a mutable state
object.** Every state change is a fact appended to a hash-chained
`events.jsonl` first; `RunState` is rebuilt from it. This is more
machinery than a single mutable object would need, and it means every new
kind of state has to ask "does this need an event, or can it be derived."
The payoff is `agentic verify-audit` (tamper-evident history for free) and
genuine process-boundary resume — a fresh `agentic` invocation can
reconstruct exactly where a run stood by replaying facts, not by trusting
whatever a previous process happened to leave in memory. The cost showed
up directly in this project: `granted_approvals` and `Decision` objects
both started as plain in-memory dicts that *looked* like state but weren't
event-sourced or persisted, and both silently vanished across a resume
until real cross-process testing caught it (see
[docs/testing.md](testing.md)). **The trade-off held**, but only because
the failure mode was loud enough (a `FAILED` node, an empty report
section) to notice — a quieter piece of derived state could still be
sitting somewhere unrecovered.

**Structural gates over runtime conditions on `approval_granted`.**
`design.review` and `release.readiness` are graph nodes with
`requires_approval=True`, not a condition string an executor checks. This
was a deliberate choice (see
[docs/orchestration-model.md](orchestration-model.md)) so an approval
checkpoint is visible in the graph shape itself and can't be silently
bypassed by an agent's own logic. The cost: adding a new approval gate
means changing the workflow graph, not just tightening a condition — a
slightly heavier change for a safety property that is meant to be heavy to
change.

**Cassette replay as the default LLM mode.** `--mode replay` makes the
whole demo suite deterministic, fast, and free to run repeatedly (this is
what makes `agentic run <workflow> --mode replay` a reliable `make demo`).
The cost is real and shows up directly in
[docs/scenarios/ambiguous.md](scenarios/ambiguous.md)'s Act 2: a genuinely
new prompt (one the cassette was never recorded against) cannot be
answered by replay at all — the run safe-stops rather than fabricate a
response. This is the correct behavior, not a bug, but it means replay
mode can only demonstrate re-planning up to the edge of what was recorded;
demonstrating a *live* re-plan requires `--mode live` and a real API key.

**In-memory repository in the generated URL shortener, not
SQLAlchemy/SQLite.** `design.data`'s own recorded decision
(`docs/scenarios/greenfield.md`) is explicit about this: it avoids async
database setup complexity in the generated app while keeping the
repository interface swappable later. This is a trade-off made by the
*generated* application, driven by the orchestrator faithfully carrying an
agent's stated rationale into the report — not a limitation of the
orchestrator itself.

## Known gaps

- **Fault injection has no CLI surface.** `FaultInjectingProvider` /
  `FaultProfile` exist and are unit-tested
  (`tests/unit/test_llm_fault_injection.py`), but nothing in `cli.py`
  constructs one from a flag. A previous Makefile target
  (`demo-faults --inject-fault ...`) referenced this and has been removed
  rather than shipped broken. Recovery itself (retry/fallback/rollback/
  safe-stop) is fully covered by `test_recovery.py` and
  `test_engine_recovery.py`; what's missing is a way for an operator to
  trigger it interactively via the CLI rather than from a test fixture.
- **Tokens/cost are always reported as zero.** `MetricsCollector` has a
  `Tokens: 0 ($0.0000)` line in every report — replay mode never calls a
  real provider that would report usage, and no cassette interaction
  records token counts. A `--mode live` run would need
  `LiveProvider`/`ValidatingProvider` to actually surface usage from the
  underlying SDK response before this becomes a real number instead of a
  placeholder.
- **Policy Evaluations is always an empty table in the demo runs.** The
  gate/policy machinery (`test_policy_engine.py`, `test_policy_rules.py`)
  is real and tested, but none of the three demo workflows currently wire
  a `PolicyRule` that would actually fire during `verify.gate` — the
  section renders correctly, it's just never populated in these
  particular runs.
- **`runs/` is gitignored (`runs/*/`, keeping only `runs/.gitkeep`) — the
  two demo runs are regenerated, not committed.** `runs/greenfield-demo/`
  and `runs/brownfield-demo/` exist on disk after running
  `scripts/run_*_demo.py` (which `make demo`/`make demo-brownfield` do not
  call directly — see below), but a fresh clone starts without them. The
  scenario docs' quoted report excerpts are real output captured from
  running these scripts during this project's own development, not files
  a reviewer will find already sitting in the repo; regenerating them
  (`python scripts/run_greenfield_demo.py`, then
  `python scripts/run_brownfield_demo.py`) reproduces the same structure,
  though re-running after a cassette re-record or an engine change will
  overwrite the local copies and can shift timing numbers.

## Assumptions

- A reviewer following [README.md](../README.md) has Python 3.11+, git, and
  is willing to run `pip install -e ".[dev,target]"` — there is no
  containerized or otherwise fully hermetic setup path.
- `--mode replay` is the assumed default for anyone without an LLM API key;
  `--mode live` is exercised by `test_llm_live.py` (skipped without a key)
  and by hand during development, not by the two scripted demo runs.
- The generated URL shortener is a demonstration target, not a product —
  its own trade-offs (in-memory repository, no auth) are appropriate for
  what it's for and are not meant to be read as this project's engineering
  bar for production code.

## Limitations

- Re-planning is scoped to a single node's declared downstream set
  (`core/replan.py`); it does not attempt to detect indirect semantic
  impact outside the graph's own edges (e.g., a documentation artifact
  that references a design decision by name but has no structural
  `depends_on` edge to it).
- The audit chain (`verify-audit`) detects tampering with recorded events;
  it does not — and is not meant to — detect an agent producing a
  well-formed but substantively wrong artifact. Gates and human approval
  are the controls for that, not the hash chain.
- Everything here was verified with `--mode replay` against fixed,
  committed cassettes. `--mode live` behavior against a real model is
  exercised only where the test suite explicitly gates on an API key being
  present; it has not been run end-to-end against all three workflows as
  part of this project's own verification.
