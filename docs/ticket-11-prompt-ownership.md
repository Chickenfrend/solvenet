# Ticket 11: provider prompt ownership

## Old coupling

The coordinator emitted a plain-text proof-format system instruction and repeated
the theorem as a user message, even though assignments also had structured
`statement` and `imports` fields. The Ollama adapter supplied its own, different
JSON output contract and tried to remove the coordinator instruction and repeated
theorem by comparing their exact contents. A coordinator wording or whitespace
change could therefore duplicate trusted context or expose contradictory output
instructions to the model.

## New wire and prompt semantics

The existing v1 fields remain in place:

- `job.statement` and `job.imports` are the trusted problem context.
- `job.messages` contains only coordinator strategy or repair feedback. An
  initial job currently sends an empty list.
- A repair sends one user message containing the previous candidate and bounded
  Lean diagnostics. It is the final wire message and final Ollama user message.

The Ollama provider exclusively owns its output-format system instruction and
JSON schema. It builds one problem-context message from `statement` and `imports`,
then appends coordinator messages in wire order. It does not compare message
content with the theorem or known coordinator prose.

This is deliberately direct prompt construction, not a reusable prompt framework.

## Compatibility

The JSON shape and protocol version are unchanged. Existing workers can consume
new assignments because `messages` was already a list and trusted context remains
available in the structured fields. The current scripted worker ignores messages.

An old coordinator paired with the new Ollama worker can still be decoded, but
its legacy repeated theorem and output-format instruction are appended verbatim;
the worker no longer guesses their meaning from content. Upgrade the coordinator
before or together with the worker to get the new ownership guarantees.

## Files changed

- `coordinator/src/solvenet/store.py`
- `coordinator/tests/test_repairs.py`
- `worker/internal/provider/ollama.go`
- `worker/internal/provider/ollama_test.go`
- `protocol/v1.md`
- `README.md`
- `docs/ticket-11-prompt-ownership.md`

## Tests and results

Results for this ticket:

- `PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -v`:
  64 tests passed, 14 skipped because Lake or the opt-in Docker smoke test was
  unavailable.
- `go test -race ./...` from `worker/`: all three packages passed.

Tests assert that initial coordinator messages are empty, repair feedback is one
final user message, Ollama composes structured context once for valid assignments,
and coordinator messages are not removed based on matching content.

## Remaining concerns

- v1 message roles and sizes still receive only the worker's existing validation;
  complete assignment validation is Ticket 12.
- Legacy coordinators do not provide the new semantic guarantee despite using the
  same wire shape. Supporting them by recognizing prompt prose would recreate the
  coupling this ticket removes.
- The Ollama context budget can still be exceeded by large problem or repair text;
  existing context/output-budget behavior is unchanged.
