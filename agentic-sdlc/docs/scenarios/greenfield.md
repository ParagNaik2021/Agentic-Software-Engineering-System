# Scenario walkthrough: greenfield

Source run: `runs/greenfield-demo/` (recorded via `scripts/run_greenfield_demo.py`,
replaying `cassettes/greenfield.json`; the full report is at
`runs/greenfield-demo/report.md` / `report.html`). Reproduce it yourself with:

```
agentic run greenfield --mode replay
agentic approve <run_id> design.review
agentic resume <run_id>
agentic approve <run_id> release.readiness
agentic resume <run_id>
```

## The requirement

`workflows/greenfield.py`'s fixed `RAW_REQUIREMENT` describes a URL shortener:
create short links from long URLs, redirect on lookup, and record click
analytics. There is no existing codebase — `intake` hands this raw text
straight to `req.analyze`.

## What happens, node by node

1. **`intake` → `req.analyze` → `req.ambiguity`** — the requirement is
   normalized into a spec with acceptance criteria, and scanned for
   ambiguity. Nothing here rises to a blocking clarification, so the run
   does not route through `req.clarify` at all (that node only appears in
   the ambiguous workflow's graph).
2. **`plan.decompose`** — produces the `task_graph` artifact that later
   drives `GraphExpander`'s dynamic `impl.<task_id>` expansion.
3. **`design.arch` / `design.data` / `design.api`** run concurrently — the
   timeline in `report.md` shows all three starting within 71ms of each
   other and finishing with overlapping windows, which is the scheduler's
   fan-out for independent graph nodes, not simulated parallelism.
4. **`design.review`** is a human approval gate. The run stops here with
   status `AWAITING_APPROVAL`; `agentic run` prints the exact follow-up
   commands. This is the process-boundary path exercised by the engine's
   `resume()` reconstruction (approvals and context are rebuilt from the
   event log in the new process, not carried over in memory).
5. Once approved and resumed, `plan.decompose`'s task graph expands into
   `impl.T1` and `impl.T2` (the dynamic nodes named in the Nodes table),
   which also run in parallel, followed by `impl.join`.
6. **`verify.static` / `verify.security` / `verify.unit`** run concurrently
   — in this recorded run they dominate the wall-clock time (13.6s, 5.9s,
   5.5s respectively out of 17.8s total), because they're the executors
   that actually shell out to ruff/mypy/pytest/bandit against the
   generated workspace rather than replay a cassette.
7. **`verify.integration`** runs after those three join, then
   **`verify.gate`** evaluates the exit conditions from the report:

   | Condition | Verdict |
   |---|---|
   | tests_pass | PASS |
   | coverage_threshold | PASS (96% vs. 80% floor) |
   | no_high_findings | PASS |

8. **`docs.generate`** writes the workspace README from the design and API
   artifacts.
9. **`release.readiness`** is the second approval gate. `ReleaseManagerAgent`
   produces both `release_readiness_report` and `engineering_summary`
   artifacts; the latter is what `agentic report` surfaces under
   "Engineering Summary" — in this run: *"All exit gates passed: unit+integration
   tests green at >=80% coverage, no HIGH/CRITICAL security findings."*
10. **`summary`** closes the run with status `SUCCEEDED`.

## Decisions recorded

The report's Decisions section (reconstructed from `DECISION_RECORDED`
events plus the persisted `decisions/` directory in the run's context
store) shows the real design choices an agent made and why, not just what
it produced:

- `design.arch` (architect): *"Layered architecture with a repository-pattern
  persistence boundary."* — rationale: keeps persistence swappable without
  touching the service or API layers.
- `design.data` (data_model): *"Use an in-memory repository for this demo
  pass rather than SQLAlchemy/SQLite immediately."* — rationale: avoids
  async database setup complexity while keeping the repository interface
  swappable for a real backend later.
- `impl.T1` / `impl.T2` (implementer): one decision per generated module,
  tying the `Link` model / in-memory repository and the `LinkService` +
  FastAPI router back to the task graph.

These are exactly the kind of statements `agentic lineage --artifact
<name>` walks backward through when asked "why does this artifact look the
way it does."

## What this scenario is meant to demonstrate

- A full run with **zero re-plans** — the spec was unambiguous enough that
  no downstream node ever invalidated an upstream one.
- Two human approval gates, each crossing a real process boundary (the
  demo script drives `agentic approve` then a fresh `Engine.resume()` for
  `agentic resume`, not an in-memory continuation).
- Genuine concurrent execution of independent graph nodes, both in the
  static design fan-out and in the dynamically expanded implementation
  tasks.
- An audit chain that verifies clean (`agentic verify-audit <run_id>`)
  end to end.
