# Ticket 5: Assignment retry limits

## Problem

Every generation job previously allowed exactly three assignments. The value was
embedded in initial-job SQL, so experiments could not select a different retry
policy even though jobs already stored the effective limit.

## Design

Run submission now accepts `max_assignments`, an integer from 1 through 100 with a
default of 3. The coordinator stores the selected value on both the run and every
initial job. Repair jobs continue to copy the parent job's stored value. Retry
decisions still use the job value, so a restart does not depend on process
configuration or a changed default.

Run inspection exposes the run-level policy as `max_assignments`; each job also
shows its effective value. Persisting the run value makes the maximum dispatch
count directly calculable as:

```
attempts * (1 + max_repairs) * max_assignments
```

This ticket does not classify failures. Expired assignments and all reported
execution failures continue to consume the same allowance.

## Migration and API impact

Schema 5 adds `runs.max_assignments` with a checked range of 1–100 and a default
of 3. Existing runs therefore retain the previous policy. Existing jobs are not
rewritten because they already persist their effective `max_assignments` value.

`POST /v1/runs` accepts the additive optional field. Invalid types, including
booleans, and values outside 1–100 return HTTP 400. Omitting the field preserves
the prior three-assignment behavior. `GET /v1/runs/{id}` includes the new run
field.

## Files changed

- `coordinator/src/solvenet/store.py`: schema migration, constants, validation,
  and initial-job persistence.
- `coordinator/src/solvenet/server.py`: HTTP validation and submission wiring.
- `coordinator/tests/test_coordinator.py`: migration, API validation, retry bound,
  inspection, and restart coverage.
- `coordinator/tests/test_repairs.py`: repair-job inheritance coverage.
- `protocol/v1.md` and `README.md`: API, migration, and dispatch-bound docs.

## Tests and results

Run from the repository root:

```sh
PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -v
```

Result: 54 tests passed and 14 environment-dependent Lean/Docker tests were
skipped. `python3 -m compileall -q coordinator/src coordinator/tests` and
`git diff --check` also completed successfully.

## Remaining concerns

The 1–100 validation ceiling is an application policy and is also enforced by the
new run column constraint. Changing that ceiling would require a migration.
Failure classification remains intentionally deferred to Ticket 6.
