# Collaboration direction

SolveNet now has a **local group of persistent logical agents** working together
on a multi-step Lean problem alongside the v1 independent/repair job path.
G1–G6 are implemented locally: a claim graph, attributable structured proposals,
composed proof manifests, frozen relevant packets and opt-in bounded frontier
decisions. See the current [G6 demonstration and observation](graph-demonstration.md),
the [frontier guide](graph-frontier.md), and the [group-loop guide](group-loop.md).
The default fixed mode and its
[historical A6 demonstration](../integration/README.md#A6-collaborating-agents--two-workers--pinned-lean)
remain supported alongside independent/repair v1.
Historical comparisons of
short prompts and coarse failure labels remain useful records, but they do not
decide whether to build collaborative agents. In particular, the
[failure-map trial](../experiments/challenge-v1-cross-chain-failure-map-2026-09-24/README.md)
did not test an ongoing group. G6 uses scripted responses and real Lean to check
actual information handoff, checked A → B → target composition, immutable replay
and actual proof use. A capped local `qwen2.5-coder:7b` observation completed:
the planner published no structured graph claims and Lean rejected the target.
This protocol observation does not measure collaboration effectiveness or
efficiency. Next, refine structured collaboration on small real-model runs,
measuring accepted proposals, useful sharing/decomposition and checked reuse
separately from target success and cost.

Start with a small group: a planner assigns distinct approaches or subgoals;
investigators pursue them, exchange bounded questions and findings, and may
request help; a critic checks assumptions and dead ends; a synthesizer uses
useful work to attempt a complete Lean proof. Roles may be combined or changed
as work develops. An agent retains its goal, bounded working state, group
membership and task/message history across model calls and worker changes. The
coordinator owns that state, task relationships, artifact provenance and budget;
workers execute bounded model jobs and keep provider credentials local. Agent
identities, jobs, leased assignments and workers have different lifecycles.

Use a **capability-aware hierarchy** rather than a universal model ranking.
Assign planning, review, specialist investigation and routine bounded tasks
according to observed task-specific performance, availability, context limits
and budget. A capable planner can delegate to cheaper or specialist models,
review their findings, redirect or escalate stuck work and revise the plan.
This requires at least two usable real-model capabilities to assess heterogeneous
delegation; the scripted two-worker demonstration establishes the collaboration
loop and routing behavior; G6 adds graph-driven handoff and composition checks,
not a model-strength efficiency claim. Avoid an
elaborate scheduler initially.

Share bounded, attributable findings, proposed lemmas, counterexamples and
verification results rather than dumping whole private transcripts into every
prompt. Mark informal claims as unverified. Lean verification of the precise
target in its pinned environment is the formal success boundary; review the
mathematical meaning of any frontier-problem formalization separately. The
local prototype does not make worker-reported success authoritative or imply
that today's sandbox is safe for hostile public contributors. Malicious tactic
IO can forge the candidate-writable completion/use receipts in the same Lean
process; public acceptance still requires a separately trusted checker.

The local integration exercises delegation, critique, reassignment and synthesis
on a multi-step fixture. In live runs, record who requested and performed
each task, which findings informed later work, rejected paths, all model calls
and tokens (including planning and review), Lean checks and time, and verified
outcomes. Use these observations and, when helpful, fair baselines to improve
coordination. Another isolated small-model prompt trial is not a prerequisite
to implementing agents. Public contributor capacity, authentication and
admission of outside problems follow only after the existing security and
problem-admission gates; one contributor is not one agent.
