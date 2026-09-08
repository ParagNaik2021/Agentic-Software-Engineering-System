# Scenario walkthrough: brownfield

Source run: `runs/brownfield-demo/` (recorded via
`scripts/run_brownfield_demo.py`, replaying `cassettes/brownfield.json`; full
report at `runs/brownfield-demo/report.md` / `report.html`). Reproduce with:

```
agentic run brownfield --mode replay
agentic approve <run_id> design.review
agentic resume <run_id>
agentic approve <run_id> release.readiness
agentic resume <run_id>
```

## The setup

`workflows/brownfield.py`'s `RAW_REQUIREMENT` asks for a change against the
**existing** URL shortener codebase left behind by the greenfield run (a
bulk-create endpoint: `POST /api/v1/links/bulk`). Unlike greenfield, this
workflow's graph has no `design.arch` / `design.data` nodes — there is no
architecture to redesign, only an existing one to extend safely.

## What happens, node by node

1. **`intake` → `req.analyze` → `req.ambiguity` → `plan.decompose`** — same
   shape as greenfield, producing a `task_graph` scoped to the one new
   capability.
2. **`analysis.impact`** is brownfield-specific: it reads the existing
   workspace (via the AST index / `tools/git.py`) and produces two
   artifacts — `impact_report` and `module_map` — identifying which files a
   change will touch *before* any code is written. In this run: `['app/service.py', 'app/main.py']`
   (visible in the demo script's own printed summary and in the artifact
   list). This is the mechanism that lets a re-plan later be scoped to only
   the modules actually affected, rather than invalidating the whole graph.
3. **`design.api`** updates the OpenAPI schema and examples for the new
   endpoint — no `design.review` content changes needed for architecture,
   but the same **`design.review`** human gate still applies, because any
   API surface change is reviewable regardless of workflow.
4. Once approved and resumed, the task graph expands to a single
   **`impl.T1`** node (one task this time, since the change is scoped to
   one endpoint) followed by **`impl.join`**.
5. **`verify.static` / `verify.security` / `verify.unit`** run concurrently
   against the *whole* workspace (not just the changed files) — the same
   quality bar applies to a one-file change as to a full build. In this
   run they took 7.1s / 6.2s / 5.8s of the 11.0s total.
6. **`verify.integration` → `verify.gate`** — exit conditions all `PASS`
   (96% coverage, no HIGH/CRITICAL findings), identical structure to the
   greenfield gate but evaluated against the extended codebase.
7. **`docs.generate` → `release.readiness`** — second approval gate. The
   `engineering_summary` artifact for this run reads: *"Bulk endpoint added;
   all exit gates passed at >=80% coverage, no HIGH/CRITICAL findings."*
8. **`summary`** closes the run `SUCCEEDED`.

## Decisions recorded

Only one decision this time, matching the scoped nature of the change:

- `impl.T1` (implementer): *"Added LinkService.create_links_bulk and the
  POST /api/v1/links/bulk endpoint."*

Compare this to greenfield's four decisions (one per design node plus one
per implementation task) — brownfield's smaller decision set is a direct
consequence of `analysis.impact` having already scoped the blast radius
down to two files.

## What this scenario is meant to demonstrate

- **Impact analysis before implementation**: the workflow structurally
  forces a "what will this touch" step ahead of any code change, which
  greenfield's graph doesn't need (there's nothing to impact yet).
- **The same verification and approval bar** applies regardless of change
  size — a one-endpoint addition still runs the full static/security/unit/
  integration/gate pipeline and still requires both human approvals.
- **A smaller, more scoped decision and artifact surface** than greenfield,
  produced by the same agents and the same context-store mechanics — the
  workflow graph shape changes, not the underlying engine.
