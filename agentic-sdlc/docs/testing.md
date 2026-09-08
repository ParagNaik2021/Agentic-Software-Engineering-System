# Testing strategy

477 tests, `pytest -q` from the repo root, ~4 minutes. `make lint` (ruff +
mypy strict) and `make test` both pass clean on `src/` as of this writing.

## Layout

```
tests/unit/          — one module's behavior in isolation
tests/integration/   — multiple modules wired together, exercising a real
                        Engine + ContextStore + EventLog + RunStore
tests/contract/       — agent input/output contracts against their
                        pydantic schemas
```

### Unit (`tests/unit/`)

Each core module has a direct test: `test_graph.py` (topological/cycle
validation), `test_context.py` (versioning, `view_for` scoping,
`input_hash`), `test_events.py` / `test_audit.py` (hash-chain integrity and
tamper detection), `test_states.py` (`RunState` projection), `test_store.py`
(SQLite persistence round-trip), `test_replan.py` (invalidation scoping),
`test_recovery.py` (retry/fallback/rollback/safe-stop decision table),
`test_gates.py` / `test_policy_engine.py` / `test_policy_rules.py` (gate and
policy evaluation), `test_autonomy.py` /
`test_agent_autonomy_enforcement.py` (an agent cannot act above its
declared `max_autonomy`), `test_approvals.py` (decision-package rendering),
`test_metrics.py` / `test_reporting.py` (report generation), and the
`test_llm_*.py` family covering the provider stack in isolation:
`test_llm_provider.py` (the `LLMProvider` protocol and `build_request`'s
canonical hashing), `test_llm_cassette.py` / `test_llm_replay.py` (cassette
lookup and `ReplayProvider`'s miss/live-fallback behavior),
`test_llm_validating_provider.py` (schema-validated retries),
`test_llm_fault_injection.py` (`FaultInjectingProvider` /
`FaultProfile` — see **Known gap** below), and `test_llm_live.py` (the real
provider, skipped unless an API key is configured). `test_tools_fs.py`,
`test_tools_git.py`, `test_tools_shell.py` and `test_ast_index.py` cover the
sandboxed workspace primitives agents call into; `test_tracing.py` and
`test_scaffolding.py` round out observability and the P0 project layout.

### Integration (`tests/integration/`)

`test_greenfield_scenario.py`, `test_brownfield_scenario.py` and
`test_ambiguous_scenario.py` each drive a full workflow through a real
`Engine` against a `tmp_path` workspace and a recorded cassette — the same
code path `scripts/run_*_demo.py` uses against the permanent
`workspace/urlshortener/`, just disposable. `test_engine_parallel_join.py`
asserts genuine wall-clock overlap for independent nodes (not just
correct ordering) with a tolerance loosened to `< 0.5s` against a `0.6s`
serial floor — timing assertions are inherently a little noisy under load,
so the margin is deliberately generous rather than flaky.
`test_engine_dynamic_expansion.py` covers `GraphExpander`'s `impl.<task_id>`
node injection. `test_engine_recovery.py` exercises retry/fallback/rollback
end to end through a live `Engine`, not just the recovery decision table in
isolation. `test_engine_resume.py` is the process-boundary test class: it
tears down and reconstructs the `Engine` from `RunStore` + `EventLog` +
`ContextStore.load_from_disk()` mid-run and asserts the resumed run
produces identical behavior to an uninterrupted one — this is the suite
that caught BUG 1 and BUG 2 (see below) once it was extended to actually
compare a resumed run's decisions against a continuous one's.

### Contract (`tests/contract/`)

`test_agents_contract.py` asserts every agent's declared `output_contract`
matches what its `output_schema` can actually produce, and that
`_to_artifacts_and_decisions` only ever emits artifacts named in that
contract — this is what stops an agent from silently producing an artifact
name nothing downstream expects, or contract-decorating this as covered by a
unit test with a real assertion.

## What manual, non-automated testing caught that pytest didn't

The automated suite's process-boundary tests use in-process fixtures that
construct a fresh `Engine` object directly. They never actually launched
`agentic` as a subprocess twice in a row the way a real operator would:

- **`agentic approve` (process 1) → `agentic resume` (process 2)** silently
  lost the approval — `Engine.grant_approval()`'s dict was in-memory only
  and `Engine.resume()` never reconstructed it from the
  `APPROVAL_GRANTED`/`APPROVAL_REJECTED` event history.
- **The same two-process sequence** made `docs.generate` (and any other
  agent that serializes `ctx.artifacts.keys()`/`.items()` into a prompt) go
  `FAILED` with a replay-miss, because `ContextStore.view_for()` iterated
  artifacts in `dict` insertion order — chronological in a live process,
  effectively the sort order of on-disk UUID filenames after
  `load_from_disk()` rehydrates a resumed one. Same artifacts, different
  key order, different prompt, different `request_hash`.
- **Decisions never survived a resume at all**: `record_decision()` only
  ever wrote to the in-memory `_decisions` dict, never to `persist_dir`, so
  `agentic report`/`agentic lineage` run in a fresh process after a resume
  showed an empty Decisions section even when the original run genuinely
  recorded several.

All three were found by literally running the CLI as two separate `python
-m agentic.cli ...` invocations against a real run directory and comparing
output to a continuous, single-process run of the same workflow — not by
any assertion pytest was making at the time. All three are now fixed (see
[docs/orchestration-model.md](orchestration-model.md) for the mechanism)
and are the reason `tests/integration/test_engine_resume.py` now explicitly
diffs a resumed run's context and decisions against a continuous one,
rather than only checking final `RunState.status`.

**Takeaway carried forward**: an in-process fixture that constructs the
object under test directly is not a substitute for actually crossing the
boundary a real user crosses (a new OS process, a fresh CLI invocation). For
this project that boundary is `agentic resume` in particular — anything
State-shaped that isn't event-sourced or explicitly persisted will silently
vanish there, and only a real second process notices.

## Known gap: fault injection is not wired to the CLI

`agentic/llm/provider.py` has a working `FaultInjectingProvider` /
`FaultProfile` pair, and `test_llm_fault_injection.py` exercises transient
and quality-fault injection against it directly. Nothing in `runtime.py` or
`cli.py` currently constructs one from a command-line flag — `agentic run`
has no `--inject-fault` option. The Makefile briefly advertised a
`demo-faults` target that referenced such a flag; it never existed in the
CLI and has been removed rather than left to fail. Recovery
(retry/fallback/rollback/safe-stop) is fully covered by
`test_recovery.py` and `test_engine_recovery.py` at the unit/integration
level — this gap is specifically about there being no interactive,
CLI-driven way to reproduce a fault-triggered recovery by hand. See
[docs/risks-and-tradeoffs.md](risks-and-tradeoffs.md).
