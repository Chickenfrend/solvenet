# Coordinator claim graph (G1–G2)

The SQLite claim graph is separate from the work graph. Task `parent_id` still
means work parentage; a task's additive `claim_tasks` record identifies its focused
claim and action. Worker assignments still belong to jobs and do not identify
logical agents. The coordinator persists and inspects knowledge and ingests
explicit graph JSON from completed model responses. The fixed group-loop
scheduling policy remains A1–A6.

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
authorizing graph proposals in completed model text is described below.

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

## Completed response ingestion (G2)

`Store.ingest_group_graph_response(group_id, job_id)` and the corresponding
`Coordinator` method read the **persisted completed response**, not caller-supplied
text or identity. Only `model.respond` jobs of type `plan`, `finding`, or `critique`
are eligible. New completions and duplicate result deliveries automatically call
the same method inside the result transaction. Operators can call it explicitly
after an upgrade/restart to ingest a completion persisted before G2.

The entire `output.text` must be a JSON object with the explicit marker
`"graph_schema":"solvenet.graph.v1"`. Prose, fenced JSON and objects without the
marker retain their previous behavior. Unsupported versions and invalid explicit
batches produce a durable rejection receipt. Invalid JSON is ordinary informal
text; no heuristic extraction promotes fragments to proposals. Duplicate JSON
fields are rejected. Existing `approaches` and `decisions` fields can coexist with
the graph arrays, preserving the fixed six-call path.

Example finding (replace `ROOT_ID` with a same-group claim ID):

```json
{
  "graph_schema": "solvenet.graph.v1",
  "claims": [
    {"key":"lemma", "statement":": True", "imports":["Init"],
     "environment":"lean-test", "reason":"Useful intermediate claim"}
  ],
  "relationships": [
    {"key":"use", "from":"ROOT_ID", "to":"$lemma",
     "kind":"suggests_using", "reason":"Try this approach"}
  ],
  "reviews": [
    {"key":"review", "relationship":"$use", "status":"promising",
     "reason":"Worth investigating"}
  ],
  "findings": [
    {"key":"note", "claim":"$lemma", "text":"Informal evidence only"}
  ],
  "artifacts": [
    {"key":"proof", "claim":"$lemma", "statement":": True", "imports":["Init"],
     "environment":"lean-test", "proof":"trivial"}
  ]
}
```

Arrays are optional. Per-response limits are eight claims, eight relationships,
eight reviews, eight findings, four artifacts, and **16 total items**. Keys must
be unique across all arrays and match `[A-Za-z0-9_-]{1,32}`. References are either
persisted same-group IDs or `$key` references to claims/relationships introduced
in this response. Claims are inserted before relationships, then reviews,
findings and artifacts; no local reference escapes its job. Findings are capped
at 2 KiB each, reasons at 2 KiB, and the G1 context and per-group capacity limits
still apply. Formal proposals are finding-job-only and must repeat the focused
claim's **exact** statement, ordered imports and environment. A changed statement
requires a new claim; it cannot mutate an old node or transfer its verification.

The complete UTF-8 JSON text, including syntax, escaped strings and every ignored
field, is capped at **8192 bytes**, matching Python and Go typed-result limits.
An oversized typed response fails the existing worker/coordinator formatting
boundary. The outer result request double-encodes this JSON string and includes
usage/generation fields; its existing 2 MiB transport limit remains in force.
These are response limits, not G4 ranked/frozen context packets: existing group
prompt/context admission and worker message limits remain in force.

Every claim publication, relationship, review, finding and formal proposal uses
the job's actual agent/task and successful completed assignment. Model-supplied
agent, task, job, reviewer, assignment, verifier identity and `verified` fields
are ignored; they never grant authority. Review `status` is independently
validated as `promising`, `challenged` or `abandoned`. Unknown extra fields are
ignored but charged to the encoded-byte limit. Findings remain unverified, and
artifacts enter `pending` for the existing pinned Lean verifier. Auxiliary checks
never solve the target run. Coordinator-authored writes remain a distinct source;
historical reviews default to `coordinator` with no invented job provenance.

All writes for one response, including exact-match publications and evidence
links, share a savepoint and durable job/assignment receipt. A bad reference,
context, shape or capacity rolls back **all** batch graph writes and their
revision increments. The successful model call is retained, with a rejected
graph receipt; rejection does not retry model work. Unexpected coordinator/crash
errors roll back the enclosing result transaction, allowing the same delivery to
be retried. Accepted and rejected receipts are idempotent across restart, even
if graph capacity later changes. The receipt maps proposal keys to persisted
IDs and is exposed as `group(id)['graph_responses']` through existing inspection.
Separate jobs publishing an identical lemma retain separate publications and
evidence. Schema 20 adds receipts and review/evidence assignment provenance;
historical artifact/message assignment IDs remain unknown (`null`).

G2 supplies proposals and reviews to the coordinator; it does not autonomously
redirect tasks or schedule a graph frontier. A coordinator can create a new
focused task after a challenge through the existing task method; the fixed loop
continues to apply its existing `decisions`-based redirect. Graph relationships
do not compose Lean declarations or prove dependencies (G3), rank/freeze worker
packets (G4), or replace scheduling (G5).

The opt-in [G5 frontier policy](graph-frontier.md) now consumes this graph and
adds bounded attributed priorities/help requests. The fixed loop remains the default.
