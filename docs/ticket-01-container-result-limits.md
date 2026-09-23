# Ticket 1: Container result limits

## Problem

Lean diagnostics were limited to 64 KiB, while the container transport read only
128 KiB of JSON. JSON escaping can encode one control-character byte as six
bytes, so an ordinary bounded Lean rejection could be misreported as a
`verifier_error`.

## Design

- `MAX_DIAGNOSTICS_BYTES` names the verifier diagnostic limit.
- `MAX_CONTAINER_RESULT_BYTES` allows for the worst-case JSON expansion of a
  maximum diagnostic plus fixed envelope overhead.
- Container JSON is encoded explicitly as UTF-8 with literal Unicode.
- Diagnostics are truncated on a complete UTF-8 character boundary and receive
  an explicit `[diagnostics truncated]` marker.
- Container results validate `status`, `diagnostics`, and nonnegative integer
  `elapsed_ms` values before constructing a `VerificationResult`.

The guest truncates before serialization and the host applies the same bound
when decoding. Thus oversized diagnostics remain useful rejections rather than
becoming transport failures.

## Files changed

- `coordinator/src/solvenet/verifier.py`: named limit and shared UTF-8-safe
  truncation.
- `coordinator/src/solvenet/sandbox.py`: size-safe serialization, envelope
  sizing, and complete result validation.
- `coordinator/tests/test_verifier.py`: Unicode truncation boundary coverage.
- `coordinator/tests/test_sandbox.py`: JSON expansion, Unicode truncation, and
  elapsed-time validation coverage.

## Tests

Run from `coordinator/`:

```text
python -m pytest
# unavailable: pytest is not installed

PYTHONPATH=src python -m unittest discover -s tests -v
# result: 49 run; 35 passed and 14 skipped (optional integration tests)
```

## Remaining concerns

The envelope limit assumes results are produced by the bundled guest writer.
Arbitrarily large or malformed third-party result files are intentionally
rejected as infrastructure errors.
