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
Stage commit: `497293b`.

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
| 2 — offline workflow reservations | 0 | $0.00 | $5.00 |
| 3 — offline mechanical correction routing | 0 | $0.00 | $5.00 |

No credentials were accessed and no paid observations were performed.

## Stage 2 implementation status (2026-10-09)

Implemented offline. Independent review remains a separate stage gate.

- Opt in with `graph_limits={"completion_reserve": 1,
  "target_corrections": 1}`. Existing callers default to no reservation.
  The durable frontier history reserves two completion jobs, their retry leases,
  weighted model work, and two Lean operations. Only synthesis/correction dispatch
  consumes the model tail; only target checks consume the Lean tail. Successful
  initial synthesis stops immediately, even with unused correction capacity.
- Weighted work remains distinct from assignments: each graph job reserves
  `2 * model_cost` work and at most two assignments. For heterogeneous synthesis
  models, the tail uses the largest configured cost, preserving capacity through
  routing changes. Task slots are held too. Earlier persisted loop configurations
  are normalized with the new default limits on idempotent startup.
- `completion_check_ms` defaults to 10,000 ms per reserved operation; configure it
  to the verifier's full operation deadline. Auxiliary admission leaves those
  deadlines and operation slots available. Actual check admission atomically checks
  the full new deadline plus the remaining tail against the Lean elapsed ceiling.
  Unknown/abandoned usage keeps its existing deadline charge and spent operation.
  Multiple artifacts from one response cannot consume the tail: a refused artifact
  is marked incompatible with a capacity diagnostic, without a synthetic Lean
  verdict, allowing the target workflow to advance.
- Trace reports remaining completion jobs/work/checks/time. Decisions distinguish
  `completion_work_reserve`, `completion_job_reserve`,
  `completion_verification_reserve`, and `completion_lean_time_reserve` from
  ordinary capacity/context/availability and deadline failures.
- The OpenAI trial enables the reservation and one target correction. Its default
  24 work units at model cost 2 cover six jobs / twelve assignments: planning,
  finding, critique, auxiliary repair, synthesis and target correction. This is
  capacity, not a guarantee of model proposals or a fixed action sequence.
  Four planning/critique assignments, six Lean operations, and 60 seconds of Lean
  deadlines are separate bounds. The wall limit is 660 seconds (group dispatch
  stops 15 seconds earlier); provider calls retain their 45-second timeout.
- CLI controls `--max-work`, `--model-cost`, `--context-capacity`,
  `--output-capacity`, `--per-run-budget-usd`, and `--wall-seconds` are bounded.
  Preflight requires at least six jobs, caps per-run projection at $1.50 and total
  authorization at $5, and includes all prior reserved spend and retry assignments.
  Each call conservatively charges the full 24,576 input and 2,048 output capacity
  (including reasoning). At $2.50/M input and $10/M output, twelve calls project
  **$0.98304**, with **$0** paid so far for this new task. Raising work or capacities
  must still pass both dollar ceilings. Preflight remains the default and does not
  read credentials or start a worker.

Validation: 100 unittest tests passed in 391 seconds across frontier, group routing,
group loop and offline OpenAI trial suites, with real Lean and `$HOME/.elan/bin` on
`PATH`. Expected injected verifier failures were logged. Repository Ruff check
and formatting pass; configured mypy passes (5 source files). Coverage includes
exhausted exploration with successful target correction, separate Lean operation
and elapsed reservations, unused correction capacity after success, heterogeneous
model costs, twelve-call conservative projection, prompt/output capacity and
independent per-run/cumulative dollar gates. No paid observations were run.

## Stage 3 implementation status (2026-10-09)

Implemented offline following reviewed stage 2 (`43c4b8e`); independent review
and the parent session's commit remain the stage gate.

- `graph_limits={"direct_auxiliary_correction": 1, "retries": 1}` routes an
  initial auxiliary Lean rejection to the same logical owner and model for one
  correction. The option defaults to `0`, retaining the existing critique-first
  auxiliary workflow. `retries=0` disables correction. Stage 1 target correction
  remains independently configured and direct.
- Routing uses persisted verification status, never diagnostic-string matching.
  Exact candidate/Lean diagnostics and artifact/job/task/claim/verifier attribution
  accompany a verbatim copy of the original frozen supplied context. Feedback or
  supplied context that cannot fit is refused rather than truncated or reselected.
  A changed verifier binding is refused before dispatch or Lean execution.
- The corrected response is a distinct model job/task and artifact. Its parent
  task and owner are retained. Durable per-claim correction history prevents
  additional artifacts, graph/context changes, lease retries and restart from
  resetting the single correction allowance.
- A rejected correction escalates to mathematical critique if the existing
  planning/work/task/deadline limits and completion tail permit. Challenged
  strategies and explicit critique requests retain their critique route. Critique
  after failure cannot authorize another correction in direct mode. Timeout and
  verifier error use critique rather than the initial Lean-rejection route.
- The OpenAI trial enables direct auxiliary correction with the existing six-job
  ceiling and completion reservation. A successful repair path can use five jobs
  (planning, auxiliary, correction, synthesis, target correction), leaving critique
  capacity for repeated failure. The offline successful auxiliary/target fixture
  uses four jobs instead of the critique-first fixture's five: eight versus ten
  reserved assignments, with the same three Lean operations. These reservations
  and scripted outcomes do not establish real-model effectiveness or paid savings.
- Real Lean coverage rejects a nested `calc` tactic with incorrect indentation,
  accepts its owner-submitted indentation correction, and verifies the final
  target using the corrected lemma's exact generated name with a known direct-use
  receipt. Coverage also exercises frozen feedback/context, duplicate submission,
  restart, multiple rejected correction artifacts, critique escalation without
  renewed correction, opt-in validation, disabled retries, explicit strategy help,
  changed verifier bindings and completion-tail protection. Existing
  critique-first and target-correction regression coverage remains active.

Validation: 127 distinct unittest tests passed across frontier (58), context
packet (20), group routing (7), group loop (24) and offline OpenAI trial (18).
The final frontier suite passed in 424 seconds with real Lean; expected injected
verifier failures were logged. The initial combined invocation passed its
coordinator tests but failed to import the trial suite because `homelab/src` was
missing from `PYTHONPATH`; the trial suite then passed with that path supplied.
Repository Ruff check and formatting pass; configured mypy passes (5 source
files); `git diff --check` passes. No credentials were accessed and no paid
observations were run. Changes remain uncommitted for parent-session review.
