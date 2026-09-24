# Collaboration direction

SolveNet's next milestone is a **local group of persistent logical agents**
working together on a multi-step Lean problem. The current v1 coordinator and
worker implement independent/repair jobs, not agents. Historical comparisons of
short prompts and coarse failure labels remain useful records, but they do not
decide whether to build collaborative agents. In particular, the
[failure-map trial](../experiments/challenge-v1-cross-chain-failure-map-2026-09-24/README.md)
did not test an ongoing group.

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
This requires at least two usable model capabilities to assess heterogeneous
delegation; a one-model prototype can establish the collaboration loop but not
a model-strength efficiency claim. Avoid an elaborate scheduler initially.

Share bounded, attributable findings, proposed lemmas, counterexamples and
verification results rather than dumping whole private transcripts into every
prompt. Mark informal claims as unverified. Lean verification of the precise
target in its pinned environment is the formal success boundary; review the
mathematical meaning of any frontier-problem formalization separately. The
local prototype does not make worker-reported success authoritative or imply
that today's sandbox is safe for hostile public contributors.

Exercise delegation, critique, reassignment and synthesis on a problem that
actually needs intermediate reasoning. Record who requested and performed
each task, which findings informed later work, rejected paths, all model calls
and tokens (including planning and review), Lean checks and time, and verified
outcomes. Use these observations and, when helpful, fair baselines to improve
coordination. Another isolated small-model prompt trial is not a prerequisite
to implementing agents. Public contributor capacity, authentication and
admission of outside problems follow only after the existing security and
problem-admission gates; one contributor is not one agent.
