# Ticket 16: Verifier readiness

## Semantics and checks

- `GET /health` remains a cheap HTTP liveness check. It does not call the
  verifier and returns 200 `{"status":"ok"}` even when verification is
  unavailable.
- `GET /ready` calls the configured verifier and returns 200
  `{"status":"ready"}` only when its prerequisites pass.
- Local readiness requires the configured project directory, `lean-toolchain`,
  and `lakefile.toml`. It then runs the configured Lean command on a generated,
  trusted source that imports `Lean` and `Init` and checks `True`.
- Container readiness runs the configured image with `--pull=never`, no network,
  a read-only root filesystem, dropped capabilities, resource limits, and a
  writable temporary `/tmp`. The image performs the local trusted smoke check.
  This detects common missing Docker executable/daemon, missing image, and broken
  image toolchain/project conditions.
- Neither readiness implementation accepts a statement or candidate proof.

## Status codes and diagnostics

- Ready: HTTP 200 with `{"status":"ready"}`.
- Unavailable: HTTP 503 with
  `{"status":"unavailable","diagnostics":"..."}`.
- Operator diagnostics are UTF-8 safe and bounded to 8 KiB. Docker stderr uses
  the existing bounded capture before the readiness bound is applied.
- Unexpected HTTP-handler failures retain the existing JSON 500 behavior.

## Security and cost

The check executes only source text embedded in the coordinator. Container mode
does not mount a candidate workspace or expose a Docker socket to the guest. The
API is still an unauthenticated loopback-only prototype, and diagnostics can
reveal tool/runtime details, so it must not be exposed publicly.

Local readiness starts Lean and Docker readiness starts a container. The endpoint
is therefore more expensive than liveness and should be probed less frequently.
No cache was added: this prototype is local, and fresh results avoid cache
invalidation or stale-ready behavior.

## Files changed

- `coordinator/src/solvenet/verifier.py` — readiness result and trusted local
  project/toolchain smoke check with bounded diagnostics.
- `coordinator/src/solvenet/sandbox.py` — restricted container readiness launch
  and image entrypoint mode.
- `coordinator/src/solvenet/server.py` — `/ready` route and 200/503 responses.
- `coordinator/tests/test_verifier.py` — local readiness success and missing
  project/command tests without requiring Lean.
- `coordinator/tests/test_sandbox.py` — mocked Docker/image readiness tests.
- `coordinator/tests/test_coordinator.py` — readiness API and liveness separation.
- `protocol/v1.md`, `README.md`, and `coordinator/README.md` — endpoint and
  operator documentation.

## Tests and results

From the repository root:

```text
PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -v
Ran 86 tests in 7.952s
OK (skipped=15)
```

Readiness unit/API tests mock Docker or use the existing fake Lean executable;
they do not require real Docker or Lean. The existing opt-in Docker smoke test
remains available with `SOLVENET_DOCKER_TEST=1`. The skipped tests require Lake
or opt-in Docker. The Go-backed Ollama API test ran successfully.

## Remaining concerns

- Readiness is evaluated per request and concurrent probes can start multiple
  Lean processes or containers. Add a short cache only if monitoring load becomes
  material.
- A successful point-in-time check cannot guarantee the next verification will
  have enough resources or that the runtime will remain available.
- Docker readiness validates the configured image by running it, not by checking
  that its tag resolves to an expected immutable digest.
