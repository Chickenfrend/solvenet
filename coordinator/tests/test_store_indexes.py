import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from solvenet.store import Store


class StoreIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.db"

    def test_run_snapshots_use_lookup_indexes_with_unrelated_history(self):
        store = Store(self.path, clock=lambda: 1000)
        runs = [
            store.submit(": True", ["Init"], attempts=3)["run_id"] for _ in range(200)
        ]
        with store.transaction() as db:
            jobs = db.execute("SELECT id FROM jobs ORDER BY rowid").fetchall()
            db.executemany(
                """INSERT INTO assignments
                (id,job_id,worker_id,token,expires,status)
                VALUES (?,?,?,?,?,?)""",
                [
                    (f"{job[0]}-{status}", job[0], "worker", "token", 2000, status)
                    for job in jobs
                    for status in ("expired", "completed", "active")
                ],
            )
            db.execute("ANALYZE")

        # Inspect the actual snapshot queries, so the test continues to cover
        # Store's access paths when projections or joins change.
        statements = []
        connect = store.connect

        @contextmanager
        def traced_connect():
            with connect() as db:
                db.set_trace_callback(statements.append)
                yield db

        with patch.object(store, "connect", traced_connect):
            history = store.run(runs[100])
            status = store.run_status(runs[100])
        self.assertEqual(len(history["jobs"]), 3)
        self.assertEqual(len(history["assignments"]), 9)
        self.assertEqual(status["jobs"], 3)
        self.assertEqual(status["assignments"], 9)

        job_queries = [sql for sql in statements if "FROM jobs WHERE run_id=" in sql]
        assignment_queries = [
            sql
            for sql in statements
            if "FROM assignments a" in sql
            and "WHERE j.run_id=" in sql
            and "a.status='active'" not in sql
        ]
        self.assertEqual(len(job_queries), 2)
        self.assertEqual(len(assignment_queries), 2)
        with store.connect() as db:
            for sql in job_queries + assignment_queries:
                with self.subTest(query=sql):
                    details = [
                        row["detail"] for row in db.execute("EXPLAIN QUERY PLAN " + sql)
                    ]
                    self.assertTrue(
                        any(
                            "SEARCH" in detail and "jobs_run" in detail
                            for detail in details
                        ),
                        details,
                    )
                    if sql in assignment_queries:
                        self.assertTrue(
                            any(
                                "SEARCH" in detail and "assignments_job" in detail
                                for detail in details
                            ),
                            details,
                        )

    def test_v24_migration_preserves_history_and_can_be_reopened(self):
        with patch("solvenet.store.MIGRATION_25", "PRAGMA user_version=25;"):
            old = Store(self.path, clock=lambda: 1000)
        run_id = old.submit(": True", ["Init"], attempts=2)["run_id"]
        assignment = old.claim("worker", ["scripted"])
        old.result(
            assignment["assignment_id"],
            {
                "lease_token": assignment["lease_token"],
                "status": "completed",
                "output": {"text": "trivial"},
            },
        )
        before = old.run(run_id)
        status_before = old.run_status(run_id)
        with old.transaction() as db:
            db.execute("PRAGMA user_version=24")
            self.assertFalse(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name IN "
                    "('jobs_run','assignments_job')"
                ).fetchall()
            )

        migrated = Store(self.path, clock=lambda: 1000)
        self.assertEqual(migrated.run(run_id), before)
        self.assertEqual(migrated.run_status(run_id), status_before)
        with migrated.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 25)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            for index, column in [
                ("jobs_run", "run_id"),
                ("assignments_job", "job_id"),
            ]:
                self.assertEqual(
                    [row["name"] for row in db.execute(f"PRAGMA index_info({index})")],
                    [column],
                )
        self.assertEqual(Store(self.path).run(run_id), before)
