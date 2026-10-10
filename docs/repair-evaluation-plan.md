# Repair lifecycle and matched evaluation

Operator authorization: at most $5 total projected provider spend for this task,
across all observations, failures and retries. Use Sol 6.1 Responses reasoning;
credentials remain worker-local. Existing earlier observations are historical,
not charged against this new authorization. Keep a cumulative budget ledger.

Execute stages sequentially with fresh build implementation and independent
build review, relevant offline checks, and a commit per stage.

1. **Target repair:** reuse immutable context and Lean feedback for one bounded
   target correction; preserve distinct attempts/provenance and prove checked
   dependency use offline. Preserve existing defaults/interfaces where practical.
2. **Workflow reservations:** make the local trial reserve enough model jobs and
   Lean operations for auxiliary work, synthesis and target repair. Keep dollar,
   assignment, token, elapsed and Lean limits distinct; reserve a completion tail
   rather than spending it on further exploration. No generalized scheduler.
3. **Mechanical correction routing:** route initial Lean rejection directly back
   to the owner with exact proof/diagnostics. Reserve separate critique for repeated
   failure or strategy-level disagreement. Avoid fragile diagnostic parsing and
   retain explicit bounded eligibility/idempotence.
4. **Matched evaluation:** repeat the consecutive-product theorem and a small
   fixed problem set under group and single-agent Lean-feedback loops with
   comparable budgets and capacities. Record autonomous target acceptance,
   actual checked dependency use, calls/tokens/provider and Lean cost, failures
   and repair counts. Preflight every paid observation against the cumulative
   $5 ceiling. No reference proofs in prompts and no extra runs merely to obtain
   a positive result. Report outcomes and limitations honestly.

Use the existing local verification trust boundary; neither local candidate
receipts nor scripted tests establish safety for public untrusted contributors.

## Stage 1 implementation status (2026-10-09)

Implemented offline; independent review approved after the binding guard fix below.
Stage commit is pending.

- Graph loops accept `graph_limits={"target_corrections": 1}` for at most one
  target correction per group. The default is `0`, preserving existing graph
  retry behavior. With correction enabled, equivalent synthesis retries are
  replaced by the explicit correction; genuinely new checked context can still
  support a fresh target strategy. Auxiliary repair behavior is unchanged.
- A rejected target creates a distinct `model.generate` job and task, linked to
  the failed target task. The frozen packet request records the failed attempt
  ID; feedback records its job ID, exact candidate, status and persisted Lean
  diagnostics. Workers submit a new candidate; coordinator code never edits it.
- The failed job's complete proof manifest and advertised checked lemmas are
  copied verbatim, including dependency closure and provenance. Subsequent graph
  opinions do not reselect context. A changed verifier binding fails admission.
  Correction verification also refuses rebinding after dispatch (including across
  restart), stopping with `verifier_binding_changed` before spending a Lean slot
  or recording a synthetic rejection.
  Existing immutable packet/proof-context storage and composed verification are
  reused. Required feedback or context that cannot fit fails admission without
  truncating feedback or dropping a supplied lemma.
- Existing work, task/depth, model availability/context, deadline, packet/source,
  declaration, verification-operation and Lean-time admission gates apply. This
  stage does not reserve a completion tail; that remains stage 2.
- Real Lean offline coverage rejects a target with an extra `rfl`, then verifies
  its separately submitted correction using the named supplied lemma. Its actual
  direct dependency-use receipt has status `known`. Coverage also checks exact
  feedback, frozen context after a challenged graph opinion, durable lineage,
  duplicate result submission, restart while repair is leased, single-correction
  exhaustion after restart, exhausted Lean capacity, oversized exact feedback,
  and the opt-in/single-correction configuration bound. Independent review added
  real-Lean coverage of transitive dependency closure and use, immutable selection,
  changed binding revisions before and after dispatch, and permanent correction
  worker failure consuming the correction allowance without a fabricated attempt.

Independent-review validation: repository Ruff check and formatting pass;
configured mypy passes (5 source files). Final combined unittest run: 79 passed
in 343 seconds (47 frontier, 20 context packet, 12 existing repair-chain tests),
with `$HOME/.elan/bin` on `PATH`. The frontier suite logs one expected injected
verifier failure. An initial pytest invocation could not run because pytest is
not installed; the repository's unittest runner was used instead.

### Cumulative new paid budget ledger

| Stage | Provider calls | New paid spend | Remaining authorization |
| --- | ---: | ---: | ---: |
| 1 — offline target repair | 0 | $0.00 | $5.00 |

No credentials were accessed and no paid observations were performed.
