# Ticket 19 — Heterogeneous initial model strategies

## Request schema and compatibility

`POST /v1/runs` accepts an additive `initial_jobs` array of ordered groups:

```json
{"statement": "(n : Nat) : n + 0 = n", "initial_jobs": [
  {"model": "ollama/model-a", "count": 2, "max_output_tokens": 256},
  {"model": "ollama/model-b", "count": 1}
], "max_repairs": 1}
```

Each group requires a nonempty model (up to 256 UTF-8 bytes) and integer count
from 1 to 100. Its optional output budget defaults to 2048 and must be 1–32768.
There must be 1–100 groups and their counts must sum to at most 100. Unexpected
group keys are rejected. The request remains subject to the 256 KiB body limit.
`initial_jobs` conflicts with any explicitly supplied `attempts`, `model`, or
top-level `max_output_tokens`, even if their values equal defaults. Shared
`imports`, `max_repairs`, `max_assignments`, and `generation_timeout_seconds`
still apply. The existing single-model form and all of its defaults work as before.

## Persistence and repairs

Initial jobs are inserted in group order, with the selected model and output
budget stored on each job. `GET /v1/runs/{id}` returns `initial_jobs` derived
from initial (`repair_depth=0`) jobs in persisted insertion order, with adjacent
identical model/budget groups coalesced. It returns effective budgets, including
the default for omitted budgets and historical single-model jobs. The existing
`jobs[]` supplies each individual job's policy; no run strategy copy or SQLite
migration is needed. Existing schema-1 through schema-6 databases continue to
open, and restarts retain initial strategy and claim order.

A repair inherits its immediate parent job's model, max output tokens, timeout,
and assignment limit. It never switches groups. Claim selection remains global
FIFO among queued jobs matching any advertised worker model; the order of a
worker's advertised model list is irrelevant.

## Bounds

For `A = sum(initial_jobs[].count)` (or legacy `attempts`), `R = max_repairs`,
and `M = max_assignments`, the run has at most `A * (1 + R)` jobs and
`A * (1 + R) * M` budgeted assignments. Defaults give `A ≤ 100`, `R ≤ 2`,
`M ≤ 100`, so at most 300 jobs and 30,000 budgeted assignments. Rejected
pre-execution assignments do not spend the budget and can repeat without a
finite raw claim bound. A mixed run's initial token-budget sum is
`sum(count * max_output_tokens)`; repaired chains use their own parent's budget.

## Files changed

- `coordinator/src/solvenet/server.py`: legacy/group conflict check and submission routing.
- `coordinator/src/solvenet/store.py`: group validation, job insertion, derived inspection strategy.
- `coordinator/tests/test_coordinator.py`, `coordinator/tests/test_repairs.py`: API boundaries, heterogeneous claims, inheritance, restart and legacy migration coverage.
- `README.md`, `protocol/v1.md`, `docs/ticket-18-terminology.md`: examples, bounds, glossary, and repair policy.
- This design and result record.

## Tests and results

- `PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -q`: 90 passed (15 skipped for unavailable optional Lean/Docker dependencies).
- `(cd worker && go test -race ./...)`: all three packages passed.
- `git diff --check`: passed.

## Remaining concerns

Identical adjacent submission groups cannot be distinguished in inspection;
they have the same effective policy. Requested model identifiers describe worker
compatibility, not pinned model weights or independently attested execution.
