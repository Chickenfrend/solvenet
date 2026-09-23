# Ticket 17 — shared protocol limits

The v1 wire contract is documented in [protocol/v1.md](../protocol/v1.md).
The authoritative *in-language* definitions are
`coordinator/src/solvenet/protocol_limits.py` and
`worker/internal/daemon/limits.go`; the two files are maintained manually.
Neither side reads limits from runtime configuration.

| Limit | Maximum | Owner / uses |
| --- | ---: | --- |
| Identifier (including worker ID and lease token) | 256 UTF-8 bytes | Coordinator claim/result validation; worker assignment and credential recovery |
| Statement | 64 KiB UTF-8 bytes | Coordinator run submission; worker assignment |
| Imports / claim models | 32 entries each | Coordinator submission/claim; worker imports validation |
| Import module / model | 256 UTF-8 bytes each | Coordinator submission/claim; worker assignment; Ollama model configuration and metadata |
| Messages / message content | 32 / 256 KiB UTF-8 bytes each | Worker assignment validation (coordinator currently emits at most one repair message) |
| Candidate (`output.text`) | 128 KiB UTF-8 bytes | Worker output check; coordinator result validation |
| Generation raw response | 128 KiB UTF-8 bytes | Ollama UTF-8-safe prefix; coordinator generation validation |
| Generation finish reason | 256 UTF-8 bytes | Ollama response metadata; coordinator generation validation |
| Error message | 4096 UTF-8 bytes | Coordinator failed/rejected result validation; worker local truncation |
| Output-token budget | 32768 | Coordinator submission; worker assignment; Ollama provider |
| Generation timeout / heartbeat interval | 86400 seconds each | Coordinator run timeout/assignment lease; worker assignment validation |
| Regular / result POST body | 256 KiB / 2 MiB encoded bytes | Coordinator HTTP transport; result cap accommodates JSON escaping |

These are protocol maxima rather than chosen defaults. Body limits count bytes
*before* JSON decoding; decoded string limits count UTF-8 bytes even when the
wire uses `\u` escaping. The coordinator's 32-lowercase-hex path syntax is a
narrower rule for its own issued IDs, while worker assignment credentials retain
the preexisting 256-byte opaque-field bound. The worker's 2 MiB claim-response
read cap is an independently chosen transport policy, not a v1 response limit.
Bounds apply together: a 64 KiB decoded statement consisting mostly of escaped
control characters can exceed the regular 256 KiB request cap and receive 413.
The 2 MiB result cap accommodates the worst-case JSON escaping of both 128 KiB
candidate and 128 KiB raw response (plus normal metadata).

Local implementation/experiment policy is kept separate: the Ollama HTTP reply
cap (1 MiB) and `num_ctx` bounds, diagnostic excerpts (8 KiB repair, 64 KiB
verifier), Docker/Lean time and resource limits, and coordinator run attempt,
repair, and retry budgets. Historical SQLite migrations retain literal CHECK
values as immutable schema history; their timeout maximum agrees with the wire
maximum. Worker error messages use a UTF-8-safe 4096-byte prefix.

## Changes and compatibility

The previously duplicated numeric bounds have been moved to named per-language
definitions and reused in server, store, worker validation/output, and Ollama
provider paths. No v1 numeric limit, endpoint, persisted schema, or default was
changed. Ollama's model-tag validation now derives its 249-byte maximum from the
256-byte wire model bound minus the `ollama/` prefix. The worker's output-size
error now derives its printed maximum from the same candidate constant. The
worker now retains up to the full 4096-byte error allowance instead of its old
4000-byte local truncation, using a UTF-8-safe prefix; coordinators already
accepted 4096 bytes. Existing coordinators and workers remain wire-compatible;
the documentation clarifies that
the response cap and provider limits are not extra protocol requirements.

## Verification

- `PYTHONPATH=src python -m unittest discover -s tests -q` (from `coordinator`):
  88 run, 73 passed, 15 environment/opt-in Lean or Docker tests skipped. The added
  HTTP tests cover exact/over-limit Unicode strings, numeric budgets, and both
  encoded request body sizes.
- `go test -race ./...` (from `worker`): all three packages passed. Added tests
  cover exact/over-limit assignment strings, collections and numeric fields,
  candidate submission, and UTF-8-safe raw-response clipping.

Remaining concern: the coordinator can construct repair feedback whose total
message is larger than the 256 KiB wire cap if historical or direct-store data
bypasses HTTP candidate validation; ordinary API-submitted candidates fit within
the cap. This is a separate store-level input-validation issue.
