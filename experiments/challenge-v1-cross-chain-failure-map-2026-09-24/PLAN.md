# Predeclared local cross-chain failure-map experiment

**Status: frozen before any model calls for this experiment.** Implement the
small local runner described below after freezing this file; do not select
seeds, prompts, classifiers, exclusions, or stopping rules from its outcomes.
The question is whether information from *two separate, unsuccessful proof
chains* helps a new chain more than an independent third attempt at comparable
total generation and Lean-verification cost. This is not a same-chain repair:
the third chain receives neither predecessor proof, raw Lean diagnostic, nor
an earlier chain's final answer. The information is a coarse, deterministic
map of approaches already tried and their failure classes.

## Fixed materials and design

- Use all 12 problems, in file order, in `problems/challenge-v1.json` (set
  `challenge`, version 1, SHA-256
  `cd199d3ccd299533a38b14ead2d768b7edf47565f7c6b2d14f63822030d85337`),
  with only the public statement and `Init` import in model messages. Never
  read or send `reference_proof` to the model or use it to construct the map.
- Use one local `ollama/goedel-prover-v2-8b:q4_k_m` (expected digest from the
  previous runs:
  `sha256:98d095df8e1d58c088000c8a97fe5d4bc28b97fad4ef77d7a794be30544299d1`),
  temperature 0.6, `num_ctx=4096`, `num_predict=256`, 120-second request
  timeout, JSON schema with exactly one string `proof` field. Use the *exact*
  system instructions, imports/theorem user message, and `extractProof` rules
  in `worker/internal/provider/ollama.go` at commit
  `7dbe2aa20d0fdd6ba8d73cbf8dbd2b40d2be5a0b` (file SHA-256
  `8893049b0c2dccb797178c828fc12e40dc1bde9e0c8ab8ddad401184e7fcdcb5`);
  record worker/runner hash before first call. If this pinned prompt or
  extractor cannot be reproduced, freeze a replacement plan before running.
  No conversation history between calls. Use the existing local restricted
  `LeanVerifier` with `leanprover/lean4:v4.19.0`, 10-second verification timeout
  and its existing axiom/unsafe checks. Record the Lean version, wrapper and
  hashes/configuration and require verifier readiness and the expected model
  digest before starting.
- Five paired blocks, base seeds **121, 132, 143, 154, 165**, in this order.
  For each problem/block request two initial proofs with seeds `base` and
  `base+1`, then verify each successfully extracted proof independently (even
  if the other verifies). These are the *identical shared prefix* for both
  arms, recorded once and attributed at full cost to **each** arm. If either
  verifies, both arms score solved and make no third request. Otherwise make
  exactly one fresh proof request with seed `base+2` per arm, independently
  verify each extracted result. Execute baseline third and collaborative third
  sequentially, alternating their order by block (baseline first in blocks
  1, 3, 5); use one worker/model instance and no concurrent requests. Even a
  formatting failure, request failure, or Lean timeout in the prefix counts as
  an unsuccessful chain and does not remove the block.
- Both third requests append **one user message** after the same initial
  messages. They use the identical template below; only the four placeholders
  differ. Collaborative placeholders carry the actual labels from *both*
  prefix outcomes. Baseline placeholders all contain `withheld`, revealing
  no outcomes but receiving the same fresh-approach instruction. The text is:

  ```text
  Two independent attempts on this theorem did not verify. Their coarse outcomes were:
  chain 1: <approach-1> / <outcome-1>
  chain 2: <approach-2> / <outcome-2>
  Choose a fresh approach. Return a complete proof of the original theorem.
  ```

  Collaborative placeholders use the classifiers below, in seed order. No additional
  examples, suggestions, proof snippets, generated prose, or feedback are
  allowed. The third request is a **new chain**, not parented to either
  attempt. If either prefix proof verified, no map is constructed or sent.

### Fixed map construction (no model call)

For each initial attempt, inspect only its extracted candidate and verifier
  status/diagnostics. For `approach`, skip blank lines and whole lines whose
trimmed content begins with `--`. On the first remaining line, remove optional
leading whitespace and one bullet `·`, `-`, or `+` **only when followed by
whitespace**. A bare bullet is `other`, and block comments are not parsed:
if first, they yield `other`. Recognize a leading tactic token only if followed
by whitespace or end of line (so `introduction` is not `intro`): `intro`, `intros`, `constructor`, `cases`,
`induction`, `rw`, `rewrite`, `simp`, `simpa`, `apply`, `exact`, `rfl`, or `decide`.
Map `intros→intro`, `rewrite→rw`, `simpa→simp`; otherwise report the recognized
token unchanged. Ignore later lines; if the first tactic line has no recognized
leading token, report `other`. If extraction failed or no model text exists,
report `none`. This is a **label**, never the candidate text.

`outcome` is `format` if no candidate was extracted, `request_failure` if
generation did not complete, `timeout` for Lean verification timeout,
`verifier_error` for verifier error, else for rejected proofs classify the
diagnostic *case-insensitively*, first matching category in this priority:
`unknown_tactic` (`unknown tactic`), `unsolved_goals` (`unsolved goals`),
`type_mismatch` (`type mismatch`), `unknown_identifier` (`unknown identifier`),
then `other_rejection`. These are the only permitted values. If a request
fails, use `none / request_failure` (request failure precedes format). Never
pass raw diagnostics, full proofs, last tactic lines, theorem-specific names,
or a verified proof into the collaborative message. Log the two map labels and
source attempt IDs, and assert the message matches this template and allowlist
before sending. The producer cost is **both** initial generations and Lean
checks; the deterministic map construction requires no model request.
Before any model calls, implementation checks must fix at least these examples:
`-- note\n · exact True.intro` → `exact`, `introduction` → `other`,
`·` → `other`, `/- note -/\nrfl` → `other`, and `rw [Nat.add_zero]` → `rw`.

## Execution, accounting and decision rules

Implement a minimal experiment-local runner using the existing Ollama request
format/proof extractor and `LeanVerifier` (or thin wrappers around them), not
new coordinator scheduling or a new fixture set. A coordinator experiment
submission alone cannot insert the conditional cross-chain message, so do not
mislabel an existing `repair` job as this intervention. Save per-block/problem
request payload hashes, seeds, complete responses/candidates and Lean outcomes
locally; publish only redacted aggregate results and IDs/configuration, never
the fixture reference proofs. Assert same first two payloads/results are used
in both arms, distinct third messages, identical third model settings and no
third call after prefix success. Count an actual provider call once per arm
when attributing the shared prefix, even if physically executed only once.
Count retries/timeouts as requests, not as free replacements. Do not retry a
failed generation, formatting error, or Lean timeout. Abort and report partial
data (no success comparison) if model digest, fixture hash, prompt/extractor,
verifier configuration, or Lean version changes mid-run, or readiness is lost.
Finish every one of the 5 × 12 paired blocks; do not stop for a promising
result or drop a difficult fixture. Freeze and publish the runner hash and
implementation checks before the first model call; if a bug is found after
calls start, preserve partial outputs, document it and freeze a new plan before
rerunning rather than silently modifying this plan.

**Primary outcome:** paired difference in number of distinct fixtures with a
verified proof per arm at each base seed and in all 60 paired problem/seed
blocks; also show the subset requiring a third request separately. Report
per-fixture paired outcomes so 60 observations are not treated as 60
independent theorems. Lean verification alone decides success; a malformed
JSON answer or verifier error is never a success.

**Cost and diagnostics:** report per-arm actual provider calls (including
failures), separately reported input and output token totals and unknown
counts, provider duration, extracted proofs, Lean checks and total Lean elapsed
milliseconds, timeout/error/format counts and third-chain success counts.
Report common prefix and incremental third-chain costs separately. The
maximum charged per arm is three requests and three Lean checks per block;
both arms have the same output cap and conditional stopping, but the map
increases collaborative input tokens and malformed outputs can change the
number of actual checks. Treat missing token usage as unknown, not zero.
Calculate both arms' absolute input-token, output-token and Lean-time
differences divided by the *larger* of the two totals for (a) all 60 charged
blocks and (b) **incremental third requests/checks alone**, excluding the
common prefix. Report both sets of three percentages; all six govern the
threshold. **Call the comparison cost-comparable only if all six relative
differences are ≤15%, no unknown token usage on any charged call or provider
failures,
and the number of actual Lean checks differs by at most one across arms.**
Otherwise report solved counts and resource differences as descriptive only,
not as evidence of an efficiency advantage. Include a per-block cumulative
solved-versus-(requests, input tokens, output tokens, Lean milliseconds) table;
do not retroactively select a matching subset or reweight results by success.

Evidence supporting a *next, small architecture step* (an explicit bounded
cross-chain-findings field in a local job protocol) requires the comparison to
meet the cost criterion above, at least **four more collaborative-only than
baseline-only** successes across the 60 pairs, an advantage in at least three
of the five seeds, and at least two distinct theorem IDs helped. This only
supports the next local experiment for this model, fixture set and failure
map. Otherwise retain the simple independent jobs and refine/replicate the
hypothesis before adding protocol state. These are decision thresholds, not a significance
test. A negative or inconclusive result is informative about this very coarse
failure map, not all collaboration. The shared prefix deliberately halves the
physical work in the first two positions but must be charged to both arms;
the tested intervention only acts when both attempts fail. Repeated fixtures,
one local model, modest seed count, potentially correlated seeded outputs,
imperfect tactic labels, and the extra map prompt limit generalization. Even
a positive result needs a separately predeclared replication before claiming
a general collaborative-search advantage.
