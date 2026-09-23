# SolveNet

This is a project that's intended to get me more familiar with LLM harnesses.

The idea is for this to be something like folding@home but for lean proofs with LLM agents collaborating to work on the same project.

## Current prototype

A Python coordinator stores proof-generation jobs and bounded repair chains in SQLite. A Go
worker claims jobs over HTTP and generates a proof using a local Ollama model or
a scripted fixture. The coordinator checks the proof with Lean and persists the
result, original model response, and reported usage. The worker currently has one
execution slot.

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

Verification has a 10-second total Lean deadline (including preflight). The
Docker verifier has a 30-second outer deadline and requires at least one second
beyond the Lean deadline for startup and cleanup. Override these with
`--lean-timeout SECONDS` and `--container-timeout SECONDS`; local verification
uses only the Lean deadline. The Docker defaults are 1 CPU, 1 GiB memory and
swap, 64 processes, a 1 MiB file-size limit, a 128 MiB `/tmp`, and 64 KiB of
retained diagnostics. These resource values are available through the Python
`DockerResourceLimits` API rather than coordinator flags.

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

### Use your local Ollama model

Ensure Ollama is running and the model has already been downloaded:

```sh
ollama list
curl -sS http://127.0.0.1:11434/api/version
```

Restart the coordinator with the current code (Ctrl-C in its terminal, then the
same start command). Existing SQLite databases migrate automatically to schema
6, preserving previous results and repair links. Upgrade the coordinator before
the worker so generation metadata is retained. The Lean verifier image does not
need to be rebuilt for this adapter change.

From the repository root, submit a model-specific run and save its ID in your
shell so it can be used directly:

```sh
RUN_ID=$(curl -fsS http://127.0.0.1:8080/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"statement":"(n : Nat) : n + 0 = n","attempts":1,"model":"ollama/qwen2.5-coder:7b","max_output_tokens":256,"generation_timeout_seconds":120}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')

(cd worker && go run ./cmd/solvenet-worker \
  -provider ollama -model qwen2.5-coder:7b \
  -ollama-url http://127.0.0.1:11434 -ollama-context 8192 \
  -id archdeepthought -once)

curl -fsS "http://127.0.0.1:8080/v1/runs/$RUN_ID" | python3 -m json.tool
```

Allow a few seconds for verification after the worker submits. Inspect
`attempts[].candidate`, `verification_status`, `usage`, and `generation`.
`attempts[].model` is the requested identifier (`ollama/...`);
`generation.model` is the model name returned by Ollama, not a pinned weight digest.
For execution/formatting failures, inspect `assignments[].error`,
`failure_class`, `generation`, and `usage`. Transient service/network failures
retry within the job's assignment limit. Invalid worker configuration and
deterministically malformed provider output are permanent and fail that job
immediately. `-once` handles just one assignment; omit it to keep polling and
process bounded retries. A model that emits an invalid tactic gives a completed,
rejected attempt, not an executor error.

The adapter uses `/api/chat` with a JSON schema requiring `{"proof":"..."}`,
the job's output-token limit, and the context selected by `-ollama-context`
(default 4096 tokens; valid range 1–1048576). The same context is sent as
Ollama's `num_ctx` and recorded as `generation.context_length`. A job is rejected
before contacting Ollama when `max_output_tokens` is greater than or equal to the
context size, because the nonempty prompt also needs context space. Choose a
larger context or a smaller output budget. For smaller output budgets, Ollama is
responsible for fitting or truncating the prompt within the selected context. Set
`generation_timeout_seconds` on run submission to choose a 1–86400 second
generation deadline (default 120); a cold model load counts toward that deadline.
The coordinator persists the policy on each job, including repairs, and run
inspection reports it. The worker uses the requested valid timeout without a
separate silent cap. Heartbeats continue during generation. Cancellation closes
the HTTP request. The worker does not automatically download models. Repair
requests carry explicit previous-proof and diagnostic text rather than
provider-specific conversation state.

Prompt ownership is explicit: the coordinator sends trusted problem context in
the structured `statement` and `imports` fields and reserves `messages` for
strategy or repair feedback. The Ollama provider creates the model-facing problem
message and is the sole owner of its JSON/proof output instructions. It appends
coordinator messages in order without exact-string filtering. Initial jobs have
no coordinator messages; repair feedback remains the final user message.
Before invoking a provider, the worker validates every required assignment and
job field, including bounded strings and collections, repair linkage, heartbeat,
and generation limits. Malformed assignments and unsupported protocol versions
do not reach the provider. If the assignment ID and lease token are usable, the
worker submits an authenticated `rejected` result before starting heartbeats and
the coordinator immediately requeues the job. This pre-execution rejection is
visible through `assignments[].rejection_kind` and does not consume the assignment
budget. Without both usable credentials the worker cannot safely release work, so
lease expiry remains the fallback. Lease expiration is decoded for protocol
conformance, but renewal scheduling uses the heartbeat interval rather than the
worker clock.

Extraction only trims whitespace and optionally removes a single Lean Markdown
fence **inside** the proof field. Full declarations, outer `by`, missing/invalid
JSON, or an empty proof are execution failures. Tactic names and logic are never
rewritten. Ollama response bodies are bounded to 1 MiB; retained generated text
is capped at 128 KiB with an explicit truncation flag. Unknown token counts are
null/omitted. Provider durations are reported in nanoseconds. If generation hits
its token limit but still returns a complete proof object, that candidate is
verified normally and the finish reason is retained.

### Bounded repair runs

Set `max_repairs` to 2 for one initial attempt followed by at most two repairs:

```sh
RUN_ID=$(curl -fsS http://127.0.0.1:8080/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"statement":"(n : Nat) : n + 0 = n","attempts":1,"max_repairs":2,"model":"ollama/qwen2.5-coder:7b","max_output_tokens":256}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
echo "$RUN_ID"

(cd worker && go run ./cmd/solvenet-worker \
  -provider ollama -model qwen2.5-coder:7b -id archdeepthought)
```

Leave this worker running: unlike `-once`, it will pick up repairs as the
coordinator verifies each candidate. In another terminal, inspect the run using
the printed ID (`RUN_ID` is a shell variable, not shared between terminals).
Alternatively, stop the worker with Ctrl-C after it finishes submitting work and
use the existing shell variable:

```sh
curl -fsS "http://127.0.0.1:8080/v1/runs/$RUN_ID" | python3 -m json.tool
```

The run reports `max_repairs`; jobs and attempts report `repair_depth` (0, 1, 2)
and `parent_attempt_id`. Each repair receives the original problem, previous
candidate, and up to 8 KiB of that attempt's Lean diagnostics. Full diagnostics
remain stored. Repair feedback is the final model message and explicitly asks the
model not to repeat the rejected candidate unchanged. Long problems/proofs can
still exceed the configured model context.

Only a Lean `rejected` outcome creates a repair. Success stops the run;
`verifier_error` stops it with an error; verification timeout ends that chain.
Lean verification is authoritative when already-dispatched results arrive late:
a verified proof upgrades an `error` or `exhausted` run to `solved`, while a late
verifier error cannot downgrade `solved`. Active assignments may finish after a
terminal transition; their candidates and verification outcomes remain recorded.
Transient provider failures and lost leases use the existing bounded assignment
retries on the same job rather than creating a repair. Permanent execution and
formatting failures stop that job without using its remaining allowance. Set
`max_assignments` from
1–100 on run submission to choose that per-job limit (default 3). Initial and
repair jobs persist the same setting. Thus a three-job chain can involve more than
three model calls if execution fails; with the default, it allows at most nine
dispatches. In general the bound is
`attempts * (1 + max_repairs) * max_assignments` for provider execution,
transient failures, and abandoned leases. Pre-execution malformed/unsupported
assignment rejections are excluded, so repeated claims by an incompatible worker
can make the raw dispatch count exceed that bound without spending model work.

Repairs are opt-in: `max_repairs` defaults to 0 and accepts 0–2. This range is an
API policy, not a database limit; the schema only requires nonnegative repair
budgets and depths, so changing the API ceiling does not require another table
migration. `attempts` means
the number of initial independent chains. To compare strategies, use
`attempts: 3, max_repairs: 0` versus `attempts: 1, max_repairs: 2`. Record actual
tokens and timings as well as requests because repair prompts are longer.
Existing runs remain independent after migration; submit a new run to enable repairs.

### Tests

```sh
PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -v
(cd worker && go test -race ./...)
```

The Python suite exercises persistence, retries, concurrent claims, idempotent
submissions, HTTP handling, container-launch policy, and the Lean verifier. When
Go is available, it also runs the actual Go worker against a fake Ollama HTTP
server and verifies success, proof rejection, and formatting-error retention.
With Lake on PATH, that integration uses real Lean. Lean-dependent tests are
skipped when Lake is unavailable. Go tests cover the schema/options, conservative
extraction, provider errors, output limits, missing usage, and cancellation.
Automated tests do not contact a real Ollama server or download models.
Repair tests cover bounds, linked history, restart recovery, duplicate verification
delivery, execution retries, terminal runs, and migration. With Go and Lake,
an end-to-end test exercises two repairs using actual Lean diagnostics and a
fake Ollama server.

After building the Docker image, explicitly test its runtime on your machine:

```sh
SOLVENET_DOCKER_TEST=1 PYTHONPATH=coordinator/src \
  python3 -m unittest discover -s coordinator/tests -p test_sandbox.py -v
```

### Boundaries of this slice

- One coordinator process per SQLite database; no authentication or public API.
- Independent attempts and optional repair chains with a bounded dispatch count,
  not dollar/token budgets.
- Scripted and Ollama execution; hosted-provider adapters can implement the same
  Go `Executor` interface later.
- The container runs without networking, capabilities, credentials, or a Docker
  socket, with read-only rootfs and memory/CPU/process/file/output/time limits.
  Only the per-attempt workspace is bind-mounted writable.
- The container restricts host access; it does not independently authenticate
  proofs against hostile Lean metaprograms that tamper with their own verifier
  process. Remote untrusted contributors still require further verification work.

See [the worker protocol](protocol/v1.md) and
[verifier documentation](coordinator/README.md).
