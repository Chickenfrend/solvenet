# Ticket 14: Bounded Docker launch diagnostics

## Design

The container verifier now drains `docker run` stderr through a pipe while
retaining only the first `MAX_DOCKER_STDERR_BYTES` (8 KiB), plus one byte used
to detect truncation. A nonzero Docker exit includes the excerpt under the
explicit `Docker stderr:` label. Infrastructure errors discovered after Docker
returns, such as a malformed result file, can include the same excerpt.

Normal verified and rejected results continue to use only `result.json`.
Docker stderr is also omitted from deadline diagnostics, preserving the
existing timeout result. The Docker CLI is killed on timeout, and the named
container is forcibly removed before its bind-mounted temporary workspace is
deleted.

## Security and bounds

- The pipe is continuously drained, so a noisy Docker process cannot block on
  stderr while only 8 KiB plus one byte is retained in coordinator memory.
- Truncated excerpts end with `[Docker stderr truncated]` and are decoded only
  at a complete UTF-8 boundary.
- The known host-side temporary workspace path is replaced with
  `[verification workspace]` before diagnostics are returned.
- Stdout remains discarded. Stderr is exposed only for infrastructure errors,
  not as proof diagnostics or successful output.

Docker daemon messages may still contain operator-configured image names or
runtime details. The coordinator does not attempt broad heuristic redaction,
which could remove the actionable reason for a launch failure.

## Files changed

- `coordinator/src/solvenet/sandbox.py`: bounded Docker stderr runner,
  formatting/redaction, and infrastructure-error integration.
- `coordinator/tests/test_sandbox.py`: launch failure, truncation, Unicode,
  cleanup/timeout, capture bound, and success isolation tests.
- `README.md` and `coordinator/README.md`: operator-visible behavior and limit.
- `docs/ticket-14-docker-launch-diagnostics.md`: design record.

## Tests and results

Run from the repository root:

```sh
PYTHONPATH=coordinator/src python3 -m unittest coordinator.tests.test_sandbox -v
```

Result: 12 tests run: 11 passed and one opt-in Docker smoke test was skipped
because `SOLVENET_DOCKER_TEST` was unset. The full coordinator suite ran 70
tests: 56 passed and 14 environment-dependent tests were skipped.

The repository environment does not provide the optional `pytest` package, so
the standard-library unittest runner was used.

## Remaining concerns

- Real Docker smoke coverage remains opt-in and depends on a locally built
  `solvenet-verifier:local` image.
- Docker daemon text outside the known temporary workspace path is retained by
  design because it commonly carries the actionable image/runtime error.
