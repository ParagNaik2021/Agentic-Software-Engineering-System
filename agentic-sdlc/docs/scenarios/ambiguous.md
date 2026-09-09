# Scenario walkthrough: ambiguous

`workflows/ambiguous.py` is the scenario built specifically to prove the
engine's execution is non-linear and stateful — not a DAG that always runs
top to bottom in the same order — and that a human's clarification
retroactively invalidates work that ran without it.

Reproduce with either path:

```
# CLI path (scripted / reproducible)
agentic run ambiguous --mode replay
agentic approve <run_id> req.clarify
agentic resume <run_id>

# GUI path (interactive)
agentic run ambiguous --mode replay
agentic watch <run_id>       # answer the questions in the window, click Submit
```

## The requirement

`RAW_REQUIREMENT` is deliberately underspecified: *"Make it faster and give
us better analytics."* Faster than what? Which analytics? `req.ambiguity`
flags this and raises clarifying questions, but the graph does not block on
an answer.

## The graph shape (why this scenario is different)

```
intake -> req.analyze -> req.ambiguity -> req.clarify (clarification checkpoint)
                              |                |
                              v                v
                        plan.decompose (join_policy="any")
                              |
                              v
                         design.arch -> summary
```

`plan.decompose` depends on `[req.ambiguity, req.clarify]` with
`join_policy="any"` — it fires as soon as **either** predecessor succeeds,
not both. Since `req.ambiguity` always finishes first (it's the one that
*produces* the clarifying questions; `req.clarify` is the human's answer to
them), `plan.decompose` — and everything downstream of it, through
`design.arch` and `summary` — runs immediately on the ambiguity agent's own
proposed defaults, without waiting for a human at all.

Note the graph has no `design.data` or `design.api`: this scenario ends at
`summary` on purpose, because what it exists to demonstrate is
clarification -> invalidation -> re-execution, not code generation.
`workflows/greenfield.py` and `workflows/brownfield.py` already cover the
full pipeline.

## Act 1: the run completes before the human answers

`agentic run ambiguous --mode replay` produces this on the first call:

| Node | Status |
|---|---|
| intake | SUCCEEDED |
| req.analyze | SUCCEEDED |
| req.ambiguity | SUCCEEDED |
| **req.clarify** | **AWAITING_APPROVAL** |
| plan.decompose | SUCCEEDED |
| design.arch | SUCCEEDED |
| summary | SUCCEEDED |

Note the ordering: everything *except* `req.clarify` is already done,
timestamped *after* `design.arch` and `summary` both completed, because it
was still sitting at the human checkpoint while the rest of the graph raced
ahead on defaults. The decision `design.arch` records makes this explicit:

> *"Add an in-process LRU redirect cache and a daily click-count aggregate,
> sized against the proposed 200ms default."* — rationale: *"No
> clarification received yet; proceeding on the ambiguity agent's proposed
> defaults so work is not blocked."*

`req.clarify` pauses at `AWAITING_APPROVAL` because it reuses the approval
mechanism, but it is a `SDLCStage.CLARIFICATION` node, not an approval
gate. That distinction is what `WatchController.is_clarification` keys on,
and it is why `agentic watch` shows this node an answer box and a **Submit**
button instead of Approve/Reject.

## Act 2: answering triggers the re-plan automatically

Answering the clarification — `agentic approve` + `agentic resume`, or
Submit in the watch window — produces the `clarification_answer` artifact.
That artifact is an input to `plan.decompose`, so every node downstream of
`req.clarify` now has a stored `input_hash` that no longer matches its
current one, and `Engine._request_replan_if_staled` queues a re-plan on the
spot. `run()`'s replan step then invalidates and re-queues the stale set:

```
REPLAN_TRIGGERED  changed_node=req.clarify  trigger=input_hash_mismatch
                  invalidated=[design.arch, plan.decompose, summary]
NODE_INVALIDATED  summary / plan.decompose / design.arch
```

Each of the three goes `SUCCEEDED -> INVALIDATED -> PENDING -> READY ->
RUNNING -> SUCCEEDED`, reaching `attempt=2` with a second real
`AGENT_INVOKED` per agent node, and the run ends `SUCCEEDED` with
`replans=1` and the hash chain intact. The design genuinely changes rather
than re-running identically:

| Artifact | Pass 1 (defaults) | Pass 2 (clarified) |
|---|---|---|
| `task_graph` T1 | "Add basic in-process redirect caching (default 200ms target)" | "Add redirect-path caching and index tuning to meet p95 < 50ms" |
| `task_graph` T2 | "Add daily click-count aggregation" | "Add geographic and device-type breakdown to click analytics" |
| `architecture_design` | `cross_cutting_concerns: []` | `cross_cutting_concerns: ["geo/device lookup on click events"]` |
| `adr_records` | "sized against the proposed 200ms default" | "Size the redirect cache and index tuning for p95 < 50ms; extend click analytics with geographic and device dimensions" |

This automatic path was wired late: until
`Engine._request_replan_if_staled` existed, `ReplanController.request()`
had exactly one caller in the codebase — `Engine.replan_now()`, reached only
from `agentic replan` — so `run()`'s `if self.replan.pending()` step never
fired on its own and an answered clarification silently never reached the
design. `detect_stale`/`compute_invalidation_set` were correct the whole
time; nothing asked them.

## Act 3: the operator-driven re-plan, for revised guidance

`agentic replan` still exists, for the different case where guidance
changes *after* a node already consumed an answer:

```
agentic replan <run_id> --node req.clarify \
  --input "p95 redirect latency under 20ms; add hourly click trend and referrer breakdown"
agentic resume <run_id>
```

In **replay** mode this halts: `plan.decompose`'s re-executed prompt now
contains an answer `cassettes/ambiguous.json` was never recorded against,
so `ReplayProvider` raises a replay-miss, the fault classifier treats it as
systemic rather than transient, and `SAFE_STOP_ENGAGED` fires instead of a
fabricated response. That is the correct behavior of a replay-backed demo —
a genuinely new answer needs `--mode live` or a freshly recorded cassette.
The committed cassette holds recordings for both passes of the **default**
answer (`DEFAULT_CLARIFICATION_ANSWER`), which is why Act 2 replays
cleanly. `agentic verify-audit` reports the chain intact through the halt.

## What this scenario demonstrates

- **Non-linear execution**: an `any`-join node can run — and everything
  downstream of it — before a human answers the very question that node
  was waiting on, because something else on the graph didn't need that
  answer to proceed.
- **Clarification-driven invalidation**: answering the question
  invalidates exactly the changed node's downstream set
  (`core/replan.py`), not the whole graph, and the re-executed nodes
  produce materially different designs.
- **Two checkpoint kinds, one mechanism**: CLARIFICATION nodes and
  approval gates share `AWAITING_APPROVAL` but are distinguished by
  stage, and `agentic watch` gives each its own controls.
- **Recovery classification under a demo's real constraints**: a replay
  cassette is a frozen transcript, and the engine's fault classifier
  correctly refuses to fabricate a response for a prompt it has never
  seen — it safe-stops instead, which is what Section 9's recovery model
  calls for on a systemic condition.
