# Ticket 10: claim scheduling policy

## Policy decision

Claims use coordinator-owned global FIFO across compatible queued jobs. A job is
compatible when its model occurs anywhere in the worker's advertised model list
and its run is still running. The oldest persisted job insertion wins; job ID is
the explicit final tie-breaker. Model-list order is capability advertisement,
not worker preference.

This policy keeps fairness reproducible at the coordinator, avoids allowing a
worker's incidental configuration order to starve older work for another model,
and remains stable across coordinator restarts. The current protocol has no
explicit model-preference field, so interpreting list order as preference would
add policy that the wire contract does not otherwise express.

## SQL reasoning

`Store.claim` now executes one compatible-job query:

```sql
SELECT j.*, p.statement, p.imports
FROM jobs j
JOIN runs r ON r.id = j.run_id
JOIN problems p ON p.id = r.problem_id
WHERE j.status = 'queued'
  AND r.status = 'running'
  AND j.model IN (?, ...)
ORDER BY j.rowid, j.id
LIMIT 1
```

`jobs.rowid` is the persisted insertion sequence already used as job age. `j.id`
makes the intended order explicit even if the primary age key is no longer
unique after a future schema change. Model values remain bound parameters; only
the number of placeholders is generated.

The query remains inside the existing `BEGIN IMMEDIATE` transaction. Expiry
processing, selection, assignment insertion, and changing the job to `assigned`
therefore remain atomic. The existing `jobs_status` index supports filtering;
this prototype does not need a new schema migration or a model-specific index.

## Files changed

- `coordinator/src/solvenet/store.py` — replace per-model query iteration with
  one globally ordered compatibility query.
- `coordinator/tests/test_coordinator.py` — cover multiple models, both model-list
  permutations, competing runs, and coordinator restart.
- `protocol/v1.md` — define claim order and atomicity.
- `README.md` — summarize the operational scheduling rule.

## Tests and results

Run after implementation:

```sh
PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -v
```

Result: 77 tests passed and 14 Lean/Docker environment-dependent tests were
skipped. The existing concurrent-claim test also passed, confirming that the
query change preserved atomic claim behavior.

## Remaining concerns

- FIFO is based on SQLite insertion order, not wall-clock timestamps. This is
  intentional and avoids clock ambiguity, but an explicit sequence column would
  be clearer if jobs are later moved between databases.
- FIFO does not reserve capacity per model or run. Such quotas would be a new
  scheduling policy and are outside this ticket.
- A future protocol that needs genuine worker preference should add a distinct,
  explicit preference field rather than overloading capability-list order.
