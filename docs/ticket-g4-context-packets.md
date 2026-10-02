# G4: relevant, frozen graph context

`coordinator/src/solvenet/context_packet.py` builds coordinator-owned context for
an explicitly focused task. Scheduling remains the existing fixed group loop;
these APIs also let a future scheduler dispatch a focused task without adding a
frontier policy here. The Go worker and its job/result contract are unchanged.

## Selection and trust

A packet reads the focus and its **outgoing one-hop** planning neighborhood,
with at most 16 claims and 32 rows per evidence category. An investigator does
not receive the target's other branches merely because both branches point back
to the target. An explicitly supplied checked proof can additionally bring its
necessary G3 declaration closure; that closure is mathematical context, not a
transcript broadcast. The fixed loop retains its existing explicit auxiliary
proof selection at synthesis for compatibility.

Ranking prefers the focus and explicit task/suggestion relevance, then reviewed
evidence and review recency, then stable IDs. Informal findings record review
recency with a monotone graph revision when reviewed. Historical reviews lacking
that field use publication order. Relationship eligibility consults the latest
review **per returned edge**, even when older review history exceeds the row cap.
An additional indexed, one-row-per-neighbor opinion aggregation prevents omitted
edges (including duplicate suggestions) from hiding a current challenge. Its
stable witness review IDs are frozen with packet source IDs.
Node traversal is stable-ID bounded; ranking is applied within that queried
neighborhood rather than promising a global relevance search.
Outgoing neighbor IDs are deduplicated before the window count and node cap;
repeated suggestions do not consume extra node slots or inflate claim omissions.

The packet has separate `untrusted` and `checked_lemmas` objects. All model text,
including findings, reasons, challenges and review notes, stays inside JSON
strings. Formal artifact status/current eligibility, finding usefulness review,
and relationship planning status remain independent. A challenged or abandoned
suggestion normally excludes its lemma from selected context; its checked status
remains visible in artifact evidence. An explicit selection can retain the lemma
with its planning opinions marked. Proof-required prerequisites remain supplied
and carry the opinions known in this neighborhood.

Only `selected_bundle` may authorize declarations. It checks current verifier
binding, exact imports/environment, immutable prior proof inputs and the bounded
acyclic closure. A newly opened Store must bind its active verifier before a new
packet advertises current eligibility. Explicit proof selection (including an
empty explicit selection) requires the local verifier identity/revision token to
match the persisted binding; reopening, another Store's binding change, or an ABA
requires a fresh local bind before selection. Historical Lean outcomes can remain
visible without authorizing a stale declaration. The packet includes the exact
generated Lean name, **complete statement**, claim/proof IDs, prerequisite IDs and
source provenance for every supplied declaration. Types and other rows are
removed whole, never shortened. Packet lemmas and the frozen G3 manifest agree
after pruning. The manifest retains all ordered proof bodies for verification.

## Bounds and omissions

The UTF-8 packet, including all JSON escaping and metadata, is at most 6 KiB.
Encoded array ceilings (bytes) are:

| Category | Ceiling |
| --- | ---: |
| Claims | 1000 |
| Planning relationships | 1000 |
| Publications | 600 |
| Relationship reviews/challenges | 1000 |
| Findings/messages | 2000 |
| Tasks | 600 |
| Formal artifact summaries | 1000 |
| Historical outcomes | 600 |
| Supplied checked declarations | 2400 |

Every category has an omission count, including empty categories. These count
row, category-byte and complete-prompt pruning in the queried neighborhood.
Lemma omissions also count ineligible/challenged candidates and candidates
excluded by the selection-operation cap. Dependency declarations removed during
prompt pruning are counted too. `traversal_truncated` distinguishes a capped
neighborhood; claim omissions include discovered neighbors outside its node cap.
Counts do not purport to inventory evidence in undiscovered branches.

The graph inspection ceiling is 256 KiB before the smaller packet selection.
Query count for the neighborhood depends on bounded depth, not returned node
count. At most eight candidate-selection attempts and eight pruning operations
resolve G3 closures of at most eight declarations/32 KiB composed source. These
operations use indexed, group-scoped lookups, not full graph reads. Tests bound
query count for branching graphs both with and without eligible proofs.

### Complete prompt admission

Packet size alone is insufficient: quoting packet JSON in a job message adds
another encoding layer. Admission charges the **complete** job messages, the
worker's trusted theorem/import wrapper, conservative ASCII/HTML JSON escaping,
a 1536-unit provider-system/framing margin, and the requested output-token
allowance. The complete input byte bound is also capped at 8 KiB, even for a
larger configured token window. Whole optional rows are removed until the bound fits; an oversized
required focus, target or original message rejects creation transactionally.
The fixed loop stops with `context_limit` when those required inputs cannot fit.

`context_bytes` in routing still caps the conservative encoded **input** byte
bound. Optional model capability `context_tokens` caps a separate conservative
whole-window upper bound: at most one input token per encoded input byte, plus the
known output-token limit. This is a byte-tokenizer admission upper bound, not an
average bytes/token estimate. An unknown window is reported as `null`; measured
input tokens remain unknown, never filled with the admission calculation.
Snapshots record known packet bytes and the labelled admission upper bound.

Configure `context_tokens` to the actual worker/provider window. For example,
the fixed-loop HTTP integration configures both `context_tokens: 8192` and
worker `-ollama-context 8192`; the Go default is 4096 tokens, which can be too
small for conservative composed-proof admission. The packet API's
`context_limit` is the complete-window admission ceiling (8192 by default),
not an assertion that an unconfigured provider actually has that window.
Provider overhead is reserved conservatively because the unchanged worker does
not advertise its full provider framing. Larger margins can reject prompts
whose actual tokenization would fit; no tokenizer measurements are invented.

## Atomic dispatch, retry and inspection

`Coordinator.build_group_context_packet(group_id, task_id, messages, **limits)`
previews a packet. It is not a reservation. Dispatch with:

```python
job_id = coordinator.enqueue_group_context_job(
    group_id, task_id, agent_id, request_key, environment,
    model, 'finding', [{'role': 'user', 'content': 'Investigate the focused claim.'}],
    cost=2, max_output_tokens=512, context_limit=8192,
)
snapshot = coordinator.job_context_packet(job_id)
```

For a target proof, pass `task_type=None`, `kind='model.generate'`, and an
appropriate output allowance. Generate jobs remain **target-only** and require
a freshly bound verifier. Auxiliary proofs continue to arrive in findings.
The underlying Store API is `enqueue_group_job(..., graph_context=True)`; its
existing default behavior is preserved for callers not requesting a packet.

Schema 22 atomically persists the full packet as a BLOB, SHA-256, graph input
revision, source IDs, complete selected proof manifest, exact serialized
`jobs.messages`, original request and admission metadata with job insertion and
work reservation. The proof context is frozen in the same transaction. Packet
and job prompt/generation fields are immutable. A failure in freezing rolls back
the job, run creation and reservation. The snapshot revision denotes the graph
read that selected context, before the job's own graph-change triggers.

An idempotent enqueue checks the original request and returns the original job,
without rebuilding context. Worker lease expiry, intervening graph changes,
review changes, and restart therefore preserve packet bytes/hash and complete
job messages. A later task may preview fresh context. Inspection returns `packet`
as bytes, parsed source IDs/manifest/messages, and budget metadata. It does not
consult current graph state to reconstruct a historical snapshot.

The fixed plan/investigate/review/redirect/synthesize ordering is retained. Its
synthesis context now uses the packet rather than truncated statement summaries.
Its explicit informal review/redirect handoffs remain bounded and JSON-quoted.
The scripted HTTP fixture checks the complete actual provider prompt, including
the worker-prepended trusted messages and output allowance. This is protocol and
Lean integration evidence, not evidence of real-model efficiency.

## Checks

```sh
PATH=/home/ben/.elan/toolchains/leanprover--lean4---v4.19.0/bin:$PATH \
  PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -q
PATH=/home/ben/.elan/toolchains/leanprover--lean4---v4.19.0/bin:$PATH \
  PYTHONPATH=coordinator/src:homelab/src:integration \
  python3 -m unittest integration.test_group_collaboration -q
```

Also run `go test ./...` from `worker/`. Packet tests cover relevant new checked
lemmas and unrelated branches, challenged/stale statuses, adversarial/huge text,
complete formal types and closures, review history/recency, encoded overhead,
atomic rollback, immutable prompts, duplicate enqueue, restart/worker retry,
target-only dispatch, and bounded query/packet work. Existing G3 tests exercise
real pinned Lean and replayable composed proof authority.
