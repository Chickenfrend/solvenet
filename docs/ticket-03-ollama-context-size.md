# Ticket 3: Ollama context size

## Problem

The worker fixed Ollama's `num_ctx` request option and generation metadata at
4096 tokens. Runs could request larger output budgets, and repair prompts could
grow beyond that context, but changing the context required recompiling the
worker.

## Design decisions

- `solvenet-worker` accepts `-ollama-context`, defaulting to the previous value of
  4096 tokens.
- The Ollama provider stores the selected context and uses it for both the
  `num_ctx` request option and `generation.context_length`, including metadata
  retained from unsuccessful HTTP responses.
- Context sizes must be between 1 and 1,048,576 tokens. The upper bound is a
  local implementation safety limit that permits current long-context models
  without accepting accidentally enormous values.
- `job.max_output_tokens` must be strictly less than the selected context. The
  prompt is nonempty, so an equal or larger output budget cannot fit in that
  total context. This guaranteed incompatibility is rejected before any Ollama
  request, with an error that names both values and suggests changing the output
  budget or `-ollama-context`.
- When the output budget is smaller than the context, the worker sends both
  values unchanged. It does not estimate tokens locally; Ollama remains
  responsible for fitting or truncating the variable-length prompt.

## Files changed

- `worker/internal/provider/ollama.go`: provider context state, validation,
  request propagation, metadata propagation, and incompatible-budget error.
- `worker/internal/provider/ollama_test.go`: context boundaries, option/metadata
  agreement, and output-budget validation.
- `worker/cmd/solvenet-worker/main.go`: parsed worker configuration and the new
  CLI option.
- `worker/cmd/solvenet-worker/main_test.go`: default, custom, and invalid CLI
  context tests.
- `README.md` and `protocol/v1.md`: operator behavior and metadata semantics.

## Tests and results

From `worker/`:

```text
go test -race ./...
# all packages passed

go vet ./...
# passed

git diff --check
# passed
```

## Remaining concerns

The worker cannot know the model's actual supported context or precisely count
tokens for model-specific tokenizers. Ollama may reject an unsupported context
or truncate an oversized prompt; its response is retained through the existing
provider error handling. A model-capability registry or tokenizer abstraction
would be premature for this prototype.
