# SolveNet

This is a project that's intended to get me more familiar with LLM harnesses.

The idea is for this to be something like folding@home but for lean proofs with LLM agents collaborating to work on the same project.

## Current prototype

A Python coordinator stores independent proof-generation jobs in SQLite. A Go
worker claims jobs over HTTP and returns a scripted proof. The coordinator checks
the proof with Lean and persists the result. No API keys or external model calls
are involved. The worker currently has one execution slot.

### Run it locally

Requirements: Python 3.11+, Go 1.22+, and either Docker or Lean 4.19 via
[elan](https://github.com/leanprover/elan). Commands below start at the repository
root unless otherwise noted.

**1. Start the coordinator**, choosing one verifier:

```sh
# Containerized verification (Docker Engine on Linux/amd64):
docker build -f Dockerfile.verifier -t solvenet-verifier:local .
PYTHONPATH=coordinator/src python3 -m solvenet.server --db solvenet.db
```

Or, for trusted local fixtures with Lean installed:

```sh
(cd lean && lake --version)
PYTHONPATH=coordinator/src python3 -m solvenet.server \
  --db solvenet.db --verifier local --project lean
```

The API listens on `127.0.0.1:8080`. Docker is the default verifier; it does not
silently fall back to executing candidate code on the host.

**2. In another terminal, submit a problem:**

```sh
curl -sS http://127.0.0.1:8080/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"statement":"(n : Nat) : n + 0 = n","attempts":3}'
```

Save the returned `run_id`.

**3. Run the scripted worker:**

```sh
cd worker
go run ./cmd/solvenet-worker -once -proof rfl
```

Omit `-once` to keep polling. `-delay 12s` simulates a slow executor and exercises
heartbeats; `-id worker-2` identifies another worker process.

**4. Inspect results** (substitute the returned ID):

```sh
curl -sS http://127.0.0.1:8080/v1/runs/RUN_ID | python3 -m json.tool
```

After verification the run should be `solved`, with one `verified` attempt and
two cancelled queued jobs. Submit `: False` with the same worker to see a rejected
proof and eventual `exhausted` run (use the continuously polling worker).

The database survives restarts. Unfinished verifications are picked up again;
unacknowledged assignments are reclaimed when their leases expire. Stop the
coordinator and worker with Ctrl-C.

### Tests

```sh
PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -v
(cd worker && go test -race ./...)
```

The Python suite exercises persistence, retries, concurrent claims, idempotent
submissions, HTTP handling, container-launch policy, and the Lean verifier. When
both Go and Lake are on PATH, it also runs the actual Go worker against the HTTP
coordinator and checks its proof with real Lean. Lean-dependent tests are skipped
when Lake is unavailable.

After building the Docker image, explicitly test its runtime on your machine:

```sh
SOLVENET_DOCKER_TEST=1 PYTHONPATH=coordinator/src \
  python3 -m unittest discover -s coordinator/tests -p test_sandbox.py -v
```

### Boundaries of this slice

- One coordinator process per SQLite database; no authentication or public API.
- Independent attempts with a bounded dispatch count, not dollar/token budgets.
- Scripted execution only; a real provider adapter can implement the Go
  `Executor` interface later.
- The container runs without networking, capabilities, credentials, or a Docker
  socket, with read-only rootfs and memory/CPU/process/file/output/time limits.
  Only the per-attempt workspace is bind-mounted writable.
- The container restricts host access; it does not independently authenticate
  proofs against hostile Lean metaprograms that tamper with their own verifier
  process. Remote untrusted contributors still require further verification work.

See [the worker protocol](protocol/v1.md) and
[verifier documentation](coordinator/README.md).
