# Ticket 2: Generation timeout policy

## Problem

The coordinator always dispatched a 120-second timeout, while the worker
independently replaced nonpositive and above-120 values with 120. The effective
policy could therefore differ silently from coordinator intent and was not
persisted for restarts or repair jobs.

## Design

- Run submission accepts `generation_timeout_seconds`, an integer from 1 through
  86400. The compatible default remains 120 seconds.
- The setting is stored on the run and copied to every initial and repair job.
- Claims expose the persisted value as `job.timeout_seconds`.
- The worker applies valid values directly. Out-of-range assignment values return
  a field-specific error before provider execution instead of being substituted.
- Run inspection exposes both the selected run policy and each job's effective
  persisted timeout.

The upper bound is a protocol validation bound, not a separate worker-side policy.
The coordinator and worker use the same bound.

## Migration and API impact

Schema migration 4 adds `generation_timeout_seconds` to `runs` and `jobs` with a
default of 120 and a `CHECK` constraint for the documented range. Existing data
therefore keeps its previous effective behavior. The API change is additive;
submissions that omit the field continue to use 120 seconds.

## Files changed

- `coordinator/src/solvenet/store.py`: schema migration, validation, persistence,
  repair inheritance, claim response, and run inspection data.
- `coordinator/src/solvenet/server.py`: run-submission validation.
- `worker/internal/daemon/daemon.go`: direct timeout use and explicit malformed
  assignment errors.
- `coordinator/tests/test_coordinator.py`: migration, API validation, inspection,
  and restart persistence coverage.
- `coordinator/tests/test_repairs.py`: repair inheritance coverage.
- `worker/internal/daemon/daemon_test.go`: above-120 deadline and invalid timeout
  coverage.
- `protocol/v1.md` and `README.md`: submission, migration, and effective deadline
  behavior.

## Tests and results

From the repository root:

```text
PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -v
# 50 tests run; passed with 14 skips (optional Lake/Docker integrations)

(cd worker && go test -race ./...)
# all packages passed

git diff --check
# passed
```

## Remaining concerns

Timeout validation is duplicated manually across Python, Go, and protocol
documentation. A later protocol-limits cleanup may centralize naming, but code
generation is not warranted for this prototype.
