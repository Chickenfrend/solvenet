# G3: immutable composed proof checks

Composed verification is a coordinator-private local operation. It checks any
focused claim; an auxiliary artifact still never solves a group run. Only a
completed target `model.generate` candidate can solve that run. Independent and
repair v1 jobs retain standalone verification.

## Local execution trust boundary

These checks and receipts operate under the existing **local execution
assumption**. Candidate elaboration and the checking commands execute in the
same Lean process. A tactic with malicious IO can write the candidate-writable
receipt, terminate that process, or otherwise interfere with the checker. A
receipt is therefore a completion/use record for normal local execution, **not
an attestation from a separately trusted process against malicious tactic IO**.
The theorem/type/universe/axiom checks remain in place, but a forged receipt
cannot establish that they ran. Docker isolation limits access and resources;
it does not separate candidate code from these in-process checks.

The rejection tests cover malformed declarations, axiom-dependent proofs,
incompatible inputs and stale/competing results within this boundary. They do
not claim resistance to a candidate that attacks the checker through tactic IO.
Public/hostile-worker acceptance remains blocked by the existing separate
trusted-checker gate. Closing that process-integrity gap is a public-stage
blocker, not a prerequisite claimed to be solved by this local G3 implementation.

## Selecting proofs

`Store.propose_group_artifact(..., prerequisite_proof_ids=[...])` selects exact
previously checked artifact IDs. A structured `solvenet.graph.v1` finding can
also supply `prerequisite_proof_ids` on an artifact. These are existing proof
IDs, not job-local claim references or planning relationships. The completed
finding's actual agent/task/assignment remains the proposal's authority.

The coordinator resolves each selected proof's frozen prerequisites and records
a deterministic dependency-order manifest. Every declaration has its claim ID,
proof ID, generated Lean name, exact statement, proof body and prerequisite
proof IDs. The common imports and declared environment must match exactly.
Different proof artifacts for the same claim are permitted. Names are generated
from proof IDs (`SolveNetLemma_<digest>`), never accepted from model text.
Only currently verified artifacts with the current verifier identity are
eligible for a new selection. The operation rejects cycles, name collisions,
renames, changed previously frozen proofs, incompatible contexts and oversized
closures. Limits are eight declarations, depth eight, and 32 KiB of encoded
aggregate replay inputs (including the candidate proof); generated Lean source
is also capped at 32 KiB.

The fixed group loop selects up to three compatible checked artifacts for its
target synthesis, freezes their complete transitive context with the queued job,
and supplies their generated names in the synthesis prompt. Every graph-enabled
target, including one with no selected lemmas, requires a fresh non-None verifier
binding and a frozen manifest. An unavailable or stale binding delays dispatch;
a missing target manifest cannot fall back to standalone verification.
The narrow private
`select_target_proof_context(job_id, proof_ids)` method also supports explicit
selection for a queued group target job. It cannot redirect generation to an
auxiliary claim. This is context selection, not a graph-driven scheduler.

## Lean checks and use semantics

The composed checker builds a fresh Lean source file and rechecks **every**
supplied declaration, even an unused one. Each theorem uses the existing command
parser, theorem-kind check, exact expected type and universe-parameter check,
and transitive allowed-axiom check. `sorryAx` and every generated
`SolveNetExpected*` axiom are forbidden by those checks, including when explicitly listed
in the verifier's allowed-axiom configuration. Arbitrary unallowed axioms are
rejected under the local execution assumption above. A bounded completion receipt
is mandatory, without providing hostile-process attestation. The same wall-clock and
diagnostic limits cover environment preflight and the assembled proof check.

The pinned Lean receipt inspects elaborated expressions, not candidate text:

- `direct`: supplied declarations occurring as constants in the focused
  theorem's **proof value**;
- `type`: supplied declarations occurring as constants in its **type**;
- `transitive`: supplied declarations reachable from either of those roots,
  recursively traversing both the types and values of referenced declarations.
  This includes direct use and type dependencies. Its traversal is bounded at
  100,000 visited constants; exceeding the bound rejects the check.

Arrays are in manifest order. Supplied declarations remain separate from these
used subsets. An unavailable or failed inspection has `usage_unknown`; it is
never treated as proof of a dependency. The pinned checker requires a valid
known receipt for successful composed verification. Planning edges are not
rewritten based on a receipt.

## Persistence, verifier binding and replay

Migration 21 adds immutable `proof_contexts` and retained `composed_checks`.
Each check retains the complete replay bundle, outcome, elapsed time, use
receipt and whether its result committed. `committed` is true only if the
authoritative artifact status UPDATE or target verification INSERT actually wins.
Duplicate and competing completions retain their outcomes with `committed=0`;
they cannot replace the winning replay bundle. The attempt and exact authorized
bundle are persisted before Lean runs; a crash leaves an identifiable incomplete
`checking` record. Only its completion fields are finalized. Checks run outside SQLite
transactions. Commit compares the exact frozen inputs and the current verifier
**identity and monotone binding revision**, protecting against A→B→A changes.
An unrelated graph revision does not invalidate a check.

A changed verifier can recheck the exact frozen source under a newly authorized
binding; that verification attempt records its own identity/revision without
editing the original selection. All supplied proofs are rechecked together,
so replay does not require current eligibility of mutable graph rows. Local
checks fingerprint the actual selected compiler (including direct elan launch),
effective ordered `LEAN_PATH`, external import roots, toolchain standard library,
and runtime library metadata. Lake probes use its effective execution environment.
The effective `LD_PRELOAD` setting and each explicitly referenced library's
resolved file metadata are also bound, including relative paths resolved against
the verification project cwd and space/colon-separated preload order. Missing
files, bare sonames requiring loader lookup, dynamic loader tokens such as
`$ORIGIN`, and over-budget preload lists return an unavailable identity rather
than guessing the loaded library. This is a narrow metadata binding, not a
general loader resolver or sandbox.
Metadata walks are capped at 128 roots, 100,000 entries and five seconds; an
over-budget or unavailable identity fails closed. These scans run only when
checking eligible work or selecting a graph target, not on idle ticks. Docker checks run the immutable
image ID in the bundle, rather than its mutable tag.

`Store.export_proof_bundle(artifact_or_attempt_id)` returns the last committed
check's self-contained JSON inputs (or the original selection before a check).
For a target attempt, it contains the actual completed candidate proof, not the
empty proof placeholder stored with its queued job. Serialize this JSON and
pass it to the authorized verifier's `verify_composed(bundle)` to replay after
restart; no graph or artifact queries are needed. `composed_evidence(id)` exposes
historical checks and actual-use receipts separately from claim status.

Acceptance tests in `coordinator/tests/test_composed.py` use pinned Lean 4.19.0
for A standalone, B using A, and a target using B, including restart/replay,
unused declarations, alternative proofs, type dependencies, malformed and axiom-dependent proof
inputs, binding ABA and unrelated graph changes. The target integration runs the
fixed loop with auxiliary findings and asserts target-attempt storage. The
Docker request/identity test uses a transport fixture; it is not a live Docker
execution or a measurement of real-model effectiveness.
