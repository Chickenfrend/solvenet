# Ticket 12: worker assignment validation

## Validation table

| Field | Worker requirement |
| --- | --- |
| `protocol_version` | Required and exactly `1`; another value produces a typed unsupported-version error |
| `assignment_id`, `lease_token`, `job.id` | Nonempty, at most 256 UTF-8 bytes |
| `lease_expires_at` | Positive finite Unix timestamp; not compared with the worker clock |
| `heartbeat_seconds` | Greater than zero, at most 86400 seconds |
| `job.kind` | Exactly `model.generate` |
| `job.model` | Nonempty, at most 256 bytes, and equal to the worker's advertised model |
| `job.statement` | Nonempty, at most 64 KiB |
| `job.imports` | 1–32 nonempty strings, each at most 256 bytes |
| `job.messages` | At most 32 messages |
| Message role/content | Role is `system`, `user`, or `assistant`; content is nonempty and at most 256 KiB |
| `job.max_output_tokens` | 1–32768 |
| `job.timeout_seconds` | 1–86400 seconds |
| Repair fields | Nonnegative depth; parent is null at depth zero and a nonempty, at-most-256-byte ID at positive depth |

## Design

Validation is centralized in the daemon and runs immediately after a successful
claim decode, before timeout setup, heartbeat startup, or executor invocation.
Errors name the invalid wire field. `UnsupportedProtocolVersionError` makes a
coherent but unsupported protocol response distinguishable with `errors.As`
from malformed v1 data. A custom unmarshal records whether `protocol_version`
was omitted so absence is malformed rather than being reported as version zero.

The Go assignment wire type now includes `lease_expires_at`. It is useful for
shape validation and diagnostics, but execution deliberately does not depend on
clock agreement with the coordinator. Heartbeats remain the lease-renewal signal.
Unknown additive JSON fields remain accepted for v1 compatibility.

The bounds are named local constants needed by this validator. This ticket does
not introduce a cross-language limit package or broader conformance framework;
that remains Ticket 17.

## Compatibility

Current coordinator assignments satisfy these requirements. Old or custom
coordinators that omitted documented required fields are now rejected before a
provider can observe a zero value. Valid v1 payloads may still carry unknown
additive fields. Assignment rejection/release behavior is unchanged and remains
Ticket 13.

## Files changed

- `worker/internal/daemon/daemon.go`
- `worker/internal/daemon/daemon_test.go`
- `protocol/v1.md`
- `README.md`
- `docs/ticket-12-worker-assignment-validation.md`

## Tests and results

- `go test -race ./...` from `worker/`: all packages passed.
- `PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -v`:
  64 tests passed; 14 skipped because Lake or the opt-in Docker smoke test was
  unavailable.

Daemon tests cover malformed IDs/tokens, lease and heartbeat values, job ID,
kind/model, statement, imports, messages and roles/content, output and timeout
limits, and parent/depth consistency. Every malformed case asserts that the
executor was not called. Unsupported and omitted protocol versions are tested
separately.

## Remaining concerns

- A rejected assignment remains leased until expiry because immediate release is
  intentionally Ticket 13.
- Limits are still manually mirrored across Go, Python, and documentation;
  consolidation and exact boundary conformance tests remain Ticket 17.
- Message content can fit the wire limit while exceeding a configured model's
  practical context window; provider context handling is unchanged.
