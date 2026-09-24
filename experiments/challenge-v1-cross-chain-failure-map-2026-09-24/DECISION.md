# Decision: keep independent local jobs

**Decision (2026-09-24):** Retain protocol v1's independent initial jobs and
per-chain repairs. Do not add a cross-chain-findings field on the strength of
this experiment. A run requests initial search chains, a job requests one model
generation, an assignment leases that job to a worker daemon, and a completed
candidate attempt is verified by Lean. None of these is a first-class logical
agent; chain counts and worker assignments do not establish separate agents or
justify remote allocation or new scheduling state. See
[`docs/ticket-18-terminology.md`](../../docs/ticket-18-terminology.md) and
[`protocol/v1.md`](../../protocol/v1.md).

**Basis and limits:** The frozen [`PLAN.md`](PLAN.md) compared a third independent
request with a third request receiving only coarse failure labels from two
unsuccessful chains. The completed, redacted [`RESULTS.json`](RESULTS.json)
reports 60 paired problem/seed blocks on 12 repeated theorems: 30 solved in
each arm, with one exclusive success per arm. The incremental third-chain
output-token difference was 22.33% (above the predeclared 15% limit), and Lean
checks differed by two (limit one), so solved counts are descriptive rather
than evidence of an efficiency advantage. The predeclared protocol-step gate
also failed: zero net map-only successes, advantage in one seed, and one theorem
helped. This is evidence about this coarse failure map with one local model and
repeated fixtures, **not** a finding that collaboration in general fails.

**One next local test that could reopen the decision:** Separately freeze a plan
*before any model calls* for a verified shared-lemma handoff on a fixed set of
multi-step Lean theorems, with fixed fixture IDs, seeds, prompts, model/version,
Lean environment, verification and stopping rules. In each paired block, the
handoff arm asks for a useful auxiliary lemma, verifies that lemma in Lean in
the same environment, and gives only its verified statement/proof to a fresh
final-proof chain; an unverified lemma is never shared or scored as progress.
The control arm uses independent whole-theorem chains under the same declared
request/output caps and stopping policy. Lean must verify the complete target
proof in both arms. This tests reusable *verified content*, rather than a map
of failed approaches; it does not require a protocol change to run locally.

Predeclare a **total** per-arm budget and report actual calls (including failed
ones), input and output tokens, provider time, all lemma and target Lean checks,
and Lean time; charge lemma generation and verification to the handoff arm and
do not compare only the final-proof call. The separate plan should fix the
number of task/seed pairs and require at least four more handoff-only than
control-only verified targets across at least two distinct theorems and three
seeds, with total input-token, output-token and Lean-time differences each
within 15%, no unknown token usage or provider failures, and actual Lean-check
counts differing by at most one. Report per-task
and per-seed pairs, failures and unknown usage; no post hoc subset selection or
free lemma preparation. Only a cost-comparable, predeclared positive local result
would warrant considering a bounded local lemma-handoff mechanism, followed by
separately predeclared replication before a general collaboration claim. It
would not by itself establish distinct agent identities or a reason to allocate
work remotely.
