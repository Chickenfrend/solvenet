# Corrected challenge-v1 paired Goedel-Prover experiment

These are **new** sequential runs on 2026-09-23, using coordinator/worker from commit `9b0e4435d6f735adc869a8f8f6b84220edd92dd9` (worker binary SHA-256 `8ad8ae5b3f3ed96a312f41add657548d24f4f8f5f7b0c2f88ff387886df15c29`). The [previous report](../challenge-v1-goedel-2026-09-23/README.md) is **invalid as a comparison** because its independent initial jobs reused the same seed; that historical record is retained unchanged. This corrected run assigns independent initial jobs seeds `base`, `base+1`, `base+2`, while the repair arm uses `base` initially and on repairs.

Configuration: `problems/challenge-v1.json` SHA-256 `cd199d3ccd299533a38b14ead2d768b7edf47565f7c6b2d14f63822030d85337`, set `challenge` version 1 (12 fixtures), environment `leanprover/lean4:v4.19.0` (local Lean 4.19.0), model `ollama/goedel-prover-v2-8b:q4_k_m`, Ollama model digest `sha256:98d095df8e1d58c088000c8a97fe5d4bc28b97fad4ef77d7a794be30544299d1`, temperature 0.6, context 4096, output limit 256 tokens per job, generation timeout 120 s, at most 3 assignments per job. Independent: 3 initial jobs, 0 repairs; repair: 1 initial job, up to 2 repairs. Coordinator used existing `solvenet.db`, local verifier with the pre-existing restricted `/tmp/opencode/solvenet-lean-bin/lake` bwrap wrapper; `/ready` returned HTTP 200 before submission. One continuously polling worker served all six experiments. Each experiment settled with 12 terminal runs, resolved attempt verifications and zero active assignments before submitting the next.

| Base seed | Strategy | Experiment ID | Solved / 12 | Completed attempts | Format / provider / other failures | Input tokens | Output tokens | Verified attempts depth 0 / 1 / 2 |
| ---: | --- | --- | ---: | ---: | --- | ---: | ---: | --- |
| 11 | [independent](independent-seed-11.json) | `9f751524f036489b922afdb100d7f6ec` | 7 | 28 | 3 / 0 / 0 | 3701 (0 unknown) | 1912 (0 unknown) | 10 / 0 / 0 |
| 11 | [repair](repair-seed-11.json) | `9281978d47ca45a58304341d724e705d` | 5 | 23 | 1 / 0 / 0 | 4396 (0 unknown) | 1269 (0 unknown) | 5 / 0 / 0 |
| 22 | [independent](independent-seed-22.json) | `dcc85c146b7f437ba470172d93acc04b` | 6 | 31 | 0 / 0 / 0 | 3690 (0 unknown) | 1422 (0 unknown) | 7 / 0 / 0 |
| 22 | [repair](repair-seed-22.json) | `4fb8cabe01f34997bed535102f468d8d` | 6 | 24 | 1 / 0 / 0 | 4823 (0 unknown) | 1249 (0 unknown) | 5 / 0 / 1 |
| 33 | [independent](independent-seed-33.json) | `300c0c10fb1649579d9981c903b84740` | 8 | 30 | 1 / 0 / 0 | 3704 (0 unknown) | 1470 (0 unknown) | 10 / 0 / 0 |
| 33 | [repair](repair-seed-33.json) | `d4db7b1ffa9049bbae0d6e756b371c37` | 7 | 24 | 0 / 0 / 0 | 4897 (0 unknown) | 1303 (0 unknown) | 5 / 2 / 0 |

Across the three paired seeds:
- **independent:** 21/36 solved run instances; 11095 input and 4804 output tokens; 89 completed attempts, 4 formatting failures.
- **repair:** 18/36 solved run instances; 14116 input and 3821 output tokens; 71 completed attempts, 2 formatting failures.

Paired solved-problem differences (repair relative to independent):
- Seed 11: repair-only none; independent-only bool-xor-assoc, nat-add-succ-swap.
- Seed 22: repair-only nat-add-succ-swap; independent-only and-or-distribute.
- Seed 33: repair-only list-map-append; independent-only bool-xor-assoc, contrapositive-iff.

Candidate diversity counts compare exact candidate strings **within each problem**, across all completed candidate attempts (including repairs). Format failures have no candidate. `Duplicate` means at least two attempt strings match; `all unique` means none match.

| Base seed | Strategy | Problems with ≥2 attempts | With duplicates | All unique |
| ---: | --- | ---: | ---: | ---: |
| 11 | independent | 10 | 2 | 8 |
| 11 | repair | 6 | 6 | 0 |
| 22 | independent | 12 | 0 | 12 |
| 22 | repair | 6 | 6 | 0 |
| 33 | independent | 12 | 2 | 10 |
| 33 | repair | 7 | 5 | 2 |

Duplicate-candidate problems: seed 11 independent `exists-and-distribute`, `forall-imp-compose`; seed 22 independent none; seed 33 independent `nat-add-cancel`, `nat-add-succ-swap`. In repair, seed 11 had duplicates on all six multi-attempt problems, seed 22 on all six, and seed 33 on five of seven (`and-or-distribute`, `bool-xor-assoc`, `contrapositive-iff`, `exists-and-distribute`, `nat-add-succ-swap`). Distinct-candidate counts for each individual multi-attempt problem were checked against the database.

First-candidate pairing: for each seed the earliest initial independent job and the repair initial job were both generated with the same base seed and prompt. Exact candidate matches where both produced a candidate:
- Seed 11: 11/11 paired problems matched.
- Seed 22: 12/12 paired problems matched.
- Seed 33: 12/12 paired problems matched.

Seed 11 had one problem without a candidate in at least one first job. All runs ended solved or exhausted; no provider/other failures, expired/rejected/active assignments, unknown token counts, or pending verifications remained. Repair found one verified attempt at depth 2 for seed 22 and none at depth 2 for the other pairs; the per-depth request, failure, and verification details remain available in each linked JSON snapshot.

Persisted job and attempt metadata were checked across all six runs: independent initial job seeds exactly `base, base+1, base+2` in order for every problem, repair initial seed `base`; all repair jobs retained the base seed. Every recorded generation matched the job seed, temperature 0.6, requested model, exact reported digest, context 4096 and output limit 256. The linked JSON files are settled coordinator summaries; they contain IDs and statuses, **no raw proofs or model responses**. Full candidate strings and diagnostics remain only in the local database.

Interpretation: only three base seeds, one model and 12 repeatedly used fixtures; 36 runs per arm are not 36 independent problems. Different realized input/output token totals mean equal per-job generation limits do not establish equal token budgets. Failed formatting and early proof success affect the number of actual generations. The verified-attempt count can exceed solved runs when multiple independent jobs verify. These data describe an exploratory comparison, not a statistically established advantage.
