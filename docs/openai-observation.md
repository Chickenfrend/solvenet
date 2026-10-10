# First paid OpenAI graph observation

On 2026-10-09, the operator authorized `gpt-6.1-sol` with a $1 total
projected-spend cap. The worker used `responses-reasoning`, low reasoning effort,
and a worker-local external key file. No credential is stored in this record.

## Outcome

- One standalone structured-output compatibility call returned `ready`.
  Its actual token usage was not emitted; $0.08 was conservatively reserved.
- The graph trial made two successful calls, with no retries: plan and synthesis.
- The planner published the target and an informal finding with an addition-law
  proof outline. The synthesizer's frozen packet included that finding and it
  returned a shorter proof.
- Real Lean 4.19.0 accepted the exact target:
  `(a b c : Nat) : (a + b) + c = (c + b) + a`.
- No auxiliary artifacts, checked-lemma dependencies, or critique were exercised.
  This is basic planner-to-synthesizer handoff, not evidence of composed lemma
  reuse or collaboration efficiency. The task was easy for this model.

```lean
calc
  (a + b) + c = c + (a + b) := Nat.add_comm (a + b) c
  _ = c + (b + a) := congrArg (fun n => c + n) (Nat.add_comm a b)
  _ = (c + b) + a := (Nat.add_assoc c b a).symm
```

## Budget and evidence

The runner reserved at most ten attempted calls, including retries, each with
24,576 input units and 2,048 output tokens (including reasoning). At conservative
rates of $2.50/M input and $10/M output, the graph ceiling was $0.81920;
including the probe reservation, the total ceiling was $0.89920. Assignment
admission, process deadlines and worker capacities independently bound execution.
Missing usage and malformed output stop expansion.

Reported graph usage was 2,093 input tokens and 363 output tokens. The conservative
graph estimate is $0.0088625; ordinary uncached standard input pricing of $2/M
would yield $0.007816. Neither figure is a provider invoice. Overall actual spend
remains unknown because the probe did not report usage.

Local evidence is `/tmp/opencode/solvenet-o4-openai-observation.json`, with SQLite
and worker-log evidence in the adjacent `.json.data` directory. These are local
operator artifacts, not versioned evidence. The JSON includes frozen packets,
worker results, usage and the Lean verdict.

The opt-in procedure is documented in [integration instructions](../integration/README.md).
The runner and its bounds received separate build-agent review; fourteen offline
trial tests, fake-provider graph integrations with real Lean, Go worker/provider
tests, Ruff and the configured incremental mypy scope passed.

## Consecutive-product trial

Later on 2026-10-09, the operator authorized a fresh $1 projected cap for
`(n : Nat) : 6 ∣ n * (n + 1) * (n + 2)`, again using Sol 6.1 and `Init`.
An offline reference proof established feasibility but was not supplied to the
model. The selectable fixture contains only statement, imports and environment.

The first planner call exhausted its 512-token output allowance and returned
incomplete output. It used 981 input and 512 output tokens, conservatively
estimated at $0.0075725. No Lean checks ran. This motivated a configurable
`graph_limits.response_output_tokens`: the production default remains 512, while
the trial requests 2,048. Synthesis still requests 2,048. Admission and persisted
job allowances agree, and the existing cost bound already reserves this amount.

The second run reserved the prior $0.0075725 and projected at most $0.8192 more,
for a combined ceiling of $0.8267725. It made four calls without retries:

1. The planner proposed `2 ∣ n * (n + 1)` and a polynomial increment identity,
   with target-to-lemma suggestions supporting an induction strategy.
2. The investigator produced a candidate for each lemma.
3. The synthesizer attempted the target after receiving the rejected-artifact
   context. No checked lemma was available for composition.

Both auxiliary candidates and the target were rejected. Both auxiliary proof
strings began with `by`, but the verifier wraps a tactic body beneath `:= by`,
causing a parse failure. Offline diagnostic copies with only the leading `by`
removed and indentation adjusted showed that the polynomial identity verifies;
the even-product candidate still leaves an arithmetic goal unsolved. The original
observed proofs and statuses remain unchanged. The target similarly left a goal
unsolved and included a tactic after its goal was already closed. Lean rejected
the resulting candidate, including its disallowed `sorryAx` dependency.

The second run stopped at `no_useful_frontier`, with three Lean checks and no
verified target, critique, or checked-lemma reuse. Its reported usage was 5,439
input and 2,862 output tokens, estimated conservatively at $0.0422175. Combined
paid usage across both attempts was 6,420 input and 3,374 output tokens, estimated
at **$0.04979**. All counters were known; these estimates are not invoices.

Evidence is preserved in `/tmp/opencode/solvenet-openai-consecutive-product-observation.json`
and `/tmp/opencode/solvenet-openai-consecutive-product-retry-observation.json`,
with adjacent SQLite and worker-log directories. This demonstrates structured
decomposition and attempted lemma work, not successful composed proof reuse.
The next prompt improvement should explicitly specify the artifact tactic-body
format; a later experiment should also provide sufficient critique/repair budget.
