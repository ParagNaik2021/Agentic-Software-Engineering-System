# Scenario walkthrough: ambiguous

`workflows/ambiguous.py` is the scenario built specifically to prove the
engine's execution is non-linear and stateful — not a DAG that always runs
top to bottom in the same order. It has no permanent `runs/ambiguous-demo/`
checked in (unlike greenfield/brownfield) because its interesting behavior
only shows up across two acts driven by real operator decisions; the
transcript below was captured by actually running both acts against
`cassettes/ambiguous.json` in replay mode.

Reproduce Act 1 with:

```
agentic run ambiguous --mode replay
agentic approve <run_id> req.clarify
agentic resume <run_id>
```

## The requirement

`RAW_REQUIREMENT` is deliberately underspecified: *"Make it faster and give
us better analytics."* Faster than what? Which analytics? `req.ambiguity`
flags this and raises clarifying questions, but the graph does not block on
an answer.

## The graph shape (why this scenario is different)

```
intake -> req.analyze -> req.ambiguity -> req.clarify (approval gate)
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

## Act 1: the run completes before the human answers

Running `agentic run ambiguous --mode replay` produces this on the first
call:

| Node | Status |
|---|---|
| intake | SUCCEEDED |
| req.analyze | SUCCEEDED |
| req.ambiguity | SUCCEEDED |
| **req.clarify** | **AWAITING_APPROVAL** |
| plan.decompose | SUCCEEDED |
| design.arch | SUCCEEDED |
| summary | SUCCEEDED |

Note the ordering: everything *except* `req.clarify` is already done. The
run's own execution timeline confirms it — `req.clarify` is the last entry
chronologically, timestamped *after* `design.arch` and `summary` both
completed, because it was still sitting at the human checkpoint while the
rest of the graph raced ahead on defaults. The decision `design.arch`
records makes this explicit:

> *"Add an in-process LRU redirect cache and a daily click-count aggregate,
> sized against the proposed 200ms default."* — rationale: *"No
> clarification received yet; proceeding on the ambiguity agent's proposed
> defaults so work is not blocked."*

Approving `req.clarify` and resuming produces the `clarification_answer`
artifact and the run reaches `SUCCEEDED` with **0 re-plans** — approving the
gate alone does not retroactively invalidate the nodes that already used
defaults. That's a deliberate property, not an oversight: the system never
auto-replans on your behalf just because new information arrived; a
human or operator has to decide the new information actually changes
something and say so.

## Act 2: forcing the re-plan

To see the re-plan machinery fire, the operator has to actually invoke it —
`agentic replan` — pointing at the node whose new answer should invalidate
downstream work:

```
agentic replan <run_id> --node req.clarify \
  --input "p95 redirect latency under 20ms; add hourly click trend and referrer breakdown"
agentic resume <run_id>
```

This immediately marks `plan.decompose`, `design.arch` and `summary` —
everything structurally downstream of `req.clarify` — invalidated
(`NODE_INVALIDATED` events) and re-queues them. In this session's own
replay-mode reproduction, `plan.decompose` then failed and the run reached
`HALTED`, with a `SAFE_STOP_ENGAGED` event citing *"systemic failure
(budget exhausted, audit chain broken, deadlock, or replan budget
exceeded)."* The reason is specific to replay mode: `plan.decompose`'s
re-executed prompt now includes a clarification answer the cassette was
never recorded against, so `ReplayProvider` raises a replay-miss, which the
fault classifier treats as systemic (not a transient, retryable condition)
and safe-stops the run rather than guessing. **This is the correct, honest
behavior of a replay-backed demo** — a real re-plan with genuinely new
guidance needs either `--mode live` (a real model call) or a freshly
recorded cassette; it is not something a fixed transcript can paper over.
`agentic verify-audit <run_id>` still reports the hash chain intact through
the halt, and `agentic halt <run_id>` reaches the same state from an
operator command instead of the automatic classifier.

## What this scenario is meant to demonstrate

- **Non-linear execution**: an `any`-join node can run — and everything
  downstream of it — before a human answers the very question that node
  was waiting on, because something else on the graph didn't need that
  answer to proceed.
- **Explicit, operator-driven re-planning**: new information does not
  retroactively invalidate finished work by itself; `agentic replan` is
  the deliberate act that does, and it is scoped to the changed node's
  actual downstream set (`core/replan.py`), not the whole graph.
- **Recovery classification under a demo's real constraints**: a replay
  cassette is a frozen transcript, and the engine's fault classifier
  correctly refuses to fabricate a response for a prompt it has never
  seen — it safe-stops instead, which is exactly the behavior Section 9's
  recovery model calls for on a systemic condition.
