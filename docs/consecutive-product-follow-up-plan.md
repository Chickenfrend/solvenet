# Consecutive-product follow-up

Execute sequentially with a fresh build agent for implementation and a separate
fresh build agent for review at each stage. Run relevant checks and commit each
stage before proceeding. Credentials remain worker-local.

## 1. Artifact proof-output contract

Inspect graph instructions and worker prompts. Specify tactic bodies beneath
the verifier's existing `:= by` wrapper: no leading `by`, declarations, or
Markdown. Preserve exact candidate strings rather than silently repairing them.
Add focused prompt/contract checks, including real Lean on a valid example.

Acceptance: artifact instructions unambiguously match source construction;
existing provider envelopes and graph ingestion remain compatible.

Implementation status (2026-10-09): implemented; awaiting separate review.
`coordinator/src/solvenet/graph_instructions.py` now specifies Lean tactic bodies
beneath the existing `:= by` wrapper, excludes leading `by`, declarations,
Markdown and explanation, and includes an unrelated `: True` syntax example.
Candidate strings remain exact; no normalization was added. Inspection confirmed
that both workers' `model.respond` paths retain the outer `{"text":string}`
envelope and preserve graph text, while the independent proof prompt already
specifies tactic bodies. The verifier's source wrapper is unchanged.

Focused tests assert the contract in frozen worker messages, preserve whitespace,
Unicode and malformed wrappers through graph ingestion, and check the prompt's
valid example with real Lean (also rejecting a leading `by`). Checks passed:
repository-wide `.venv/bin/ruff check .`, scoped Ruff format check,
`.venv/bin/mypy`, 37 coordinator tests across graph instructions, graph response
and context packets, and `go test ./internal/provider` in `worker/`.
Real Lean tests ran with `/home/ben/.elan/bin` added to `PATH`.
No paid model calls or credentials were used. No commit was made in this
implementation session; review and commit remain with the parent workflow.

## 2. One bounded feedback-and-repair cycle

Inspect existing rejected-artifact, critique, replanning and packet paths first.
Reuse those paths where possible; add only missing behavior needed to deliver
the rejected candidate and its Lean diagnostics to a repair assignment. Keep
repair count, model assignments, work and verification bounded. Configure the
trial to permit the cycle within its precomputed dollar ceiling.

Acceptance: an offline scripted scenario with real Lean demonstrates rejection,
feedback delivery, one corrected auxiliary candidate, and final checked use of
that lemma. Exhaustion and repeated-delivery checks establish bounded behavior.

## 3. Paid repeat observation

Use `gpt-6.1-sol`, Responses reasoning, the same consecutive-product theorem and
a fresh $1 projected cap. Review the total worst-case price bound, including
failed calls and retries, before launching. Do not supply reference proofs.
Preserve output, frozen packets, usage, Lean verdicts and actual dependency-use
evidence. Missing usage remains explicit; no automatic extra observations.

Acceptance: report whether decomposition, feedback, repair, checked-lemma handoff
and actual final proof use occurred. Target acceptance is Lean's exact verdict;
a failure or direct proof is recorded honestly rather than treated as composed
collaboration. Update the observation record and commit the stage.
