# Engineering summary

`agentic-sdlc` is a stateful, governed, non-linear orchestration engine
that drives a requirement through requirements analysis, design,
implementation, verification and release readiness using specialised
agents, using a URL-shortener service as its concrete target application.
This document is the top-level "what was built and why" — for how the
engine actually schedules and recovers work see
[docs/orchestration-model.md](orchestration-model.md); for real end-to-end
transcripts see [docs/scenarios/](scenarios/).

## Plan

The build proceeded in ten phases (P0–P9), each gated on its own
acceptance criteria (lint clean, mypy clean, its slice of the test suite
passing) before the next began:

- **P0** — project scaffolding, config, CLI stub.
- **P1** — core event sourcing: `EventLog` (hash-chained JSONL),
  `RunStore` (SQLite projection), `RunState`.
- **P2** — `WorkflowGraph`, the scheduler tick, `ContextStore`
  (namespaced, versioned blackboard), re-plan invalidation.
- **P3** — the LLM provider stack: `LLMProvider` protocol, canonical
  request hashing, `ReplayProvider`/cassettes, `ValidatingProvider`,
  `FaultInjectingProvider`.
- **P4** — the agent layer: `Agent` base class, autonomy levels, the
  requirements/ambiguity/planner/architect/implementer agent family.
- **P5** — recovery: `RecoveryManager`, the retry/fallback/rollback/
  safe-stop decision table, `GitCheckpointer` for workspace rollback.
- **P6** — governance: `ApprovalManager`, gates, policy rules,
  `ReleaseManagerAgent`.
- **P7** — the greenfield workflow end to end, generating the real
  `workspace/urlshortener/` baseline.
- **P8** — the brownfield and ambiguous workflows, dynamic graph
  expansion (`GraphExpander`), process-boundary resume.
- **P9** — wiring the CLI from stubs to a real implementation
  (`src/agentic/runtime.py` + `cli.py`), and this documentation set.

## Rationale for the core design choices

- **Event sourcing over a mutable state object** so that audit
  (`agentic verify-audit`) and process-boundary resume
  (`agentic resume` in a fresh process) both fall out of the same
  mechanism instead of needing separate bespoke support.
- **Structural approval gates** (`requires_approval=True` on a graph node)
  over a runtime condition an executor checks, so a safety checkpoint is
  visible in the graph shape and can't be quietly bypassed by agent logic.
- **A namespaced, versioned `ContextStore`** instead of agents reading each
  other's output directly, so `input_hash` (the signal that drives
  re-plan invalidation) is computed over exactly what a node is allowed to
  see, and provenance (`produced_by_node`, `produced_by_agent`) is
  structural rather than something an agent has to remember to attach.
- **Cassette replay as the default LLM mode** so the full demo suite is
  deterministic, fast, and runnable with no API key — at the deliberate
  cost that a genuinely new prompt cannot be replayed (see
  [docs/scenarios/ambiguous.md](scenarios/ambiguous.md)'s Act 2).

Full discussion of these and other trade-offs, including the ones that
turned out to have real costs during this build, is in
[docs/risks-and-tradeoffs.md](risks-and-tradeoffs.md).

## What exists

- A working CLI (`agentic run|resume|approve|reject|report|lineage|replan|
  halt|verify-audit|approvals list|approvals show`) backed by a real
  engine — not a stub. `make demo` (`agentic run greenfield --mode
  replay`, then approve+resume twice) runs to `SUCCEEDED` end to end.
- Three workflows (greenfield, brownfield, ambiguous), each with a
  recorded cassette. `scripts/run_greenfield_demo.py` and
  `scripts/run_brownfield_demo.py` regenerate `runs/greenfield-demo/` and
  `runs/brownfield-demo/` (gitignored, not shipped in the repo) with
  `report.md`/`report.html` — the reports the scenario docs quote from.
- 477 passing tests across unit, integration and contract suites; ruff and
  mypy clean on `src/`.
- A generated target application (`workspace/urlshortener/`) that the
  greenfield run produces and the brownfield run extends — itself
  passing its own generated test suite at 96% coverage.

## Artifacts this project produces, per run

Every run leaves behind `runs/<run_id>/{events.jsonl, state.db,
artifacts/, artifacts/decisions/, report.md, report.html}` — a complete,
independently auditable record of what happened, in what order, on whose
approval, and why (via recorded `Decision`s), without needing the
orchestrator process itself to still be alive to answer questions about
it. `agentic lineage --artifact <name>` walks that record backward from
any artifact to the decisions, nodes and agents that produced it and
everything upstream of them.

## Risks, trade-offs, assumptions and limitations

See [docs/risks-and-tradeoffs.md](risks-and-tradeoffs.md) for the full,
non-duplicated discussion — deliberately kept as one document rather than
repeated in summary form here.

## What P9's own work surfaced

Wiring the CLI from stubs to a real, two-process-capable tool (rather than
only the single-process fixtures the test suite used until this phase)
surfaced three real defects — lost approvals across a resume, prompt-order
nondeterminism across a resume, and decisions never being persisted at
all — none of which any prior automated test caught, because none of them
had actually exercised a second, independent `agentic` process against a
run the first process started. All three are fixed and are now covered by
`tests/integration/test_engine_resume.py`'s explicit resumed-vs-continuous
comparison. The full account, including exactly what was wrong and how
each was found, is in [docs/testing.md](testing.md).
