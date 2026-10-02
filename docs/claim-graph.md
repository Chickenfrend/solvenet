# Coordinator claim graph (G1)

The SQLite claim graph is separate from the work graph. Task `parent_id` still
means work parentage; a task's additive `claim_tasks` record identifies its focused
claim and action. Worker assignments still belong to jobs and do not identify
logical agents. This ticket persists and inspects knowledge; it does not change
the fixed group-loop scheduling policy or ingest new graph JSON from model text.

## Immutable claims and sources

New group creation atomically inserts its target root, a coordinator-authored
publication, and its revision record. A claim's statement, ordered imports list
(including duplicates), and declared environment are immutable. Only **exact**
matches of all three within the same group deduplicate. Whitespace changes,
different imports/order, and different environments create different nodes.
There is no mathematical-equivalence matching and publication grants no formal
verification status.

`Store.propose_group_claim(group_id, request_key, statement, imports, environment,
agent_id=..., task_id=..., job_id=..., reason=...)` returns `(claim_id,
publication_id)`. Repeating the same key and contents returns both original IDs;
changing contents conflicts. A different key always preserves a distinct
publication, even on an existing node. Coordinator publications may omit agent
and task; if supplied they must identify a same-group task and its owning agent.
Job publications additionally resolve the actual completed assignment from the
job's persisted agent/task association. These are coordinator-private methods,
not permission for workers to assert identity or verification. Parsing and
authorizing graph proposals in completed model text is G2.

## Advisory relationships and review

`propose_group_relationship(group_id, request_key, from_id, to_id, kind, ...source)`
retains the same source information and a bounded reason:

* `suggests_using(A, B)` suggests that B may help prove A. It is not a required
  prerequisite, an actual proof use, or a theorem about either claim.
* `alternative_to(A, B)` records the proposed alternative A to B.
* `supersedes(new, old)` proposes replacing the old statement with a corrected
  node. The old node, publications and proof artifacts remain intact. Supersession
  is advisory; it does not invalidate the old theorem or transfer its proofs.

All links require distinct, same-group nodes with identical imports/environment.
Reciprocal links and suggestion cycles are allowed. No suggestion creates the
acyclic proof-use manifest planned for G3.

`review_group_relationship(group_id, request_key, relationship_id, reviewer_id,
status, reason)` appends an attributable `promising`, `challenged`, or `abandoned`
review. Reviews have their own IDs and idempotency keys and retain history; a
reader can inspect their insertion order. They do not change Lean results.

## Work and evidence

`add_group_task(..., claim_id=..., action=...)` atomically attaches new work to
an existing claim. In new groups its default focus is the target root. Actions
are `investigate`, `critique`, `prove`, and `synthesize`.
`attach_group_claim_task(group_id, task_id, claim_id, action)` adds a missing
focus; an existing focus is immutable (use a new task for a new focus/action).
The fixed loop explicitly records root-focused work rather than interpreting
its informal approach strings as mathematical claims.

New messages inherit their task's focus. Formal artifact proposals publish their
**own exact statement/imports/environment**, with their existing checked source,
and attach to that claim. They do not inherit a task's potentially different
claim or verify its target. `attach_group_claim_evidence(group_id, claim_id,
kind, evidence_id)` can attach existing `message` or `artifact` evidence;
artifact contexts must match exactly. Informal evidence may concern several
claims. Review status on messages and relationship reviews stays independent
of formal artifact status. Several artifacts can prove or fail on one claim.

Formal outcomes retain diagnostics and actual verifier identity/revision in an
append-only history. The existing artifact status may return to `pending` when
the active verifier changes; its old outcome is still inspectable. Migration
copies v18 terminal outcomes before any verifier invalidation, preserving known
identity and diagnostics; their unknown historical binding revision is `null`.
An inspection
only reports `current_status=verified` when a supplied observed identity matches
both the active coordinator binding and the checked artifact identity. No
inspection or auxiliary artifact marks a target run solved.

## Revision, bounds and inspection

Graph revisions are monotone counters, not timestamps or proof validity tokens.
Transactional triggers advance the counter for new claims, publications,
relationships, reviews, work/evidence links, focused task status changes,
linked message reviews, and linked artifact verification/eligibility changes.
Focused task work reservations and availability changes, group work availability,
and creation/status transitions of jobs on focused tasks also advance it within
the same transaction.
Exact retries do not advance it. Existing fixed-loop writes use the same links
and triggers. Workers and idle schedulers do not fetch the graph.

Local per-group ceilings are 64 claims (including the root), 256 publications,
256 planning relationships, 256 relationship reviews, 512 links per evidence
kind, and 256 retained formal outcomes. Existing group task/message/artifact
ceilings also apply. Claim/import/environment bytes use existing protocol
limits; proposal and review reasons are capped at 2 KiB. Capacity checks and
inserts share immediate transactions, so failed publication or attachment rolls
back its node, artifact, task, budget and revision changes.

`group_claim_neighborhood(group_id, claim_id=None, depth=1, max_nodes=16,
max_items=32, max_bytes=65536, verifier_identity=None)` defaults to the root and
returns a single read-transaction snapshot containing metadata/revision, claims,
publications, relationships, reviews, focused tasks, informal messages, artifacts,
and historical outcomes. Neighborhood traversal follows planning links in both
directions and uses group-scoped endpoint indexes. It is capped at three hops,
32 nodes, 64 rows per category and 256 KiB of complete JSON-encoded output.
Traversal tie-breaking uses stable claim IDs, and category rows use insertion
order. Category fetches are batched, not one query per claim. The result includes
row/byte omission counts within the selected neighborhood and a
`traversal_truncated` flag for the node cap; whole records are omitted rather than
editing statement text. Review omission counts include reviews of every
relationship within the selected nodes, even when that relationship is excluded
by the edge row cap. Returned reviews reference returned edges; their count and
fetch use bounded batched joins. Extremely small byte budgets fail if metadata cannot fit.
This is inspection data, not a frozen/ranked worker context packet (G4).

Schema 19 gives preexisting groups an empty graph/revision zero and **no root or
historical links**. Their v1 and fixed-loop data still load normally. Explicit
graph methods may add claims/links later; new work in a historical group has no
inferred focus. Independent v1 runs create no graph records.
