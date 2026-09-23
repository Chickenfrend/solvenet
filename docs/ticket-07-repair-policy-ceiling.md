# Ticket 7: Repair policy ceiling

## Problem

Schema 3 encoded the experimental two-repair maximum in `CHECK` constraints on
both `runs.max_repairs` and `jobs.repair_depth`. Raising that operational limit
therefore required rebuilding tables even though values above two are
structurally valid.

## Design

`MAX_REPAIRS = 2` is now the named application-level upper bound. It is an API
validation policy, not deployment configuration: all coordinators serving the v1
API enforce the same documented range, and there is no new CLI setting. The
existing default remains zero and the current externally visible behavior is
unchanged.

The database now enforces only structural integrity:

- `runs.max_repairs >= 0`
- `jobs.repair_depth >= 0`

Consequently, changing `MAX_REPAIRS` and the protocol documentation in a future
release will not require another schema migration. The store validates direct
submissions as well as HTTP submissions, while an existing database containing a
larger nonnegative value remains readable and executable.

## Migration details

Schema 6 atomically rebuilds `runs` and `jobs`, because SQLite cannot remove a
`CHECK` constraint in place. During the transaction foreign-key enforcement is
temporarily disabled so referenced tables can be replaced. The migration:

1. creates replacement tables with the same columns, defaults, foreign keys, and
   all unrelated checks;
2. copies every row and its `rowid`, without rewriting run policy or repair
   values;
3. replaces the old tables;
4. recreates `jobs_status` and the partial unique `jobs_parent_attempt` index;
5. runs `PRAGMA foreign_key_check` before committing; and
6. restores foreign-key enforcement.

Preserving rowids retains the existing queue-order behavior. The migration is a
single transaction, so a copy or integrity failure rolls back the whole rebuild.
Assignments, attempts, verifications, repair parent links, and uniqueness are not
changed.

## Files changed

- `coordinator/src/solvenet/store.py`: named API maximum, schema 6 rebuild, and
  nonnegative checks.
- `coordinator/tests/test_coordinator.py`: schema-version and API-bound coverage.
- `coordinator/tests/test_repairs.py`: migration preservation, structural-bound,
  foreign-key, index, linkage, and uniqueness tests.
- `protocol/v1.md` and `README.md`: schema 6 and API-versus-schema policy docs.
- `docs/ticket-07-repair-policy-ceiling.md`: design and migration record.

## Tests and results

Run from the repository root:

```sh
PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -v
python3 -m compileall -q coordinator/src coordinator/tests
git diff --check
```

Result: 61 tests passed and 14 environment-dependent Lean/Docker tests were
skipped. The compile check and `git diff --check` also completed successfully.
A copy of the repository's populated schema-3 database was also migrated to
schema 6: all six table row counts were unchanged, `foreign_key_check` returned
no rows, and `integrity_check` returned `ok`.

## Remaining concerns

The v1 API still intentionally accepts at most two repairs. Raising that policy
requires a code and protocol change, but no schema change. SQLite migrations
still require brief exclusive write access; schema 6 is therefore best applied
while one coordinator owns the database, as required by the protocol.
