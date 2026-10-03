# Worker-local OpenAI API profiles (O1)

Model IDs are configurable: the worker offers exactly `openai/<model>` and sends
the configured ID unchanged. An unfamiliar ID requires `-openai-profile`; no
model-name prefix or release allowlist selects API behavior. Profiles are tested
contracts, not assurances that an account can access a model or that it can solve
the task. Credentials, model, profile and base URL stay worker-local. Jobs cannot
override them; HTTP redirects are refused.

| Profile | Endpoint and output contract | Output budget | Supported controls |
| --- | --- | --- | --- |
| `chat-json` | `/chat/completions`, `response_format: {type: json_object}`, one stopped text choice | `max_tokens` (legacy non-reasoning contract) | v1 temperature 0–2 and nonnegative seed; no reasoning option |
| `responses-reasoning` | `/responses`, strict `text.format` JSON schema, one completed assistant output message; stateless `store: false` | `max_output_tokens`, including reasoning | worker-local `-openai-reasoning-effort low\|medium\|high`, or omitted provider default; no temperature or seed |

Both profiles return `{"text":"..."}` for `model.respond` and
`{"proof":"..."}` for `model.generate`. The text string can contain nested
`solvenet.graph.v1` JSON; provider extraction does not parse or grant authority
to graph proposals. The coordinator ingests proposals and real Lean verifies
artifacts and the target. Responses reasoning items are ignored, and tools,
commentary messages, refusals, incomplete/truncated responses and malformed
envelopes are rejected. No fallback profile or silent option stripping occurs.

Conservative default selections are `gpt-4o-mini` → `chat-json` and `gpt-5.4` /
`gpt-6.1-sol` → `responses-reasoning`. Other aliases and snapshots require explicit
selection. These are examples, not the accepted set of names. For example,
an operator-configured worker can use:

```sh
go run ./cmd/solvenet-worker -provider openai -model <model-id> \
  -openai-profile responses-reasoning -openai-reasoning-effort medium \
  -openai-context 32768 -openai-context-bytes 32768 -openai-max-output 8192
```

Run from `worker/` with the existing worker-local `OPENAI_API_KEY` or
`OPENAI_API_KEY_FILE` configuration. Starting a worker serves coordinator jobs
and can make billable calls; automated tests use only fake loopback providers.

For external key-file setup, native/Compose launch and rotation, and the explicit
`-openai-check` / `-openai-check-paid` workflow, see
[operator setup](homelab-docker.md#optional-openai-hosted-worker). Startup and
public health reporting perform no OpenAI requests and report unobserved until
a structured generation succeeds. A standalone check does not persist readiness.

## Capacity and routing

Profiles do not imply equal model capacity. Configure `-openai-context` (tokens),
`-openai-context-bytes` (input bytes) and `-openai-max-output` per model. Context
defaults are 32768 each. Omitted output capacity resolves to 16384 for
`gpt-4o-mini` and unfamiliar model IDs, and the v1 32768 ceiling for the named
reasoning examples `gpt-5.4` and `gpt-6.1-sol`. An explicit positive output
capacity overrides this conservative default; unfamiliar models still require
an explicit API profile and operators should configure their actual capacity.
Context capacities are bounded at 1048576 and output at the v1
32768-token ceiling. Every job must fit both the model output cap and full
provider prompt admission: JSON-encoded request bytes + 512 framing units +
output-token limit ≤ context tokens; request bytes + 512 ≤ input byte capacity.
This is a conservative one-byte-per-token upper bound, not measured token usage.
The Responses output limit includes hidden reasoning, so a small limit can be
consumed without a visible answer. Limits reject locally rather than silently
reducing the output budget or truncating the prompt.

Set graph/fixed group `model_capabilities["openai/<model>"].context_tokens` and
`context_bytes` to matching worker limits (or smaller). Existing coordinator
`prompt_cost` counts the complete theorem/import wrapper, frozen messages and
1536 provider/framing units plus output tokens; integration asserts that this
upper-bounds the actual request + framing for both profiles and both job kinds.
Worker admission independently checks the actual complete request. Output caps
are worker-local; choose job budgets within them. The v1 claim protocol is
unchanged: Chat advertises `generation_settings` and `model_respond`, Responses
advertises only `model_respond`. Responses therefore does not lease jobs asking
for v1 temperature/seed controls, and direct unsupported settings also fail
locally. Existing Ollama and scripted behavior is preserved.

Usage supplied by the provider is retained for success, formatting failure,
refusal and incomplete output. Missing/null counters stay unknown; actual zero
stays zero. Raw text is bounded to 128 KiB; upstream HTTP bodies are never
forwarded. A bounded `model_not_found` code is recognized for inaccessible or
unsupported models; 401/403 mark credentials rejected, 429/408/5xx are transient,
and cancellation/deadline errors retain their identity. Failed Responses bodies
returned with successful HTTP status also preserve usage and elapsed time:
bounded `error.code` values `rate_limit_exceeded` and `server_error` are transient;
other, malformed or oversized errors are permanent. Provider error messages are
never forwarded. Local elapsed request
time is recorded in `generation.total_duration_ns` (not provider-reported compute
time). Returned text and metadata redact an exact credential echo, including
after outer-envelope JSON decoding so Unicode-escaped echoes cannot bypass
redaction of decoded task text or proof bodies.
Decoded task text uses the same bounded structural sanitizer for nested graph
JSON, including its keys and embedded JSON strings, before reaching the
coordinator. Ordinary prose is preserved; unchecked malformed nested JSON or
opaque escapes receive diagnostic markers rather than recoverable credentials.

Retained OpenAI `generation.raw_response` is a diagnostic JSON envelope, not an
exact byte-for-byte archive: complete envelopes are structurally decoded,
credential-redacted in string values and keys (including nested JSON strings),
and re-serialized. Malformed raw JSON is replaced with a fixed diagnostic
marker. Malformed nested JSON, residual opaque escape syntax and nesting beyond
the bounded inspection depth are replaced with markers inside the retained
envelope. Oversized raw output is replaced with a size-limit marker and retains
`raw_response_truncated: true`; unchecked partial envelopes are never retained.
Diagnostic JSON encoding does not HTML-escape `<`, `>` or `&`. Truncation denotes
the original model-output limit; if sanitized diagnostics alone grow beyond the
retention limit they are omitted without rejecting a bounded original output.
Extraction still validates the original output independently, and usage, failure
category and elapsed time are preserved.

## Documentation inspected and verification

Official OpenAI documentation inspected read-only on 2026-10-03:

* [Chat Completions create](https://platform.openai.com/docs/api-reference/chat/create):
  JSON mode, legacy `max_tokens`, sampling, finish reasons and optional usage.
* [Responses create](https://platform.openai.com/docs/api-reference/responses/create):
  `text.format` schema, `max_output_tokens`, output items, completion statuses,
  refusal and usage fields.
* [Reasoning guide](https://developers.openai.com/api/docs/guides/reasoning):
  model-dependent effort support and incomplete output when reasoning exhausts
  the output budget.
* [GPT-5.4](https://developers.openai.com/api/docs/models/gpt-5.4) and
  [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol):
  Responses and structured-output support; shared low/medium/high effort values.
* [GPT-4o Mini](https://developers.openai.com/api/docs/models/gpt-4o-mini):
  the documented 16384-token maximum informs its default output cap.

`worker/internal/provider/openai_profiles_test.go` tests arbitrary IDs, both job
kinds, nested graph envelopes, request fields, usage, errors and local rejection.
`integration/test_graph_collaboration.py` runs the existing graph A → B → target
scenario through each profile, compiled Go processes, coordinator HTTP and
pinned real Lean, including a formally rejected branch. No existing credential
or real OpenAI endpoint is accessed by these tests.
