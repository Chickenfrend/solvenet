# Fixed matched repair evaluation

On 2026-10-10, after independent implementation review and explicit authorization,
exactly one batch ran in this order: consecutive-product group, consecutive-product
single agent, Nat-reorder group, Nat-reorder single agent. All four autonomously
verified with `gpt-6.1-sol`, `responses-reasoning`, low effort, `Init`, and pinned
Lean 4.19.0. No reference proofs, preliminary probes or extra observations were used.

## Comparison

| Problem / mode | Accepted | Calls / jobs | Target checks | Auxiliary checks | Target repairs* | Input / output tokens | Estimated USD | Lean ms | Provider seconds** |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Consecutive-product / group | Yes | 4 / 4 | 2 | 1 | 1 | 5,526 / 2,550 | $0.039315 | 5,210 | 45.239 |
| Consecutive-product / single | Yes | 1 / 1 | 1 | 0 | 0 | 138 / 776 | $0.008105 | 1,561 | 18.365 |
| Nat-reorder / group | Yes | 2 / 2 | 1 | 0 | 0 | 2,083 / 223 | $0.0074375 | 1,495 | 6.803 |
| Nat-reorder / single | Yes | 1 / 1 | 1 | 0 | 0 | 136 / 250 | $0.00284 | 1,338 | 6.060 |

All eight assignments completed, without retries or provider failures. All provider
token counters and Lean elapsed entries were known. There were no auxiliary
repairs. All runs stopped at `verified_target`. Total usage was 7,883 input and
3,799 output tokens; six Lean operations used 9,604 ms.

*The original ledger incorrectly reports zero group target repairs. Its exact
strategy lookup missed the stored `target-correction|context:…` strategy. Final
stage-4 review fixed the runner to count dispatched correction strategies by their
base name, including both target and auxiliary corrections. A regression covers
distinct contexts, initial work, undispatched decisions and similarly named
non-corrections. The table agrees with the corrected offline summary: one target
correction. Original ledger/JSON/SQLite evidence is preserved unchanged.

**Sum of worker-reported provider generation durations, not end-to-end wall time.
The records do not provide a comparable measured wall duration for all four runs.

## Actual handoff, correction and checked use

The consecutive-product planner proposed an even-product lemma and a successor
product increment identity. Only the increment identity was separately investigated:

```lean
(n : Nat) : (n + 1) * (n + 2) * (n + 3) =
  n * (n + 1) * (n + 2) + 3 * ((n + 1) * (n + 2))
```

- Verified auxiliary artifact: `53d0daa9cbc14a7eba9b7c4f36f05ba3`, supplied as
  `SolveNetLemma_2932797e3078494180f718c3`.
- Initial target attempt: `142a3b5074614a00a2ebfa9b3e268390`. Lean rejected it for
  an unnecessary tactic after goal closure and an unsolved arithmetic goal;
  `sorryAx` was also rejected. Its dependency-use receipt remains `usage_unknown`.
- Correction job: `c1fde12f74934380aea566152c867492`; task
  `b637fcfaf86641578aa6d572c92db39e`, parent
  `af99a4a339f94bae80c17ad986bcc2ce`. The same owner received the exact failed
  candidate and persisted diagnostics. The original and correction packet manifests
  are byte-identical. The model removed the redundant tactic and adjusted rewriting.
- Accepted target attempt: `ffc0a642ef0544a6a4cc8beee65fdf29`. Its committed
  composed-check receipt has `status: known`, with the supplied lemma in both
  `direct` and `transitive`; `type` is empty. This is actual checked lemma use.

The single-agent consecutive-product proof used its own local even-product
induction and `omega`, verifying on its first attempt. No baseline feedback
correction was exercised. Reorder's group proof verified with a known empty
supplied-dependency receipt; it exercised planner-to-synthesizer handoff only.

## Budget, execution and evidence

Each run had six jobs / twelve assignment reservations, 24,576 input capacity,
2,048 output capacity including reasoning, 45-second provider deadlines, six
10-second Lean operations, and a 660-second wall cutoff. The group retained its
completion reservation, direct auxiliary correction and one target correction.
The baseline allowed up to five target corrections. Equal ceilings do not imply
equal prompts or allocation: group planning and auxiliaries consume capacity.

Offline compilation created `/tmp/opencode/solvenet-matched-worker`. Default
preflight passed with prior task spend zero and **$0.98304 per run / $3.93216
total** reserved. The same command then ran once with `--execute-paid`, using
`~/.config/solvenet/openai_api_key` only in the Go worker. The full reservation
remains held, including failed/unknown-call allowances, leaving **$1.06784**
unreserved under the fresh $5 ceiling. Known usage estimates total **$0.0576975**
at $2.50/M input and $10/M output; actual provider invoices remain unknown.

Local operator evidence root:
`/tmp/opencode/solvenet-matched-repair-evaluation/`.

- `ledger.json`: immutable target inputs, all-four reservation and run summaries.
- `offline-corrected-summaries-stage4.json`: separately named derived report from
  the corrected summarizer, with original-file hashes, source/ledger hashes,
  packet hashes and forensic assertions. Only consecutive-product group
  `target_repairs` changes, from zero to one; costs and other summaries agree
  exactly with the original ledger. Recalculation used read-only SQLite access
  and made zero additional paid calls.
- `graph-nat-consecutive-product-{group,single}.json` and
  `graph-nat-reorder-{group,single}.json`: snapshots/results and terminal reasons.
- Each observation has an adjacent `.json.data/state.db` and `worker.log`, retaining
  packets, jobs, assignments, responses, exact proofs, failures, Lean verdicts,
  immutable feedback/lineage and composed-use receipts where applicable.

Read-only inspection confirmed all four SQLite integrity checks, all four record
SHA-256 values against the ledger, and all six group packet hashes. This evidence
remains local rather than versioned; the documentation records its location.

Final offline checks passed: 39 matched-runner/OpenAI-trial/repair unittest tests
in 16.0 seconds, repository Ruff check and format check (120 files), configured
mypy (five source files), and `git diff --check`. These checks made no paid calls.
Read-only assertions also confirmed exact candidate/status/diagnostic feedback,
same-owner parent lineage, and accepted dependency-use/selected-proof IDs. All
23 original evidence files retained their pre-recalculation SHA-256 hashes.

## Interpretation

This batch demonstrates successful autonomous target correction and checked lemma
composition on the consecutive-product problem under the existing local trust
boundary. Both baselines succeeded immediately, and group execution used more
calls, estimated provider cost and Lean time on both problems. It therefore does
not demonstrate a collaboration advantage. One observation per mode/problem,
with an easier reorder control, cannot establish general effectiveness or success
rates, isolate the causal benefit of repair, or assess auxiliary correction.
