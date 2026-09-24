# Local coordinator-owned group loop (A3)

Use `Store.start_group_loop(request_key, statement, imports, environment, models,
max_work=12, deadline=None)` to start an idempotent local group. `models` must
explicitly supply `planner`, `investigator`, `critic` and `synthesizer` model IDs;
all four may be the same model. The returned group ID can be inspected with
`Store.group(id)` and `Store.group_loop(id)` (phase, reason, configured models,
deadline). The deadline defaults to one hour and may be set to a finite time
within the next hour; a group without a compatible worker stops when it expires.
The coordinator scheduler calls `advance_groups()` between Lean
checks; `advance_group(id)` is also available for deterministic local driving.
The same local coordinator exposes `POST /v1/groups` with the above fields and
`GET /v1/groups/{id}` for the combined group/loop snapshot and terminal reason.

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
