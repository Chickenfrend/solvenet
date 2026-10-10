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

## Bounded consecutive-product follow-up

On 2026-10-09, after commits `4a66283` (artifact tactic-body contract) and
`e845e3e` (bounded rejection feedback and repair), exactly one further observation
ran on the same consecutive-product fixture. It used `gpt-6.1-sol`,
`responses-reasoning`, low reasoning effort, `Init` and pinned Lean 4.19.0.
No reference proof was supplied. The external credential was read only by the
compiled Go worker; Python passed its file path in the worker environment.
The binary's recorded build revision was `16d7f25`; its worker source was unchanged
through the two implementation commits.

The no-call preflight confirmed a fresh $1 allowance, prior reservation zero,
and a **$0.81920** worst-case projection at $2.50/M input and $10/M output.
It reserved five jobs / ten attempted assignments (including failures/retries),
20 work units, 24,576 input and 2,048 output tokens per attempt. The graph allowed
four planning/critique assignments, one auxiliary repair, four Lean operations
and 40 seconds Lean time. Per-call, group and process deadlines remained 45,
285 and 300 seconds. No compatibility probe or additional observation ran.

### Observed handoff and verdicts

1. The planner proposed an even-product lemma and the successor-product increment
   identity, with target-to-lemma suggestions. Only the identity received a
   separate investigation within this budget.
2. The first identity artifact was the exact string
   `"rw [Nat.mul_add]\n  ac_rfl"`. Lean rejected its inconsistent indentation
   with `expected end of input` and a missing candidate constant. The model
   followed the no-leading-`by` rule but still produced malformed layout.
3. The critic's frozen packet contained that exact rejected artifact and its
   persisted diagnostics. Its attributed critique recommended equally indented
   tactic lines; the repair investigator received both the same rejection and
   the exact critique.
4. The repaired artifact `"  rw [Nat.mul_add]\n  ac_rfl"` verified. Its proof ID
   is `abc64cd6bb5646538ebf08c3da2baf6d`. The synthesizer's frozen packet and
   composed manifest selected this artifact alone, under the supplied name
   `SolveNetLemma_0dad0a4c68a87c3fcf79f485`; the rejected artifact was excluded.
5. The final candidate explicitly called that declaration in a rewrite and
   attempted its own local even-product proof. **Lean rejected the target** with
   `no goals to be solved` and `Candidate uses disallowed axiom: sorryAx`.
   The committed target composed-check receipt records `usage_unknown`, with
   no direct/type/transitive dependency-use evidence. Naming the supplied lemma
   in rejected source establishes attempted use, not checked final proof use.

This demonstrates real-model decomposition, exact feedback delivery, a successful
bounded auxiliary repair and checked-lemma handoff. It does not demonstrate a
verified composed target proof, hierarchy benefits or collaboration efficiency.
The graph stopped at `capacity_or_model_budget` with zero work remaining; the
runner recorded `observation_complete` and the target run was `exhausted`.
No final-target retry or separate even-product investigation was dispatched.

### Cost and durable evidence

Actual execution made five completed provider calls / five leases, zero assignment
retries and zero provider failures. Four graph-response ingestion receipts were
accepted; formal verification separately rejected one auxiliary, verified its
repair and rejected the target. Three Lean operations used six subprocesses and
5,093 ms total (1,519 + 1,545 + 2,029 ms). Ten assignments and all 20 work units
were reserved, rather than ten calls being executed.

Reported usage is 6,727 input and 2,133 output tokens, with zero unknown provider
usage entries. At the authorized conservative rates the estimate is
**$0.0381475**; this is not a provider invoice. Dependency-use evidence for the
rejected target remains unknown independently of the known provider token usage.
The JSON's generic `Probe usage unknown` limitation and null prior-probe field
are inherited labels: this observation had no probe and zero prior reservation.

Evidence is `/tmp/opencode/solvenet-openai-consecutive-product-follow-up-observation.json`
and its adjacent `.json.data` directory. Read-only inspection of SQLite confirmed
all three committed composed checks, exact rejection/critique delivery, selected
declarations, use receipts and five frozen packets. All five packet hashes match
the frontier decision records. These remain local operator artifacts.
