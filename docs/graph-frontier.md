# Graph-driven frontier (G5)

`POST /v1/groups` and `Store.start_group_loop` accept `mode: "graph"`.
Omitting it keeps the fixed A1–A6 loop. Independent and repair runs retain their
standalone verification path. Graph mode uses the same persistent five agents,
SQLite jobs/leases, Go worker protocol and pinned composed verifier.

Example request:

```json
{
  "request_key": "graph-example",
  "statement": ": True ∧ (True ∧ True)",
  "imports": ["Init"],
  "environment": "local-pinned-lean",
  "mode": "graph",
  "max_work": 24,
  "models": {
    "planner": "scripted", "investigator": "scripted",
    "critic": "scripted", "synthesizer": "scripted"
  },
  "graph_limits": {"verification_operations": 12}
}
```

The policy in `coordinator/src/solvenet/frontier.py` is a small rule table, not an
optimizer or a dependency-solving phase machine. It serializes frontier work
initially, so a completed result can inform the next selection. It considers at
most 16 claims and 32 suggestion edges within three outgoing hops of the target.
Stable claim IDs break ties; bounded attributed priorities break ties within a
rule tier. The order is:

1. One initial decomposition call.
2. Try the target with currently checked, relevant, non-challenged context.
3. Independently critique a challenged suggestion, rejected formal branch, or
   help request.
4. Prove a promising, investigated, or agent-prioritized claim.
5. Investigate an unresolved suggested claim.
6. Try the target directly, including when suggestions are abandoned or reciprocal.
7. Replan on observed checked/rejected/timed-out evidence (including target verdicts), reviewed challenges or
   failed jobs, with at most two additional planning calls.

Each tier is still subject to routing, packet admission and caps. An unavailable
or unfit action is deferred and a lower-ranked viable action may win. Exhausted
equivalent actions become explicit dead ends. An unreachable initial planner
terminates with a capacity/model-budget reason. Critic opinion never changes a
checked artifact's mathematical status. A pending proof is checked before making
the next choice; only a checked target can solve the run.

## Agent proposals and immutable decisions

G2 JSON (`graph_schema: "solvenet.graph.v1"`) supports the existing claims,
relationships, findings, reviews and artifacts, plus:

```json
{
  "graph_schema": "solvenet.graph.v1",
  "priorities": [{"key": "focus", "claim": "CLAIM_ID", "priority": 3, "action": "prove", "reason": "Promising branch"}],
  "help_requests": [{"key": "help", "claim": "CLAIM_ID", "action": "critique", "reason": "Check this suggestion"}]
}
```

Each new array permits four items, sharing the existing 16-item/8 KiB batch
ceiling. Actions are `investigate`, `prove`, or `critique`; priorities are integers
0–3. Reasons are bounded to 512 bytes. References must resolve within the group
or to a claim introduced by the same job (`$key`). They are attributable to the
actual completed job/agent/assignment and participate in the same atomic,
idempotent graph ingestion. They are requests, not scheduling authority.

Each choice atomically freezes its reason, deferred alternatives, input packet
revision, focused task, job, routing explanation, weighted model-work reservation,
full G4 packet/messages/hash and selected G3 manifest. Packet retries are identical
even after graph changes. Deduplication keys include claim, action, explicit
strategy and exact selected declarations, not work-induced graph revisions.
At most one independent retry of a completed strategy is allowed by default;
it gets explicit retry lineage and uses the other investigator or an alternative
model where available. Worker lease retries reuse the original reservation/job.
Agents' IDs persist across model routing changes and coordinator restart.

A challenged incoming suggestion is supplied to the focused critic by explicit
relationship/review IDs persisted in the frozen request. Its review reason is
supplied as a bounded JSON-encoded excerpt, preserving exact IDs, endpoints,
status and provenance under the existing category, packet and prompt limits;
unrelated incoming relationships are not broadcast. Investigator packets likewise
carry the particular suggestion that selected their focus. A trigger that cannot
fit defers that action rather than dispatching an evidence-free critique.

Committed target rejections/timeouts are durable replanning events even though
their generation jobs finish as `done`. A replanner receives the latest negative
target attempt/job IDs, verdict and JSON-quoted diagnostic excerpt (400-byte
excerpt bound, 1000-byte category ceiling). The persisted evidence fingerprint
deduplicates unchanged events across restart, and the existing three-plan and
reserved planning/critique assignment caps still apply.

For an auxiliary rejection, the focused critic receives an explicitly selected
`untrusted.rejected_artifacts` row containing the exact candidate proof, persisted
Lean diagnostics, verdict and artifact/job/task/claim/verifier identifiers. A
completed critique can publish a focus-claim finding with a concrete retry
strategy. With `retries=1`, the frontier can dispatch one further investigation
of that auxiliary, carrying both the rejection row and the exact critique message.
`retries=0` disables this repair. The retry has investigator task lineage; it
cannot reset its count through new artifacts, graph revisions or critique messages.
No useful critique finding means no repair assignment. Rejected candidates remain
untrusted and are never selected as checked declarations.

Opt in with `direct_auxiliary_correction=1` to send the first Lean-rejected
auxiliary directly to its original logical owner and model, without an intervening
critique. `retries=1` allows one correction per claim; `retries=0` disables it.
The correction freezes the rejected job's complete supplied manifest and checked
lemma packet, plus the exact artifact proof, diagnostics and attribution. It
submits a separate graph artifact with parent-task lineage. Diagnostic text is
never classified to decide whether a failure is mechanical or mathematical.
An explicit challenged strategy still routes to critique. Repeated failure,
timeout or verifier error routes to bounded critique, with no further auxiliary
correction in this mode. Critique remains subject to planning, work and completion
tail admission. Artifact multiplicity, context changes and restart cannot reset
the correction allowance. An unavailable owner or oversized exact context defers
correction and permits target fallback.

Selected rejection and critique evidence is required, not silently pruned or
shortened. Rejections have a 3000-byte JSON category ceiling; critiques share the
existing 2000-byte message category. If exact feedback exceeds category, packet
or prompt limits, the action is deferred. Compact feedback-specific graph
instructions preserve the tactic-body contract within the existing input bounds.
Ordinary direct-target fallback, global work/deadline limits, planning/critique
assignment reservations and verification limits still apply.

## Separate ceilings and cost accounting

`graph_limits` may configure smaller values than these defaults, except
`response_output_tokens`, which accepts 1–32768 (the protocol output ceiling):

| Limit | Default |
| --- | ---: |
| `planning_calls` (reserved planning/critique assignments, including lease retries) | 8 |
| `verification_operations` | 24 |
| `lean_elapsed_ms` | 180000 |
| `included_lemmas` | 8 |
| `source_bytes` (encoded replay inputs and generated Lean source) | 32768 |
| `packet_bytes` (full encoded graph packet) | 6144 |
| `retries` (independent retries per strategy) | 1 |
| `direct_auxiliary_correction` (opt-in direct owner correction, 0 or 1) | 0 |
| `response_output_tokens` (non-synthesis plan/finding/critique output) | 512 |

Synthesis continues to request 2048 output tokens. The configured response
allowance is included in packet/context admission and the queued job request.

Existing group `max_work` remains the weighted model reservation ceiling (two
assignments at configured model cost); it is distinct from measured model calls,
tokens and Lean operations. A smaller planning cap can reserve a single lease.
G4 also enforces the existing 8 KiB complete input bound, trusted theorem/import
wrapper, conservative provider framing, output allowance and model context fit.

A logical composed operation reserves a durable slot **before** verifier
execution. Failed, timed-out, uncommitted/superseded and restarted checks consume
slots, including dependency rechecks after verifier rebinding. A logical operation
may involve several Lean subprocesses: the pinned composed verifier records
preflight and proof subprocess counts separately, even on rejection. Unknown
subprocess counts stay explicitly unknown. Unknown elapsed time, including an
unfinished reservation after a crash or verifier exception, is conservatively
charged one verifier deadline rather than zero. Known elapsed time is checked
before the next operation; serial execution bounds cumulative-cap overshoot by
one per-operation deadline. Smaller source limits are applied to generated Lean
source too, before launching subprocesses.

Each composed attempt/artifact has one durable exclusive verification owner,
acquired atomically with its operation reservation in a short transaction.
Overlapping ticks skip a busy or already-completed input without buying another
slot or writing a rejection. Ownership lasts for the verifier deadline plus a
five-second commit grace; no database transaction spans Lean execution. After
restart, an unexpired owner remains busy until that bound. Recovery marks an
expired check `abandoned`, retains its spent operation and unknown deadline-time
charge, and admits a recheck only if capacity remains. An abandoned token cannot
commit a late result over its replacement. Schema 24 adds this ownership record;
pre-upgrade unfinished checks are retained as abandoned operations.

The frontier preflights operation and cumulative-time capacity before reserving
more proof-producing model work. For example, if an auxiliary consumes the last
operation, the group stops with `verification_budget` before creating a target
synthesis job. Active dispatched work drains ahead of this preflight. A genuine
capacity refusal for a still-unchecked target cancels its verification work with
an explicit group stop reason; it is not recorded as a Lean rejection.

At deadline, queued model jobs are cancelled and no auxiliary verification starts.
An already-dispatched valid target may drain after restart, using the same durable
operation/time ceilings. Budget-denied work never executes Lean. Terminal reasons
include verified target, deadline, verifier error, task limit, capacity/model
budget, verification budget, Lean-time budget and no useful frontier.

`GET /v1/groups/{id}` exposes `group.frontier`: selected/deferred decisions,
packet hashes/sizes/admission budgets, exact selected manifests, model reservation
totals and logical Lean operations/subprocesses/time with unknown counts.
Existing `calls`, `cost`, graph receipts and composed evidence retain model calls,
failures, unknown tokens, verifier bindings and actual declaration-use receipts.
The private `Store.frontier_trace` returns the same compact trace.

## Validation scope

`coordinator/tests/test_frontier.py` exercises paired three-branch scenarios whose
checked/rejected/challenged evidence changes the next action, actual auxiliary
and target proof use with real pinned Lean, immutable restart/lease retries,
independent retry lineage, reciprocal/abandoned suggestions, model failure and
routing, caps, controlled concurrent target/auxiliary/deadline ticks, superseded
checks, abandoned-owner restart recovery, unknown costs and deadline draining.
Run with the pinned toolchain on PATH:

```sh
PATH="$HOME/.elan/bin:$PATH" PYTHONPATH=coordinator/src python -m unittest discover -s coordinator/tests
```

These deterministic tests validate policy and proof-composition behavior. They
do not measure real-model effectiveness or compute efficiency. The
[G6 demonstration](graph-demonstration.md) exercises process-level multi-worker
composition and documents a capped local-model observation.
