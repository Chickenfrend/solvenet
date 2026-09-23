# Predeclared challenge-v1 repair-prompt comparison

The prior [corrected comparison](../challenge-v1-goedel-corrected-2026-09-23/README.md)
found repeated repairs. An audit of its local attempt history found 36 repair
attempts: 27 reported an unknown tactic, and 24 of those repeated a candidate
from an earlier depth unchanged. Examples used tactics unavailable with the
fixture's `Init` imports (`tauto`, `ring_nf`, `aesop`). The local SQLite history
was backed up before the audit at
`~/.local/share/solvenet/backups/solvenet-20260923T232802Z-before-repair-prompt.db`
(SHA-256 `2afbb5806f860f9f4a1c126881189457a18957dc40bed1c27e7c0ba4e43af1ed`).

**Intervention:** Only for Lean diagnostics containing `unknown tactic`, omit
the previous candidate text from the next repair message. Tell the model the
tactic is unavailable with the current imports, to start a fresh proof, and to
favor elementary available tactics (`intro`, `constructor`, `cases`, `exact`,
`apply`, `rw`, `simp`, `induction`). Keep the diagnostics and original proof in
the database. All other repair messages and all independent prompts stay as
before. This tests whether avoiding a verbatim invalid example reduces repeated
repairs; it does not change the proof verifier or fixture set.

**Before seeing new results**, compare 10 pairs with base seeds
`11, 22, 33, 44, 55, 66, 77, 88, 99, 110` in that order. Each pair submits
independent then repair, waiting for all runs and verifications to settle before
submitting the next experiment. Both use the checked-in `challenge-v1` set
(SHA-256 `cd199d3ccd299533a38b14ead2d768b7edf47565f7c6b2d14f63822030d85337`),
`ollama/goedel-prover-v2-8b:q4_k_m`, temperature `0.6`, context `4096`,
`max_output_tokens=256`, generation timeout `120` seconds, and the same worker.
Independent starts three initial chains with effective seeds base, base+1,
base+2 and no repairs. Repair starts one chain with the base seed and allows
two feedback repairs; its jobs inherit that seed. This is a request-budget
comparison, not an equal-token-budget comparison.

Primary descriptive outcome: number of the 12 problems solved by each strategy
at each base seed and across all 10 pairs. Secondary outcomes: initial versus
repair successes by depth; count of repeated repair candidates (compare each
attempt to previous depths in its run), specifically those following unknown-
tactic feedback; completed generations, execution/formatting failures, known
and unknown input/output token use, provider duration and Lean verification
time. Report all 10 pairs, including failures and any missing usage; no
seed-based exclusions or stopping based on outcomes. Preserve JSON summaries,
configuration, model digest and local experiment IDs. Reusing 12 fixtures over
10 seeds does not produce 120 independent problems, and differing realized
tokens limit strategy conclusions.
