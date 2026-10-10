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
Parent workflow subsequently committed stage 1 as `4a66283`; stage 2 began from
that clean committed state.

## 2. One bounded feedback-and-repair cycle

Inspect existing rejected-artifact, critique, replanning and packet paths first.
Reuse those paths where possible; add only missing behavior needed to deliver
the rejected candidate and its Lean diagnostics to a repair assignment. Keep
repair count, model assignments, work and verification bounded. Configure the
trial to permit the cycle within its precomputed dollar ceiling.

Acceptance: an offline scripted scenario with real Lean demonstrates rejection,
feedback delivery, one corrected auxiliary candidate, and final checked use of
that lemma. Exhaustion and repeated-delivery checks establish bounded behavior.

Implementation status (2026-10-09): implemented; awaiting separate review and
commit. Fresh inspection found that the frontier already selected a critique for
negative auxiliary evidence, but packet projection discarded proof/diagnostics
and no subsequent repair investigation was admitted. The existing graph response
finding, critique message, frozen packet, investigator lineage, retry/work and
Lean reservation paths now carry the cycle; no new job kind or schema was needed.

Explicit rejection IDs deliver exact submitted proof strings and persisted Lean
diagnostics in `untrusted.rejected_artifacts`. The repair requires a completed
rejection critique of that same artifact and its attributed focus-claim finding,
whose exact text and ID are also retained. Required feedback fails admission if
it cannot fit the bounded categories/prompt; it is never silently truncated or
selected as a checked lemma. Compact feedback instructions preserve the stage-1
tactic-body contract. `graph_limits.retries=1` permits one auxiliary repair;
zero disables it. New artifacts or messages cannot reset that claim's repair
count. Empty critiques do not dispatch an evidence-free repair.

Offline real-Lean evidence uses the existing simpler Init-only
`integration/fixtures/graph-nat-reorder.json` fixture, not a reference proof for
the paid consecutive-product target. The proposed auxiliary is
`(a b c : Nat) : (a + b) + c = a + (c + b)`. Lean rejects the exact candidate
`"\n  exact Nat.add_comm a b\n"` with a type-mismatch diagnostic. The critic's
finding explains that this proves only `a + b = b + a` and recommends reassociation
then inner commutation. The next investigator receives that exact critique and
rejection, submits `rw [Nat.add_assoc, Nat.add_comm b c]`, and Lean accepts it.
The final target proof calls the generated lemma by its actual supplied name;
the committed direct-use receipt names that corrected artifact's declaration.
Rejected proof IDs never appear in the checked manifest.

The successful scripted trace is plan → investigation → critique → repair
investigation → synthesis: exactly five jobs, ten reserved assignments, 20 work
units, and three composed Lean operations (at most six subprocesses). Failed
repair exhaustion, disabled retry, empty critique, duplicated results, frozen
packet restart, exact Unicode/whitespace feedback, wrong-focus selections and
oversized-feedback rejection are covered. Repeated ticks do not buy another
cycle or verification. The live runner now reserves four planning/critique
assignments and permits one frontier retry, keeping its five-job/two-assignments-
per-job ceiling, four Lean operations and 40-second Lean allowance.

Checks passed with the pinned Lean toolchain on `PATH`: 57 tests across frontier,
graph instructions, graph response and offline graph collaboration (including
compiled workers); 37 context-packet and offline OpenAI trial tests;
repository-wide Ruff, scoped Ruff formatting and configured mypy. The initial
combined test command omitted `homelab/src` and failed only to import the trial
module; its corrected command passed. The expected injected-verifier-error test
logs its exception while passing.

Fresh dry-run preflight passed without opening a key or launching a worker:
20 work / model cost 2 allows five jobs and ten attempted calls including
assignment failures/retries. At 24,576 input and 2,048 output tokens per call and
$2.50/$10 per million, the bound is
`10 × (24576 × 2.50 + 2048 × 10) / 1000000 = $0.81920`, below the fresh $1 cap.
No paid calls or credential reads were made; stage-2 provider spend is zero.
For stage 3, use the fresh-$1 command in `integration/README.md` with a new
`--output /tmp/opencode/solvenet-openai-consecutive-product-follow-up-observation.json`
and `--prior-reserved-usd 0`. Preflight now records the repair graph limits.
This stage has not executed that paid observation or made a commit.

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
