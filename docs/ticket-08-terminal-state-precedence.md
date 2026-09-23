# Ticket 8: Run terminal-state precedence

## Decision

SolveNet uses **verified-proof precedence**, rather than making every terminal
state immutable. Lean verification is the source of truth, and work assigned
before a terminal transition is intentionally retained and verified. Therefore a
late verified proof upgrades `error` or `exhausted` to `solved`; no later outcome
can downgrade `solved`.

This is the smallest policy consistent with the existing protocol. It does not
add a general state-machine layer. The guarded SQL updates at the point where a
verification is stored now express both sides of the rule directly. The previous
`_refresh` query that inferred `running -> error` from verification history was
removed so precedence no longer depends on asymmetric refresh side effects.

## Transition table

The rows are the current run state and the columns are a newly stored verification
outcome. “Normal processing” means rejection may create a permitted repair and
the refresh step may mark a run exhausted when no work remains.

| Current run state | `verified` | `verifier_error` | `rejected` / `timeout` |
| --- | --- | --- | --- |
| `running` | `solved` | `error` | normal processing |
| `solved` | `solved` | `solved` | `solved` |
| `error` | `solved` | `error` | `error` |
| `exhausted` | `solved` | `exhausted` | `exhausted` |

Queued jobs are cancelled when a run becomes `solved` or `error`. Assignments
that were already active remain valid: their results are accepted under the
normal lease rules, candidates are verified, and their jobs become `done`.
Rejections after a terminal transition never create repair jobs. Duplicate
verification delivery for one attempt remains idempotent; the first stored
outcome for that attempt is retained.

## Files changed

- `coordinator/src/solvenet/store.py`: explicit guarded success and verifier-error
  transitions; removed inferred error transition from refresh.
- `coordinator/tests/test_coordinator.py`: both terminal event orders, retained
  active assignments, duplicate deliveries, and concurrent-like cross-attempt
  delivery coverage.
- `protocol/v1.md`: protocol-level precedence and late-work promise.
- `README.md`: user-facing summary of late verification behavior.
- `docs/ticket-08-terminal-state-precedence.md`: decision record and transition
  table.

## Tests and results

`PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -v`

- 64 tests passed.
- 14 tests skipped because optional Lake or opt-in Docker prerequisites were not
  available/enabled.
- New coverage passed for both terminal event orders, completion of assignments
  already active at a terminal transition, repeated delivery, and concurrent-like
  delivery of outcomes from separate attempts.

## Remaining concerns

- Verification is currently serial in the coordinator, although store calls can
  arrive concurrently and SQLite transactions serialize them. Deterministic
  selection of pending attempts is Ticket 9 and is intentionally out of scope.
- Conflicting outcomes submitted for the *same* attempt retain the first stored
  verification. Such disagreement indicates a verifier consistency problem;
  normal retries must submit the same outcome.
