# Lean verifier

## Browse API

`GET /v1/fixture-sets` lists checked-in set IDs, versions, hashes, pinned
environments and problem counts, sorted by set ID and version.
`GET /v1/fixture-sets/{set_id}/versions/{version}` returns the same set fields
plus `problems` (fixture-order compact rows with `id`, `title`, `category`,
`description`, `difficulty`) and `next_offset`. Follow a row with
`GET /v1/fixture-sets/{set_id}/versions/{version}/problems/{problem_id}`
for its statement and imports, together with set identity and descriptive
metadata. Optional `limit` (1–100, default 20) and `offset` (nonnegative,
default 0) paginate problem rows. Missing set/version/problem IDs return 404.
Fixture reference proofs are excluded from every response.

`GET /v1/runs` and `GET /v1/experiments` return compact recent activity as
`{"items": [...], "next_cursor": integer_or_null}`. Rows are newest first by
persisted insertion order, even when creation timestamps are equal or absent.
Use `?limit=20&before=<next_cursor>` to fetch older rows (`limit` defaults to
20, maximum 100). A null cursor means there are no more rows. Run rows contain
`run_id`, stored `problem_id`, `fixture_problem_id`, `fixture_set_id`,
`fixture_version`, `experiment_id`, `status`, `created_at` and initial `models`;
fixture and experiment fields are null for ad hoc runs. Experiment rows contain
`id`, `created_at`, `set_id`, `version`, `sha256`, `strategy`, `run_count`, and
`solved_count`. Fetch `GET /v1/runs/{run_id}` or
`GET /v1/experiments/{id}` for full detail. Invalid pagination returns 400.
These list responses omit candidates, raw generations, and diagnostics.

## Versioned experiments

The coordinator selects the checked-in `problems/core-v1.json` (`core`, version
`1`) or `problems/challenge-v1.json` (`challenge`, version `1`) by set ID and
version. Start all runs for the selected set in one request (substitute the
exact SHA-256 of its fixture):

```sh
sha256sum problems/core-v1.json
curl -X POST http://127.0.0.1:8080/v1/experiments \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"core-repair-001","set_id":"core","version":1,"sha256":"<sha256>","strategy":"repair","model":"ollama/qwen2.5-coder:7b"}'
```

For the harder challenge set, use `sha256sum problems/challenge-v1.json` and
send `"set_id":"challenge","version":1` with its hash and a distinct
idempotency key. Submit separate independent and repair experiments against
the same hash to compare strategies. The API uses an explicit checked-in set
allowlist and does not accept fixture paths from requests.

`strategy: "independent"` defaults to 3 initial chains and 0 repairs;
`"repair"` defaults to 1 initial chain and up to 2 repairs. Each default
allows up to three completed model generations per problem. Set `attempts`,
`max_repairs` (0 for independent, 1–2 for repair), and `max_output_tokens`
explicitly to adjust this policy. For heterogeneous initial chains, replace
`model`/`attempts`/`max_output_tokens` with `initial_jobs`, e.g.
`[{"model":"a","count":2,"max_output_tokens":512},{"model":"b","count":1,"max_output_tokens":512}]`.
Repair jobs inherit their parent model and output limit. Optional
`generation_timeout_seconds` and `max_assignments` are supported existing
execution settings. Optional `generation_settings` accepts `temperature` (0–2)
and `seed` (0–9223372036854775807) for provider-backed models. For ad hoc runs
and experiments alike, a supplied seed is the base: initial chains receive
base + their zero-based position across all initial groups. The full range must
fit the seed bound. Repairs inherit the parent's effective seed and assignment
retries reuse their job's seed. For a paired independent/repair experiment with
the same base, the first initial chain has the same seed in both arms. Without
a seed the provider chooses its default. Run/experiment settings retain the base;
each job and claim shows its effective `generation_settings`, also visible on
completed attempts and assignments in `GET /v1/runs/{id}`. Existing stored jobs
retain their original settings.
Assignment retries (failures/expiries) can exceed the strategy's completed
generation count and remain visible separately in each run's `assignments`.

The response includes `id`, persisted `config` (set ID, version, SHA-256, pinned
Lean environment, strategy, initial jobs and execution settings), and ordered
`runs` with fixture problem ID, run ID and current status. Read it again with
`GET /v1/experiments/{id}`; `GET /v1/runs/{run_id}` exposes `experiment_id`
and `fixture_problem_id`. Reposting the same normalized configuration and
`idempotency_key` returns HTTP 200 with the existing runs, including after a
coordinator restart; reusing a key with different settings returns 409. A new
experiment needs a new key. Creation and run/job insertion are a single SQLite
transaction. The fixture hash must match the checked-in file, so an old version
cannot silently run after fixture changes. Only statements and imports are copied
into run records and worker claims; fixture reference proofs are never stored as
prompts or sent to workers. Existing `POST /v1/runs` remains available for ad hoc
runs and has no experiment linkage.

## Experiment reports

`GET /v1/experiments/{id}/summary` returns a JSON snapshot. For a readable
export, use `GET /v1/experiments/{id}/summary.md` (for example,
`curl -o repair.md http://127.0.0.1:8080/v1/experiments/<id>/summary.md`).
Export the independent and repair experiment IDs separately to compare them
side by side. Both include the persisted set/version/SHA-256, Lean environment,
strategy and request configuration. Run and attempt IDs in JSON lead to
`GET /v1/runs/{run_id}` for full candidates and diagnostics; reports do not
repeat the raw text.

Request outcomes count **assignments**, including execution retries and leases
that expire or are rejected before execution. Completed generations have an
attempt; worker failures may have partial usage without an attempt. Known token
and provider-duration totals are accompanied by known/unknown counts, including
zero as a *known* value when actually reported. Missing counts are never
estimated. Provider failure classification recognizes Ollama errors; the
`Ollama proof format:` prefix identifies formatting failures, while other
unclassified failures remain separate. Provider time is reported by the worker
in nanoseconds; Lean verification time is in milliseconds. These are not
wall-clock totals for the experiment. Three requests do not imply an equal
token budget. Time to first proof is run creation to successful Lean verification
(queue and retry delays included); pre-migration runs lack timestamps and report
this duration as unavailable. In-progress experiments have provisional rates.

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
