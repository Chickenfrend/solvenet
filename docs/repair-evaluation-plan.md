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
| 4 — offline matched evaluation runner | 0 | $0.00 | $5.00 |
| 4 — authorized fixed paid batch (2026-10-10) | 8 | Invoice unknown; usage estimate $0.0576975; reservation $3.93216 | $1.06784 unreserved |

Stages 1–3 and the stage-4 implementation/review were offline. The subsequently
authorized fixed stage-4 batch ran once; its irrevocable reservation remains held.
The usage estimate does not release that reservation or establish invoiced spend.

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

## Stage 4 implementation status (2026-10-10)

Implemented offline in `integration/matched_repair_evaluation.py`; independent
review and the parent session's commit are the gate before paid execution.
No paid calls, credential reads or extra compatibility probes were performed.

### Fixed evaluation design

- Four observations in fixed order: consecutive-product group, consecutive-product
  single agent, Nat-reorder group, Nat-reorder single agent. Consecutive-product is
  the existing `Init`-only divisibility target. Nat-reorder is an easier control,
  not evidence of performance on a broad moderate-difficulty problem set.
- Both modes use `gpt-6.1-sol`, Responses reasoning, low effort, input capacity
  24,576 and output capacity 2,048 (including reasoning), one compiled Go worker,
  the existing coordinator HTTP/SQLite protocol, and real pinned Lean. Only
  statement/imports/environment are copied from fixtures. The reorder fixture's
  offline reference proofs are explicitly excluded from both prompts and records.
- Each observation has six model jobs, at most two assignments per job/twelve
  provider-call reservations, 45-second generation deadlines, six Lean operations
  at 10 seconds each, and a 660-second wall cutoff with terminate/kill cleanup.
  The group retains its 24 weighted-work-unit limit, four planning/critique calls,
  direct auxiliary correction, one target correction and completion reservation.
- The single logical agent submits the target directly, then at most five target
  corrections. Each new job receives the same target and the immediately previous
  exact candidate/Lean status/diagnostics/attempt ID, without diagnostic parsing,
  excerpts or graph collaboration. Oversized exact feedback halts admission.
  These are sequential independent coordinator runs with trial-owned immutable
  packets/parent lineage in `single_inputs`, not six independent logical agents.
  This avoids changing the existing repair-v1 schema's two-repair bound.
- Equal ceilings are not equal workflows: graph jobs include planning, auxiliary
  proofs and critique; baseline jobs are all target attempts. Baseline may use six
  target checks; graph shares its six operations between auxiliary and target work
  and stops dispatch 15 seconds before wall cutoff. Actual jobs, assignments,
  checks, repairs and elapsed time must accompany outcomes. One sample per mode
  and problem cannot support success-rate or efficiency conclusions.

### Cumulative ledger and records

The batch reserves **all four worst cases before starting any worker**:
`12 × (24576 × $2.50/M + 2048 × $10/M) = $0.98304` per observation,
**$3.93216 total**, below both the $1.50 per-run cap and fresh $5 ceiling.
The ledger includes failed observations and unknown usage; reservations never
shrink. No automatic retry/resume or extra observation is supported. The output
directory must be new, preventing the same batch command from accidentally
restarting its paid runs. Do not create a second batch directory to retry failures:
the $5 authorization is cumulative for this task, not per invocation.

`ledger.json` persists target-only immutable batch inputs, reservation state before
each observation, individual summaries and aggregate known usage estimates.
Per-observation JSON and `.json.data/state.db` sidecars retain exact request
packets, responses, failures, candidate/Lean evidence and graph composed-use
receipts. Summaries include autonomous target acceptance, target/auxiliary repairs,
assignment upper bounds (including failures), known/unknown token counts, rate-based
cost estimates, target/auxiliary check counts and Lean time. Actual checked
dependency-use receipts are copied from composed checks; no dependency-use claim
is inferred merely from graph structure. Provider bills remain unknown. Starting
any observation changes actual paid spend from the initial $0 to unknown;
reported usage estimates are not provider invoices. Unknown counters cannot be
treated as zero or release reservations for later observations.

### Exact commands

From the repository root, compile the local worker (offline):

```sh
go -C worker build -o /tmp/opencode/solvenet-stage4-worker ./cmd/solvenet-worker
```

Preflight is the default; it reads target fixtures but does not read the key file,
launch a worker, create output files or contact the provider. Replace the key path
with the operator's existing worker-local credential file path; never include key
contents in arguments or records:

```sh
PATH="$HOME/.elan/bin:$PATH" PYTHONPATH=coordinator/src:homelab/src:integration \
  .venv/bin/python integration/matched_repair_evaluation.py \
  --model gpt-6.1-sol --profile responses-reasoning \
  --worker /tmp/opencode/solvenet-stage4-worker \
  --key-file "$HOME/.config/solvenet/openai.key" \
  --output experiments/stage4-matched-repair-2026-10-10
```

**Only after independent review and the parent session's explicit paid-stage
decision**, run that exact command once with `--execute-paid` appended. The four
observations reserve $3.93216 with no preliminary probe or reruns. A runner
exception preserves the full batch reservation and aborts remaining execution;
unused reservations remain held. Inspect the ledger/sidecars and report the
failure rather than issuing a new command to obtain a positive result.

Offline checks:

```sh
PATH="$HOME/.elan/bin:$PATH" PYTHONPATH=coordinator/src:homelab/src:integration \
  .venv/bin/python -m unittest integration/test_matched_repair_evaluation.py \
  integration/test_live_openai_graph_trial.py coordinator/tests/test_repairs.py
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
git diff --check
```

Validation: 38 tests passed in 17 seconds, including the compiled Go worker with
a scripted loopback Responses provider and real Lean rejecting then accepting a
baseline correction, six real-Lean rejections exhausting the baseline, exact
immutable feedback, twelve failed retry assignments, unknown-usage blocking,
all-four cumulative preflight and irrevocable ledger reservations. No reference
proof was sent to a paid provider.
Repository Ruff check and formatting, configured mypy (five source files), and
`git diff --check` all pass. A direct default preflight with nonexistent worker
and key paths confirms the $3.93216 batch reservation without accessing credentials
or launching a process. Changes are uncommitted for parent-session commit.

Independent stage-4 review approved the fixed batch for the parent session's paid
decision after correcting unknown target Lean time reporting: persisted group
unknown-time markers and zero-duration internal baseline verifier errors now count
as unknown elapsed entries, rather than known zero cost. An injected coordinator
verifier failure regression covers this case. Review also checked the actual Go
`job.timeout_seconds` deadline, immutable baseline packets (update/delete refusal),
absence of baseline group agents/runs, low-effort model/output wire settings,
unknown-usage early pause, six-job/twelve-assignment caps and completion-tail
alignment. Go daemon/provider tests pass in addition to the checks above. No paid
calls, credential reads, subagents or commits were performed during review.

### Authorized stage-4 execution (2026-10-10)

After independent review and explicit parent-session authorization, built a fresh
worker at `/tmp/opencode/solvenet-matched-worker`, ran the default offline preflight,
then executed exactly one four-observation batch in the fixed order above. The
worker alone used `~/.config/solvenet/openai_api_key`. Model/profile/low effort and
all reviewed capacities were retained. No probe, extra observation or rerun ran.

All four targets were autonomously accepted. The consecutive-product group used
four calls, one verified auxiliary, one rejected target and one successful target
correction. Its accepted composed-check receipt records known direct/transitive
use of the supplied lemma. The consecutive-product single agent succeeded with
one call and one check. Reorder used two group calls versus one baseline call;
both verified without repair or auxiliary dependencies. These observations show
bounded target correction and composed lemma use, not a collaboration advantage.

Eight completed calls reported 7,883 input and 3,799 output tokens; conservative
usage estimate **$0.0576975**. There were no failed/retried assignments or unknown
provider usage entries. Six Lean operations used **9,604 ms**, with no unknown
elapsed entries. Actual provider invoices remain unknown. The full **$3.93216**
reservation is retained, leaving **$1.06784** unreserved under the $5 ceiling.

Evidence is `/tmp/opencode/solvenet-matched-repair-evaluation/ledger.json` and the
four observation JSONs with adjacent `.json.data/state.db` and worker logs. The
raw ledger undercounts the consecutive-product group target repair as zero:
`summarize` looks up `target-correction` exactly, while the persisted strategy is
`target-correction|context:…`. Read-only inspection confirms **one** correction,
its parent task, exact rejection feedback and byte-identical frozen manifest.
Final independent review fixed `summarize` to count dispatched target and auxiliary
corrections by strategy base name, with a regression distinguishing initial work,
context variants and undispatched decisions. The separate derived report
`offline-corrected-summaries-stage4.json` recomputes all four summaries offline;
only that target repair count changes, from zero to one. All 23 original evidence
files retain their SHA-256 hashes. No additional paid calls were made. See the
[matched evaluation record](matched-repair-evaluation.md) for comparison and IDs.

Post-execution offline validation: 39 matched-runner/OpenAI-trial/repair unittest
tests passed in 16.0 seconds; repository Ruff check and formatting (120 files),
configured mypy (five source files), and `git diff --check` pass. Read-only checks
confirmed SQLite integrity, record/packet hashes, exact rejection feedback,
same-owner correction lineage, frozen context and actual accepted dependency use.
