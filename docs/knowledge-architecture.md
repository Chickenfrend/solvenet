# Knowledge architecture direction

**Agreed direction, 2026-10-07.** This describes planned knowledge-store work,
not delivered features. The group claim graph and proof-artifact history exist;
the reusable library catalog, shared Postgres service and publication/cache bridge
are not implemented yet.

## Ownership and database breakdown

| Store | Responsibility |
| --- | --- |
| Homelab site SQLite | UI/operator settings, model cards and service configuration; reads research data through APIs. |
| Local coordinator SQLite | Problems, agents/tasks, attempts, private claims, artifacts and verification history; initially also the reusable catalog and cached shared knowledge. |
| Shared-service Postgres (planned) | Admitted shared library imports, published claims/proof artifacts, dependency edges, sources/licenses, publications and verification evidence. |

“Local” means a SolveNet installation, including homelab and standalone instances.
The homelab website's settings database does not own research knowledge. Preserve
both existing SQLite databases; initially extend coordinator SQLite rather than
creating a separate catalog database. Agents access knowledge through the local
coordinator/knowledge service; the homelab UI accesses it through APIs.

Postgres is explicitly planned when creating the shared service. It does not
replace local SQLite or become a prerequisite for useful local research. The
shared knowledge service is not automatically a network-wide job scheduler;
local coordinators manage execution. Shared campaign scheduling would be a
separate architectural responsibility.

## Two distinct knowledge boundaries

1. **Workspace versus reusable catalog.** Group claims, tasks, messages and attempts
   are bounded problem-local working state. The catalog makes selected declarations
   and exact proof artifacts retrievable across problems. Initially these share
   coordinator SQLite but have distinct records and lifecycles. Do not load mathlib
   into each group graph or present speculative workspace material as verified.
2. **Local versus shared catalog.** Adding a result locally does not publish it to
   the network. Use distinct API/UI operations such as “Add to local catalog” and
   “Publish to shared service.” Sharing requires an explicit operation or explicitly
   configured policy; existence or successful verification never implies upload.

## Local-first delivery

First deliver the independently useful SQLite catalog: bootstrap from a pinned
mathlib environment, retain sources/provenance/licenses, extract typed dependencies,
provide bounded retrieval and demonstrate Lean-checked reuse in frozen context.
Private collections, failures, speculative work and unpublished results stay local.

Then build the Postgres-backed shared API and explicit bridge operations:

- Query shared knowledge under an explicitly configured remote policy.
- Cache selected provenance-bearing snapshots locally.
- Submit immutable publication bundles for server validation and required checking.
- Record issuing-service-qualified remote identities and durable receipts.
- Refresh selected cached snapshots when a real freshness need arises.

No arbitrary bidirectional replication is planned. Local IDs require no remote ID.
Local research and cached data remain usable without the hosted service; offline
proof checking additionally needs matching Lean environments and exact artifacts.
A local-only policy prevents outbound research queries as well as uploads.

## Shared concepts and invariants

- Share knowledge formats and narrow retrieval contracts across SQLite/Postgres,
  without identical SQL schemas or interchangeable service responsibilities. Local
  workspace/cache management and shared admission differ.
- Claims and proof inputs are immutable; corrections create new entries. Checkers
  produce verification evidence and repositories persist it. Client labels and
  generic updates cannot change propositions or confer verification authority.
- Preserve exact environments, immutable source revisions, original provenance,
  licenses/notices and historical verification evidence. Origin, scope, visibility
  and verification are separate. Generated does not imply novel; remote does not
  imply verified.
- Cached mathlib entries retain mathlib as their source. Shared-service retrieval
  metadata adds provenance rather than replacing original source identity.
- Match exact-context duplicates first and retain publications/alternative proofs.
  Equivalence/contradiction suggestions are advisory without formal evidence.
  Remote matching never requires destructive local merging.
- Freeze catalog snapshots and artifacts into context/replay inputs; refresh cannot
  rewrite queued tasks or historical proofs.
- Shared admission establishes server-side assurance. Public untrusted publication
  requires authentication and the separate trusted checker; current local receipts
  are not that attestation.

Start with indexes and ordinary search. pgvector, advanced graph queries and more
source importers follow concrete requirements, not hypothetical scale.

## Planning records

Detailed plans currently live outside the repository:

- `/home/ben/.opencode/plan/solvenet-knowledge-graph-collaboration.md`: local catalog
  K1–K6 and shared-service S1–S6 tickets and acceptance criteria.
- `/home/ben/.opencode/plan/solvenet-next-steps.md`: roadmap and delivery sequence.

Those paths may not exist in another checkout. This repository document preserves
the agreed architecture for fresh agents; external plans hold implementation detail
and remaining questions. K/S features remain planned until delivery is recorded.
