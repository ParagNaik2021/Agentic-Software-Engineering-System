# Orchestration Model

This document explains *how* the orchestration core (`src/agentic/core/`)
actually executes a workflow: the graph, the gates, the scheduler loop,
the context store, and dynamic re-planning. It assumes you've read
`docs/architecture.md` for the system-level component map; this is the
next level down.

## 1. The graph is structural, not runtime

`core/graph.py`'s `WorkflowGraph` holds `NodeSpec` objects and
`depends_on` edges. It knows nothing about what has run or succeeded —
`validate()` checks for cycles (DFS), dangling dependencies, and nodes
unreachable from the declared root, once, at build time.
`topological_layers()` (Kahn's algorithm) exists mainly as a build-time
sanity check and for tests; the *runtime* scheduling decision is made
fresh every tick by the gate evaluator, not precomputed.

Three workflow builders exist — `workflows/greenfield.py`,
`brownfield.py`, `ambiguous.py` — each constructing a `WorkflowGraph`
against the same node-id vocabulary (`intake`, `req.analyze`,
`plan.decompose`, `design.*`, `impl.*`, `verify.*`, `release.readiness`,
`summary`, ...). They differ in which nodes exist and how they're
wired, not in the machinery that runs them.

### Dynamic expansion

Section 5.1's `impl.<task_id>` nodes don't exist when the graph is
built — they're created mid-run once `plan.decompose` produces a
`task_graph`. `core/engine.py`'s `GraphExpander` mechanism makes this
race-free:

1. `impl.join` is declared upfront with a **placeholder** dependency
   (`["design.review"]`) — just enough to pass `validate()`.
2. The moment `plan.decompose` succeeds, `Engine._expand_graph_if_needed`
   calls the workflow's expander (`workflows/common.py:expand_impl_tasks`),
   which adds one `impl.<task_id>` node per task (`depends_on=["design.review"]`)
   and **patches** `impl.join.depends_on` to the real list.
3. This happens synchronously, inside the same tick that settled
   `plan.decompose` — long before `design.review`'s human approval could
   ever let anything downstream run. There is no window where
   `impl.join` could incorrectly appear "ready" against its placeholder.

`Engine.resume()` re-runs the same expander against a freshly-built
graph object (which never saw the mutation) before continuing — a
resumed process must reconstruct the exact same dynamic shape, not just
the `RunState`.

## 2. Gates: the only thing that decides readiness

`core/gates.py` implements all 13 condition types from the plan's Section
5.2 table. Two — `upstream_satisfied` and `budget_available` — are
**structural**: they're evaluated on every node's entry gate whether or
not a workflow author declared them, because they're scheduler-level
concerns, not opt-in policy. Everything else (`artifacts_present`,
`schema_valid`, `coverage_threshold`, ...) only fires when a `NodeSpec`
declares it in `entry_gate`/`exit_gate`, since it needs node-specific
parameters.

`upstream_satisfied` is also where join semantics live: `all`/`any`/
`quorum` are just different aggregations over the statuses of a node's
`depends_on` list. There's no separate "join node" type in the code —
`design.review`, `impl.join`, and `verify.gate` are ordinary `NodeSpec`s
with `agent=None` and a `join_policy`.

`GateEvaluator.evaluate_entry`/`evaluate_exit` return a list of
`GateResult`s; `aggregate()` takes the worst of PASS/WARN/FAIL. A FAIL
on exit is what feeds `ErrorRecord(error_class=QUALITY_FAILURE)` and
triggers recovery (§4 below) — it does not simply abort the run.

**Approval is deliberately not a gate condition.** `approval_granted`
exists as a condition type for completeness, but the engine doesn't
require workflows to declare it, because a node that is "ready but
unapproved" must be distinguishable from a node that is "not ready at
all" — see §3.

## 3. The scheduler tick

`Engine.run()` is one `while True` loop:

```
while not all_settled():
    ready = ready_set()                      # entry gates, every candidate node
    if not ready:
        if any AWAITING_APPROVAL: persist and return   # pause
        else: safe-stop("deadlock")
    for n in ready: transition PENDING/BLOCKED/INVALIDATED -> READY
    approved, blocked = partition_by_approval(ready)
    for n in blocked: request_approval(n)     # READY -> AWAITING_APPROVAL
    if not approved: persist and return        # everything ready needs a human
    results = await gather(*[execute(n) for n in approved])   # concurrent, bounded by a semaphore
    for n, r in zip(approved, results):
        settle(n, r)                           # exit gate, artifact/decision recording, SUCCEEDED/FAILED
        recover(n)                             # if FAILED and a RecoveryManager is configured
    if replan.pending(): apply it
    save state
```

`ready_set()` includes nodes in `PENDING`, `BLOCKED`, `INVALIDATED`, and
`AWAITING_APPROVAL` — the last one so a node that was approved *after*
a prior pause is picked back up without a separate code path.
`_partition_by_approval` is the actual approval gate: `requires_approval`
nodes only enter `approved` when `granted_approvals[node_id]` matches
the node's *current* `input_hash` — which is exactly how a stale
approval (input changed after approval) gets silently ignored rather
than honoured. `_transition()` is the single place any node's status
changes; it emits `NODE_STATE_CHANGED` *before* mutating in-memory
state, and derives `started_at`/`ended_at`/`duration_ms` from the
event's own timestamp — the same derivation `events.replay_to_state()`
performs, so a live run and a replayed run agree exactly.

Concurrency is a plain `asyncio.Semaphore` around each node's executor
call, not a separate worker pool — the workload is I/O-bound on LLM/tool
calls, so there's nothing multiprocessing would buy here.

## 4. Recovery: what happens when settle() sees FAILED

`governance/recovery.py`'s `RecoveryManager` classifies the failure
(`ErrorClass`: TRANSIENT, MALFORMED_OUTPUT, QUALITY_FAILURE, POLICY_DENY,
CONTRACT_BREACH, SYSTEMIC — the exception type or gate condition decides
which) and returns a `RecoveryDecision`. `Engine._recover()` acts on it:

- **retry** — `RETRY_SCHEDULED` event, exponential backoff (actually
  `await`ed), re-execute in place.
- **fallback** — `FALLBACK_ENGAGED` event, re-execute in place (the
  node's own executor is expected to behave differently the second
  time — e.g. `test_engineer` regenerating a failing test with the
  failure report appended in a real live-mode run).
- **rollback** — `ROLLBACK_STARTED`/`COMPLETED` events, then calls a
  workflow-supplied `rollback_handlers[node_id]` callable. The engine
  doesn't know what "revert" means for a given node — the workflow does
  (for `workflows/brownfield.py`, it's `git reset --hard` to the
  `impl.join` checkpoint). The node ends `ROLLED_BACK`, a terminal
  status; nothing downstream of it can ever become ready, so the next
  tick's "nothing ready, nothing pending approval" condition safe-stops
  the run — this *is* the "escalate to a human" Section 6.4 describes.
- **safe_stop** — immediate, for SYSTEMIC failures (budget exhaustion,
  a broken audit chain, deadlock).

This is the one part of the loop that isn't exercised by every
workflow: `workflows/greenfield.py`'s cassette never triggers a failure,
so `RecoveryManager` sits idle for that run. `workflows/brownfield.py`'s
fault-injection test (`FaultInjectingProvider(kind="quality", persist=True)`
wrapped around `verify.unit`'s provider) is what actually drives
fallback-then-rollback end to end — see `docs/scenarios/brownfield.md`.

## 5. Context store and re-planning

`core/context.py`'s `ContextStore` is a namespaced, versioned blackboard:
agents never read each other directly, only through `put()`/`get()`/
`view_for()`. `view_for(node_id)` restricts to artifacts produced by
nodes in `node_id`'s *transitive upstream* — this is what makes
`input_hash(node_id)` (a sha256 over sorted `name:version:content_hash`
triples) a meaningful fingerprint of "everything this node's decision
could have depended on."

**Re-planning** (`core/replan.py`) is a direct consequence of that
fingerprint: `compute_invalidation_set(state, graph, context,
changed_node_id)` recomputes `input_hash` for every `SUCCEEDED` node
downstream of `changed_node_id` and flags a mismatch as stale, then
transitively closes the set (invalidating a node also invalidates
everything downstream of *it*, since its regenerated output will itself
change). The engine applies this by transitioning each invalidated node
`SUCCEEDED -> INVALIDATED -> PENDING` and emitting `REPLAN_TRIGGERED` +
one `NODE_INVALIDATED` per node — visible in the event log as the
concrete proof that execution is non-linear, not just structurally
capable of it. See `docs/scenarios/ambiguous.md` for this playing out
end to end with real numbers.

A subtlety worth knowing if you're extending an agent: `input_hash`
covers *every* upstream artifact reachable from a node, not just the
ones a particular agent's prompt happens to read. This is deliberate —
a node's true inputs are broader than what one agent chooses to use —
but it means an agent that doesn't read a changed artifact will still
be correctly invalidated (its own future run might use it, or another
downstream node depends on the same distinction being conservative
rather than agent-specific).
