# Historical decision: keep independent local jobs for the failure-map trial

**Roadmap update (2026-09-24):** This document records the decision reached
under the trial's original, narrow protocol-change criterion. The project's
subsequent [collaboration direction](../../docs/collaboration-direction.md)
replaces its proposed *next* verified-lemma comparison as a roadmap step. The
trial never evaluated a persistent agent group. Build and observe a
persistent, communicating local agent group with capability-aware delegation.
Keep the trial and its frozen plan as historical evidence; its result still
does not justify adding the particular coarse failure-map field to v1.

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

**Retired follow-up:** The previously proposed isolated verified-lemma handoff
comparison and its pass/fail protocol gate are no longer the next step. The
next milestone is the communicating local agent group described above; its
costs and verified outcomes should still be recorded honestly.
