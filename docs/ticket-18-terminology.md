# Ticket 18 — Task lifecycle terminology

## Glossary

| Term | Meaning in the current prototype |
| --- | --- |
| Problem | A Lean theorem statement plus imports, stored under a `problem_id`. Submitting a run creates its problem record. |
| Run | One submission and its search policy/state, stored under a `run_id`. Initial jobs may now target different requested models; the simple submission form still targets one. |
| Initial attempt / initial search chain | One of the starting paths requested by v1 `attempts` or a grouped `initial_jobs[].count`. Each starts with one initial job (`repair_depth=0`) and may acquire up to `max_repairs` sequential repair jobs after Lean rejects candidates. Independence means separate job/repair lineage, not necessarily a different model, worker, or logical agent. |
| Job | A queued unit of model generation, initial or repair. Failed/expired execution can retry **the same job**. A rejected candidate can lead to a **new repair job**. |
| Assignment | A leased dispatch of one job to one worker daemon, created on claim. It can complete, fail, expire, or be rejected before execution. Retry of a job creates another assignment, not another initial search chain. |
| Candidate attempt | A completed assignment's proof body, stored in the `attempts` table and exposed as `attempts[]` in run inspection. Lean verification determines whether it is verified, rejected, timed out, or an infrastructure error; a claim or provider failure alone is not a candidate attempt. |
| Worker daemon | A Go process advertising available models and claiming assignments; the current implementation has one execution slot. Several chains can pass through the same worker, and separate assignments of one job can reach different workers. |
| Provider | The worker's model-specific execution adapter (currently scripted or Ollama). The run's requested model identifier determines which workers can claim a job; `generation.model` is provider-reported metadata, not an agent identity. |
| Logical agent | A reasoning process/strategy in the project's longer-term multi-agent goal. It has no first-class identity, scheduling, or persistence in v1; neither `attempts` nor a worker count denotes distinct agents. |

## Bounds and compatibility decision

For submission `attempts = A` (or `A = sum(initial_jobs[].count)`), `max_repairs = R`, and `max_assignments = M`,
there are initially `A` jobs and at most `A * (1 + R)` jobs in total. At most
`A * (1 + R) * M` **budgeted assignments** can be consumed by completed work,
transient failures, permanent failures, and expired leases. These are ceilings,
not promised counts: verification success can cancel queued jobs, repairs require
Lean rejection, and permanent failures can stop jobs early. A failed or expired
assignment may consume budget without a model call or completed candidate.
Pre-execution malformed/unsupported assignment rejections do not consume the
budget and can be repeated indefinitely, so **raw claims/assignments do not have
that bound**. The formula is not a bound on tokens or a guarantee of model calls.

Keep v1 `POST /v1/runs` field `attempts`, SQLite table `attempts`, and inspection
array `attempts[]` as they are. An additive `initial_attempts` alias would create
two competing request spellings (and require a precedence/conflict rule) without
changing the distinct meaning of inspection `attempts[]`. Documentation clarifies
the existing wire and storage names; no schema/API migration or behavior change
is needed. A future protocol revision can choose a more explicit submission name.

## Files changed

- `README.md`: prototype identity, repair-chain example, and dispatch bounds.
- `protocol/v1.md`: request/inspection distinction and claim lifecycle.
- `AGENTS.md`: distinguish future agents from current jobs and workers.
- `coordinator/src/solvenet/store.py`, `coordinator/src/solvenet/server.py`: explanatory API comments/docstrings without wire changes.
- This document: glossary, compatibility decision, and checks.

## Checks and results

- Local Markdown link targets across the repository: OK.
- `git diff --check`: OK.
- `PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -q`:
  88 tests passed (15 skipped when optional Lean/Docker dependencies were absent).
- `(cd worker && go test ./...)`: all three packages passed.

## Remaining concerns

The v1 field `attempts` remains overloaded across submission and inspection;
consumers should use the context above. The budgeted-assignment formula does not
limit repeated pre-execution rejections or quantify actual token usage.
