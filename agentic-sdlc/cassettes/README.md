# Cassettes

`<workflow>.json` maps `request_hash -> recorded interaction`, so
`--mode replay` needs no API key, no network and no cost (Section 9.1).
A prompt edit changes the hash and misses cleanly rather than silently
returning the wrong answer.

## Provenance of `ambiguous.json`

Not every entry in this file came from a live model call, and anyone
reading run output from this scenario should know which is which.

**Recorded live (6 entries)** — the requirements and ambiguity responses,
plus `plan.decompose` and `design.arch` for *both* passes: the
pre-clarification pass on the ambiguity agent's proposed defaults, and the
post-clarification pass against *"p95 redirect latency under 50ms; add
geographic and device breakdown"*. These are what make the scenario's
re-plan meaningful — the task graph and architecture genuinely change
between passes.

**Seeded from `greenfield.json` (8 entries)** — `design.data`,
`design.api`, `impl.T1`, `impl.T2`, `verify.security`, `verify.unit`,
`docs.generate`, `release.readiness`. When `workflows/ambiguous.py` was
moved onto `common.build_canonical_graph()` it gained the full
implementation and verification fan-out, and this cassette had no
recordings for those nodes. Rather than leave the scenario unrunnable,
each was filled with the response `greenfield.json` recorded for the
*same node*, re-keyed to this workflow's own `request_hash`.

Consequences worth knowing:

* the data model and API contract this scenario produces are greenfield's,
  so the geographic/device clarification is reflected in the **task graph
  and architecture artifacts only**, not in the data/API design or in the
  generated code;
* the implementer entries are greenfield's `impl.T2` response filtered to
  each task's declared `file_scope` (`app/service.py`), because
  ambiguous's task graph scopes its tasks more narrowly than greenfield's
  and an unfiltered replay trips the implementer's file-scope check;
* the seeded hashes assume the demo's **recorded approval order**:
  `req.clarify` answered *before* `design.review` is granted. Granting
  `design.review` first changes the downstream prompts, and those hashes
  are not recorded — expect a replay miss.

To replace any of this with genuine recordings, set `ANTHROPIC_API_KEY`
and run `agentic run ambiguous --mode live`, which records as it goes.
