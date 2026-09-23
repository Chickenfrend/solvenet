# Challenge-v1 unknown-tactic repair feedback comparison

This is the completed run of the [predeclared plan](PLAN.md), using coordinator commit `af0181f64f61c0193e3f6601a55c71cad8e9fd40` and worker binary SHA-256 `3855f11b0b39a4f1fbdb3b95a37f6966cb688d60b1ea6bd595aaba640defd17e`. It compares three independent initial chains with one initial chain plus up to two repairs. Only feedback following an `unknown tactic` diagnostic changed: the repair prompt withheld the rejected proof and requested a fresh proof using available elementary tactics. The independent prompt and Lean verifier did not change.

Configuration: `problems/challenge-v1.json` SHA-256 `cd199d3ccd299533a38b14ead2d768b7edf47565f7c6b2d14f63822030d85337` (12 fixtures), local Lean 4.19.0, `ollama/goedel-prover-v2-8b:q4_k_m` (reported digest `sha256:98d095df8e1d58c088000c8a97fe5d4bc28b97fad4ef77d7a794be30544299d1`), temperature 0.6, context 4096, 256 output tokens per job, 120-second generation timeout and up to three assignments per job. Independent initial chains used seeds `base`, `base+1`, `base+2`; the repair chain and its repairs used `base`. One local worker served the experiments sequentially, independent then repair at each of the ten predeclared seeds. The coordinator used the existing local `solvenet.db`, the restricted local Lean verifier, and a successful `/ready` check before submission. Every experiment settled with 12 terminal runs, resolved verifications and zero active assignments before the next submission.

| Base seed | Independent solved / 12 | Repair solved / 12 (initial + repair) | Independent attempts / format failures | Repair attempts / format failures | Input tokens independent / repair | Output tokens independent / repair |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 11 | [7](independent-seed-11.json) | [6](repair-seed-11.json) (5 + 1) | 28 / 3 | 22 / 1 | 3,701 / 3,882 | 1,912 / 1,172 |
| 22 | [6](independent-seed-22.json) | [6](repair-seed-22.json) (5 + 1) | 31 / 0 | 23 / 1 | 3,690 / 4,582 | 1,422 / 1,129 |
| 33 | [8](independent-seed-33.json) | [8](repair-seed-33.json) (5 + 3) | 30 / 1 | 22 / 1 | 3,704 / 4,486 | 1,470 / 1,344 |
| 44 | [6](independent-seed-44.json) | [6](repair-seed-44.json) (5 + 1) | 30 / 1 | 22 / 1 | 3,709 / 4,567 | 1,563 / 1,185 |
| 55 | [7](independent-seed-55.json) | [4](repair-seed-55.json) (4 + 0) | 32 / 0 | 28 / 0 | 3,826 / 5,249 | 1,262 / 1,023 |
| 66 | [7](independent-seed-66.json) | [6](repair-seed-66.json) (6 + 0) | 30 / 0 | 24 / 0 | 3,579 / 4,334 | 1,162 / 817 |
| 77 | [9](independent-seed-77.json) | [8](repair-seed-77.json) (6 + 2) | 29 / 1 | 18 / 2 | 3,577 / 3,715 | 1,394 / 1,300 |
| 88 | [7](independent-seed-88.json) | [5](repair-seed-88.json) (4 + 1) | 31 / 1 | 22 / 2 | 3,835 / 4,452 | 1,297 / 1,417 |
| 99 | [7](independent-seed-99.json) | [6](repair-seed-99.json) (6 + 0) | 29 / 1 | 24 / 0 | 3,579 / 4,320 | 1,507 / 855 |
| 110 | [7](independent-seed-110.json) | [5](repair-seed-110.json) (4 + 1) | 27 / 5 | 25 / 1 | 3,808 / 4,680 | 2,245 / 1,179 |
| **Total** | **71 / 120** | **60 / 120 (50 + 10)** | **297 / 13** | **230 / 9** | **37,008 / 44,267** | **15,234 / 11,421** |

Independent solved more run instances in seven pairs and tied in three; repair never solved more within a pair. Repair had verified attempts at depths 0/1/2 of **50/9/1**; independent had verified proofs only at depth 0. The linked snapshots contain complete per-depth verification and request counts, settings, local experiment IDs, and timings. There were no provider or other failures, expired or rejected assignments, or missing input/output usage. Independent had 310 assignments with reported usage; repair had 239. Provider-reported total generation duration was **304.8 / 268.0 seconds** (independent / repair, zero unknown), and summed Lean verification time was **384.3 / 296.6 seconds** (zero unknown). These sums are not end-to-end wall-clock duration.

## Repeated repairs

An exact repeat means that a repair candidate string matches an ancestor candidate in the **same run**, following the stored parent-attempt links. Across all ten seeds, **56 of 113** repair candidates repeated an ancestor; **28 of 71** repairs following an unknown-tactic diagnostic did so. Another **58 of 113** repair candidates themselves produced an unknown-tactic diagnostic. Thus the intervention reduced some anchoring but did not eliminate it.

For the three seeds also in the [earlier corrected comparison](../challenge-v1-goedel-corrected-2026-09-23/README.md), the new repair runs repeated **15/32** candidates, including **8/21** following unknown-tactic feedback. The previous prompt repeated **29/36**, including **24/28** following unknown-tactic feedback. Repair solved **20/36** instances in the new three-seed subset versus **18/36** previously. These are separate model generations, not an isolated randomized prompt-effect estimate. The new independent results for seeds 11/22/33 matched the old solved totals (7/6/8), and their first candidates matched the new repair arm on 11/11, 12/12, and 12/12 problems with candidates in both arms; corresponding matches for seeds 44 through 110 were 11/11, 12/12, 12/12, 12/12, 11/11, 12/12, and 12/12.

## Interpretation and record

This is a **request-limited, not equal-token**, comparison: independent used fewer input tokens but more output tokens (52,242 combined versus repair's 55,688). Early success and formatting failures affect actual requests. The same 12 fixtures were reused across ten seeds, so 120 run instances per arm are not 120 independent problems. The results characterize this particular local model, prompt and strategy implementation, not a general limit on collaboration or repair. Given this result, further prompt experiments are lower priority than building the private homelab interface.

All 20 settled coordinator summaries were saved as linked JSON files. Their run and assignment IDs refer to detailed local history; **the snapshots contain no raw candidate proofs, diagnostics or model responses**. Those remain in the untracked `solvenet.db`. The pre-run integrity-checked backup path and hash are in the [plan](PLAN.md). The database was checked again after the run (`integrity_check=ok`, no foreign-key violations), and every saved generation reported the requested seed, temperature, context and output cap as well as the model digest above. Independent job seeds and inherited repair seeds were checked against the database.
