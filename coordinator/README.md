# Lean verifier

Requires Python 3.11+ and Linux/POSIX process-group support. Install Lean using
[elan](https://github.com/leanprover/elan); `lean/lean-toolchain` pins Lean 4.19.0.
From the repository root, download the toolchain before running timed checks:

```sh
(cd lean && lake --version)
PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -v
```

Without Lake on PATH the real-Lean tests are skipped; process-runner tests still
run. The suite includes real axiom-spoofing, declaration-injection, placeholder,
early-exit, and parameterized-theorem cases.

## Verify a proof

```sh
printf 'rfl\n' > /tmp/proof.lean
PYTHONPATH=coordinator/src python3 -m solvenet.verifier \
  --project lean \
  --statement '(n : Nat) : n + 0 = n' \
  --candidate-file /tmp/proof.lean
```

The command prints JSON containing `status`, `diagnostics`, and `elapsed_ms`.
Exit status is zero only for `verified`. Imports default to `Init`; repeat
`--import MODULE` to select modules installed in the Lake environment.

## Coordinator health checks

`GET /health` is a cheap liveness check and always remains independent of the
verifier runtime. `GET /ready` invokes the configured verifier's readiness check.
Local readiness checks that the project directory contains `lean-toolchain` and
`lakefile.toml`, then runs Lean on a generated trusted source that imports `Lean`
and `Init`. Container readiness starts the configured image with networking and
capabilities disabled and runs that same trusted check inside it. It uses
`--pull=never`, so it reports a missing local image instead of downloading one.

Success is HTTP 200 `{"status":"ready"}`. Missing project files, commands,
Docker daemon/image, timeouts, or a failed trusted import return HTTP 503 with
`status: "unavailable"` and at most 8 KiB of diagnostics. The check never accepts
or executes a submitted theorem or candidate. It can still be relatively
expensive, especially in Docker mode, and is intentionally uncached for this
single-node prototype; use `/health` for frequent liveness polling.

## Acceptance and execution

The statement and imports are trusted problem inputs. Each invocation first
checks their elaboration separately, so broken environments or problem statements
produce `verifier_error`. The candidate is parsed as exactly one theorem
declaration. A Lean harness checks its declaration kind, original type and
universe parameters, and transitive axiom dependencies. Only `propext`,
`Classical.choice`, and `Quot.sound` are allowed by default. `sorryAx` and the
temporary expected-type axiom are rejected. Console text is diagnostic only.
A completion receipt also guards against an early successful process exit.

The total wall-clock deadline includes preflight. Output is collected with a
bounded buffer; exceeding the limit rejects the attempt. Timeouts and excessive
output terminate the process group, including Lake's children. Abnormal exits
produce `verifier_error`. A normal compiler exit of 1 after preflight is treated
as rejection; an environment that changes between the two invocations can still
cause a misclassified failure.

The coordinator defaults this Lean deadline to 10 seconds and retains 64 KiB of
diagnostics. `solvenet.server --lean-timeout SECONDS` changes the deadline for
both local and container verification. For Docker, `--container-timeout SECONDS`
sets the outer deadline (default 30 seconds), which must leave at least one
second beyond the Lean deadline for container overhead. The inner deadline and
diagnostic limit are included in each container request rather than relying on
image defaults. A failed Docker launch includes a UTF-8-safe, bounded 8 KiB
stderr excerpt in its `verifier_error` diagnostics. The known temporary host
workspace path is redacted, and Docker stderr is omitted from successful,
rejected, and timeout results.

This is a local experiment runner, **not an isolation boundary against hostile
Lean metaprograms**. Tactics can execute IO in the Lean process, including reading
or modifying its files. The completion receipt does not authenticate against
such code. Do not expose this runner to remote contributors as-is: process
isolation and independently checking exported proof artifacts are needed for
that threat model. The runner currently limits output and wall-clock time, not
total CPU, memory, or filesystem usage.
