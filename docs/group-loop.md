# Local coordinator-owned group loop (A1–A6)

A1–A6 are implemented locally. The A6 process-level test exercises the group
with two Go workers, scripted fake model responses and real pinned Lean; it
validates the loop and trust boundary, not actual model efficiency. An optional
live-model operator run remains the next empirical follow-up (see the
[integration guide](../integration/README.md#A6-collaborating-agents--two-workers--pinned-lean)).

Use `Store.start_group_loop(request_key, statement, imports, environment, models,
max_work=12, deadline=None)` to start an idempotent local group. `models` must
explicitly supply `planner`, `investigator`, `critic` and `synthesizer` model IDs;
all four may be the same model. The returned group ID can be inspected with
`Store.group(id)` and `Store.group_loop(id)` (phase, reason, configured models,
deadline). The deadline defaults to one hour and may be set to a finite time
within the next hour; a group without a compatible worker stops when it expires.
The coordinator scheduler calls `advance_groups()` between Lean
checks; `advance_group(id)` is also available for deterministic local driving.
The same local coordinator exposes `POST /v1/groups` with the above fields
(including optional `model_capabilities`) and
`GET /v1/groups/{id}` for the combined group/loop snapshot and terminal reason.
For a process-level demonstration using two compiled Go workers, scripted
Ollama responses and the pinned Lean verifier, see
[`integration/README.md`](../integration/README.md#A6-collaborating-agents--two-workers--pinned-lean).

The planner returns a bounded JSON `{"approaches": ["subgoal 1", "subgoal 2"]}`
in a `model.respond` plan result. Two persistent investigators receive separate
scoped tasks and return findings. The critic receives both findings and returns
`{"decisions": ["accept", "redirect"]}` (either order); the other investigator
revisits the redirected branch with the accepted finding. Finally the
synthesizer receives bounded, explicitly **unverified** findings and a
`model.generate` job for the original theorem. Only the existing Lean verifier
can solve the run. Malformed plan/review results, exhausted retries, rejected
proof, work limits, deadline and verifier errors yield explicit group reasons.

Each transition and its next task/job are committed together before dispatch.
Each of the six jobs reserves two possible worker leases (12 work units total),
so a lost lease retries the *same* job and agent without adding a new task.
The v1 run can temporarily become `exhausted` between group calls; insertion of
the next group job reactivates it. After the final proof check, group phase and
reason become terminal. A target proof already awaiting Lean may verify after
the deadline; Lean's accepted result atomically changes the terminal reason to
`verified_target`. Independent and repair runs use their existing path.
Provider credentials and execution remain exclusively in workers. Group state
is coordinator-private SQLite state, not a worker-side agent conversation.

## Formal artifacts (A4)

An investigator may return a bounded finding containing JSON
`{"artifact":{"statement":": True","imports":["Init"],"environment":"lean-test","proof":"trivial"}}`.
The coordinator stores the proposal with its group agent, task and completed
job provenance. Its statement, imports, environment and proof are immutable;
the worker's `status` field (if supplied) has no authority. Other findings
remain informal, explicitly unverified text. `Store.group(id)['artifacts']`
shows each proposal's verification status and bounded diagnostics. Local
coordinator-authored proposals may omit a job; these have `source=coordinator`
and retain the owning agent/task as context rather than claiming a worker job.
Job proposals must match the completed finding from that task's owner.

The coordinator checks the exact proposed claim with Lean. An artifact for a
different declared environment is incompatible. If its imports differ from the
target imports, Lean checks it again under the target imports. Only successful
checks are labeled verified in the synthesizer's bounded context; rejected or
incompatible proposals remain unverified findings. A lemma never changes the
group run to solved: the assembled target proof still passes through the
ordinary Lean verification path. Environment identity is configured locally
and is not attested by workers. A verified artifact also records its actual
verifier identity: the local Lean command/version and project inputs, or the
resolved Docker image ID. Restarting with a changed or unavailable identity
invalidates the old verification; the coordinator rechecks before sharing it
as verified context. Timeout and verifier errors are terminal artifact
outcomes, allowing synthesis to continue without claiming verification.
If identity is temporarily unavailable, artifact checks stay pending and
previously verified claims are withheld until identity returns and Lean
rechecks them. A check uses one captured verifier identity for its original
imports and target-import replay; a binding change prevents a stale result
from being recorded. Local Lean identity is checked again after verification
and before synthesis consumes verified context. The project and its dependency
files remain locally trusted; this is not an atomic snapshot against hostile
concurrent filesystem mutation.
Prompts carry informal findings as bounded JSON-quoted `unverified` data,
separate from the JSON list of Lean-verified auxiliary claims.
Artifact checks execute the resolved Docker image ID even if its configured
tag changes between inspection and execution; ordinary proof verification
continues to use the configured tag. Local Lean identity includes Lake package
sources and compiled dependency outputs; it checks file metadata on each
artifact-related tick without rereading all dependency contents each time. It
also fingerprints the actual Lean executable selected by `lake env lean`, not
only the Lake launcher and reported Lean version.

Artifact `status` in a group snapshot is the historical check outcome.
`current_status` is `verified` only if the check's verifier identity matches
the identity observed for this inspection; otherwise historical verifications
show `needs_recheck`. Direct `Store.group(id)` calls without a fresh observed
identity conservatively show `needs_recheck`. The group inspection endpoint
fingerprints on demand for verified artifacts, while archived/stopped groups
do not trigger identity scans on each scheduler tick.

## Local routing and cost (A5)

`start_group_loop` also accepts `model_capabilities={model_id: {"tasks":
["finding", "plan", "critique", "proof"], "context_bytes": 8192, "cost": 1}}`.
Each role in `models` can be a model ID or an ordered list of IDs. Configure
IDs supported by local workers; the coordinator learns recent availability
through worker claims/heartbeats (`ready` or `unobserved`); non-proof routing
also requires the worker's declared `model.respond` capability. A recent
proof-only worker is explicitly ineligible for non-proof work. With explicit
capabilities an unseen model is ineligible; stale or unavailable workers are
ineligible. Without capabilities, the original single-model setup can enqueue
for an unseen worker until the deadline. Capabilities are operator configuration,
not provider credentials or a universal strength ranking. `tasks` limits task
fit, `context_bytes` limits target plus prompt bytes, and `cost` reserves
2 × cost work units (two possible leases). Cheaper eligible investigators are
preferred; role preference, observed failed jobs, reviewer-accepted findings,
Lean-verified target proofs and availability inform the
deterministic choice. A failed investigation gets one scoped follow-up by the
**same agent**, preferring a different eligible model. If no model fits or
budget cannot cover it, the group stops with an explicit reason. One-model
decisions are labeled `single_model_fallback_not_hierarchy_evidence`.
Completed calls are displayed separately as throughput, never counted as
proof quality; unverified or invalid outputs do not become successes.

`group(id)['routing']` persists each dispatch explanation (including refusals),
with candidates, exclusions and selected model. Join `request_key` to `tasks`
(parent/owner), `jobs` (lease/provenance), `messages` (handoffs), and `calls`
(each assignment result). `cost` totals all requests, leases, retries, failures,
reported input/output tokens and provider `total_duration_ns`, plus target and
artifact Lean checks and elapsed milliseconds. Each reported metric retains
known values and unknown counts; missing usage, expired leases and elapsed time
for coordinator-caught verifier exceptions are never
silently counted as zero. Provider duration is provider-reported generation
time, not wall time; a queued request has no provider usage.
