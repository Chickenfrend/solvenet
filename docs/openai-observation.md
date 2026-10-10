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
