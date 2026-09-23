# Ticket 6: Execution failure classification

## Design

Failed worker results have one small classification: `transient` or `permanent`.
Transient failures requeue under the job's persisted `max_assignments` limit.
Permanent failures mark the job failed immediately; the run becomes `exhausted`
when no other active job can produce work. Classification is retained in the
assignment result JSON and exposed as `assignments[].failure_class`.

The Ollama adapter classifies network/read errors, HTTP 408/429/5xx responses,
and provider error envelopes as transient. Invalid job/context configuration,
other HTTP 4xx responses, oversized or invalid envelopes, incomplete responses,
invalid metadata, and proof-format extraction failures are permanent. The worker
also treats its own empty/oversized executor output as permanent. Unclassified
executor errors default to transient rather than inventing a broader taxonomy.

## Compatibility

The wire field is additive and requires no SQLite schema migration because the
complete result is already persisted as JSON. A failed result from an older worker
that omits `failure_class` is normalized to `transient`, preserving v1's previous
bounded retry behavior. Inspection also reports historical failed results that
lack the field as transient. Completed assignments report a null classification.

## Files changed

- `coordinator/src/solvenet/store.py`: normalize, persist, act on, and inspect the classification.
- `coordinator/src/solvenet/server.py`: validate the additive failed-result field.
- `worker/internal/daemon/daemon.go`: classified errors and result serialization.
- `worker/internal/provider/ollama.go`: focused Ollama failure decisions.
- Python and Go tests: compatibility, transitions, wire output, and provider cases.
- `protocol/v1.md` and `README.md`: retry semantics and inspection behavior.

## Tests and results

Run from the repository root:

```sh
PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -v
(cd worker && go test -race ./...)
```

Result: 58 Python tests passed with 14 environment-dependent Lean/Docker tests
skipped. All Go packages passed with the race detector. Python bytecode
compilation and `git diff --check` also completed successfully.

## Remaining concerns

Ollama's HTTP-200 `error` envelope does not provide a stable machine-readable
error code, so it is conservatively transient. A later provider API with reliable
codes can refine that adapter without expanding the protocol taxonomy.
