# Ticket 9: Deterministic verification ordering

## Policy

The coordinator selects pending candidates across all runs in persisted attempt
insertion order (`attempts.rowid` ascending). Attempt ID ascending is the explicit
final tie-breaker. A completed worker result inserts its attempt, so completion
order—not run or job creation order—determines when candidates enter the
verification queue. Both values are persisted in SQLite, so coordinator restarts
preserve the effective order.

Verification remains single-threaded: `Coordinator.loop` selects and verifies one
candidate at a time. This ticket does not add verifier concurrency, reservations,
or multi-verifier scheduling, and does not change worker job-claim policy.

## Query and index reasoning

`Store.pending()` now explicitly uses `ORDER BY t.rowid, t.id LIMIT 1`. SQLite's
`EXPLAIN QUERY PLAN` reports an `attempts` table scan in rowid order followed by
indexed primary-key lookups for the joined assignment, job, run, problem, and
verification rows. It does not report a temporary sort. Since rowid order already
matches the queue order and pending status is represented by absence from the
typically small `verifications` table, an additional index would neither encode
the anti-join nor avoid the ordered attempts scan. No supporting index was added.

## Files changed

- `coordinator/src/solvenet/store.py`: order pending verification selection by
  attempt insertion rowid and attempt ID.
- `coordinator/tests/test_coordinator.py`: cover interleaved attempts from multiple
  runs, repeated selections, completion-order semantics, and restart stability.
- `README.md`: describe restart-stable ordering and serial verification.
- `protocol/v1.md`: record the queue policy and distinguish it from job claims.
- `docs/ticket-09-verification-ordering.md`: decision and validation record.

## Tests and results

`PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -v`

- 76 tests passed.
- 14 tests skipped because optional Lake or opt-in Docker prerequisites were not
  available/enabled.
- The new test passed with four candidates completed in a deliberately interleaved
  order across two runs. It reopened the store before every selection and observed
  the same insertion order until the queue was empty.

## Remaining concerns

- SQLite rowids are stable across ordinary closes and restarts, which is the
  required recovery behavior. A future migration that rebuilds `attempts` must
  preserve rowids if it promises to preserve an existing queue's order.
- The ordered scan is intentionally minimal for the prototype. If pending queues
  become large, query measurements may justify persisting an explicit sequence or
  pending state; that should be driven by observed load rather than added now.
- Single-threaded verification can cause head-of-line blocking. Parallel verifier
  scheduling requires reservation and outcome-order semantics and remains out of
  scope.
