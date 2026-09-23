# Ticket 13: immediate assignment rejection

## Decision

The existing authenticated result endpoint now accepts a third terminal worker
outcome, `status: "rejected"`. A separate release endpoint would duplicate lease
authentication, idempotency, and result persistence without adding useful state.

Rejection is only for assignment validation before provider execution. It does
not consume `max_assignments`, create an attempt, or advance repair depth. The
coordinator records the assignment as `rejected` and immediately queues the job.
Ticket 6 behavior remains unchanged after execution begins: transient failures
consume an assignment and retry under the limit; permanent failures consume the
assignment and end the job.

Both malformed v1 assignments and unsupported protocol versions have this state
effect. They remain distinguishable as `malformed_assignment` and
`unsupported_protocol` because incompatibility is operationally different from a
coordinator emitting invalid v1 data.

## Wire semantics

The worker submits to `POST /v1/assignments/{id}/result`:

```json
{
  "lease_token": "opaque-secret",
  "status": "rejected",
  "rejection_kind": "malformed_assignment",
  "error": "job.statement must be a nonempty string"
}
```

`rejection_kind` must be `malformed_assignment` or `unsupported_protocol`, and
`error` is a nonempty string of at most 4000 bytes. Identical retries are
idempotent. Run inspection exposes assignment status, error, and rejection kind.

The worker first retains the bounded claim response, independently extracts the
assignment ID and lease token, then decodes and validates the full assignment.
This permits rejection even when another field has the wrong JSON type. If both
credentials are not valid bounded strings, it sends no rejection because it
cannot authenticate or safely identify a lease. Normal expiry then recovers the
assignment.

## State and budget effects

- A valid rejection changes the active assignment to `rejected` and the job to
  `queued` in one transaction.
- Rejected assignments are excluded from `_retry`'s assignment count.
- Expired leases and transient/permanent execution failures remain budgeted.
- No attempt or verification row is created.
- Unsupported protocol and malformed assignment use identical queue behavior.

The documented formula is therefore a bound on budgeted provider/abandonment
attempts, not raw claims. An incompatible worker can repeatedly reclaim and reject
a job. Worker capability negotiation or scheduler exclusion would be needed to
bound that case, but is outside this ticket.

## Security and ordering

The coordinator requires the assignment's opaque lease token and compares it
with `secrets.compare_digest`; a wrong token returns 409 and leaves both assignment
and job unchanged. There is no unauthenticated release path.

Full validation still occurs before timeout setup, heartbeat startup, or executor
invocation. Rejection submission also happens before those steps. When credentials
cannot be recovered, the worker does not guess an ID, omit authentication, or
start a heartbeat for an invalid payload.

## Files changed

- `coordinator/src/solvenet/store.py`: rejection state transition, budget policy,
  idempotency, and inspection.
- `coordinator/src/solvenet/server.py`: rejected-result validation.
- `coordinator/tests/test_coordinator.py`: prompt recovery, budget, API validation,
  idempotency, and token-authentication tests.
- `worker/internal/daemon/daemon.go`: bounded raw claim decoding, independent
  credential recovery, classified rejection, and shared result retry logic.
- `worker/internal/daemon/daemon_test.go`: malformed/unsupported rejection,
  no-heartbeat/no-execution ordering, and unusable-credential fallback tests.
- `protocol/v1.md` and `README.md`: wire and operational behavior.

## Tests and results

Run from the repository root:

```sh
PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -v
(cd worker && go test -race ./...)
```

Result: 67 Python tests passed with 14 environment-dependent Lean/Docker tests
skipped. All Go packages passed with the race detector. Python bytecode
compilation and `git diff --check` also completed successfully.

## Remaining concerns

- Rejections are deliberately unbudgeted, so a persistently incompatible or
  faulty worker can repeatedly claim the same local job. The API remains intended
  for loopback use; future capability negotiation could prevent those claims.
- Syntactically invalid JSON cannot be inspected reliably for credentials and
  falls back to lease expiry.
- The coordinator cannot independently prove that a worker rejected before doing
  work. As with other worker metadata, this is not an attestation boundary.
