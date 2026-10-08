"""Shared SQL operations inside an existing Store transaction.

Callers retain admission, idempotency, budget and lifecycle policy. These helpers
neither open transactions nor refresh run state.
"""

import sqlite3

from .contracts import GroupRunFields, JobKind, TaskType


def create_group_run(
    db: sqlite3.Connection, group: sqlite3.Row | GroupRunFields, created_at: float
) -> str:
    from .store import identifier

    problem_id, run_id = identifier(), identifier()
    db.execute(
        "INSERT INTO problems VALUES (?,?,?)",
        (problem_id, group["statement"], group["imports"]),
    )
    db.execute(
        """INSERT INTO runs(id,problem_id,status,max_repairs,created_at)
        VALUES (?,?,'running',0,?)""",
        (run_id, problem_id, created_at),
    )
    db.execute(
        "INSERT INTO group_runs VALUES (?,?,?)",
        (group["id"], run_id, group["environment"]),
    )
    return run_id


def insert_group_job(
    db: sqlite3.Connection,
    job_id: str,
    run_id: str,
    group_id: str,
    request_key: str,
    task_id: str,
    agent_id: str,
    environment: str,
    cost: int,
    *,
    model: str,
    max_output_tokens: int,
    max_assignments: int,
    kind: JobKind,
    task_type: TaskType | None,
    messages: str,
) -> None:
    """Insert a queued job and its binding; cost need not equal retry capacity."""
    db.execute(
        """INSERT INTO jobs
        (id,run_id,status,model,max_output_tokens,max_assignments,
         generation_timeout_seconds,kind,task_type,messages)
        VALUES (?,?,'queued',?,?,?,120,?,?,?)""",
        (
            job_id,
            run_id,
            model,
            max_output_tokens,
            max_assignments,
            kind,
            task_type,
            messages,
        ),
    )
    db.execute(
        """INSERT INTO group_jobs
        (job_id,group_id,request_key,task_id,agent_id,environment,cost)
        VALUES (?,?,?,?,?,?,?)""",
        (job_id, group_id, request_key, task_id, agent_id, environment, cost),
    )


def stop_group_jobs(db: sqlite3.Connection, group_id: str, reason: str) -> None:
    """Stop new dispatch and cancel queued jobs, leaving dispatched work to drain."""
    db.execute(
        "UPDATE group_loops SET phase='stopped',reason=? WHERE group_id=?",
        (reason, group_id),
    )
    db.execute(
        """UPDATE jobs SET status='cancelled' WHERE status='queued' AND id IN
        (SELECT job_id FROM group_jobs WHERE group_id=?)""",
        (group_id,),
    )


def group_job_source(
    db: sqlite3.Connection, group_id: str, task_id: str, agent_id: str, job_id: str
) -> sqlite3.Row | None:
    """Look up the latest completed assignment for an exact group/job binding.

    A binding without a completed assignment still returns a row. Callers check
    completion and validate the output against their message/artifact contract.
    """
    row: sqlite3.Row | None = db.execute(
        """SELECT j.status,j.kind,j.task_type,a.result,a.id AS assignment_id
        FROM group_jobs gj JOIN jobs j ON j.id=gj.job_id
        LEFT JOIN assignments a ON a.job_id=j.id AND a.status='completed'
        WHERE gj.job_id=? AND gj.group_id=? AND gj.task_id=? AND gj.agent_id=?
        ORDER BY a.rowid DESC LIMIT 1""",
        (job_id, group_id, task_id, agent_id),
    ).fetchone()
    return row
