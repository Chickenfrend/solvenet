"""Small, transactional SQLite job queue. Times are Unix seconds."""

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class Conflict(Exception):
    pass


SCHEMA = """
CREATE TABLE problems (id TEXT PRIMARY KEY, statement TEXT NOT NULL, imports TEXT NOT NULL);
CREATE TABLE runs (id TEXT PRIMARY KEY, problem_id TEXT NOT NULL REFERENCES problems(id), status TEXT NOT NULL);
CREATE TABLE jobs (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), status TEXT NOT NULL,
 model TEXT NOT NULL, max_output_tokens INTEGER NOT NULL, max_assignments INTEGER NOT NULL);
CREATE TABLE assignments (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), worker_id TEXT NOT NULL,
 token TEXT NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL, result TEXT);
CREATE TABLE attempts (
 id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL UNIQUE REFERENCES assignments(id),
 candidate TEXT NOT NULL, model TEXT NOT NULL, usage TEXT NOT NULL);
CREATE TABLE verifications (
 attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), status TEXT NOT NULL,
 diagnostics TEXT NOT NULL, elapsed_ms INTEGER NOT NULL);
CREATE INDEX jobs_status ON jobs(status);
CREATE INDEX assignments_expiry ON assignments(status, expires);
PRAGMA user_version = 1;
"""


def identifier():
    return uuid4().hex


class Store:
    def __init__(self, path: Path, *, lease_seconds=30, clock=time.time):
        self.path = path
        self.lease_seconds = lease_seconds
        self.clock = clock
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "COMMIT;")
            elif version != 1:
                raise RuntimeError(f"Unsupported database schema {version}")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    def submit(self, statement, imports, attempts=3, model="scripted", max_output_tokens=2048):
        problem, run = identifier(), identifier()
        with self.transaction() as db:
            db.execute("INSERT INTO problems VALUES (?, ?, ?)", (problem, statement, json.dumps(imports)))
            db.execute("INSERT INTO runs VALUES (?, ?, 'running')", (run, problem))
            for _ in range(attempts):
                db.execute("INSERT INTO jobs VALUES (?, ?, 'queued', ?, ?, 3)",
                           (identifier(), run, model, max_output_tokens))
        return {"problem_id": problem, "run_id": run}

    def _refresh(self, db):
        db.execute("""UPDATE runs SET status='error' WHERE status='running' AND id IN (
          SELECT j.run_id FROM jobs j JOIN assignments a ON a.job_id=j.id
          JOIN attempts t ON t.assignment_id=a.id JOIN verifications v ON v.attempt_id=t.id
          WHERE v.status='verifier_error')""")
        db.execute("""UPDATE jobs SET status='cancelled' WHERE status='queued'
          AND run_id IN (SELECT id FROM runs WHERE status IN ('solved','error'))""")
        db.execute("""UPDATE runs SET status='exhausted' WHERE status='running' AND NOT EXISTS
          (SELECT 1 FROM jobs WHERE jobs.run_id=runs.id AND status IN ('queued','assigned','verifying'))""")

    def _expire(self, db):
        expired = db.execute("SELECT * FROM assignments WHERE status='active' AND expires<=?", (self.clock(),)).fetchall()
        for a in expired:
            db.execute("UPDATE assignments SET status='expired' WHERE id=?", (a['id'],))
            self._retry(db, a['job_id'])
        self._refresh(db)

    def _retry(self, db, job_id):
        count = db.execute("SELECT count(*) FROM assignments WHERE job_id=?", (job_id,)).fetchone()[0]
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        state = 'queued' if count < job['max_assignments'] else 'failed'
        db.execute("UPDATE jobs SET status=? WHERE id=?", (state, job_id))

    def expire(self):
        with self.transaction() as db:
            self._expire(db)

    def claim(self, worker_id, models):
        with self.transaction() as db:
            self._expire(db)
            job = None
            for model in models:
                job = db.execute("""SELECT j.*, p.statement, p.imports FROM jobs j
                  JOIN runs r ON r.id=j.run_id JOIN problems p ON p.id=r.problem_id
                  WHERE j.status='queued' AND r.status='running' AND j.model=? ORDER BY j.rowid LIMIT 1""", (model,)).fetchone()
                if job:
                    break
            if job is None:
                return None
            assignment, token = identifier(), secrets.token_urlsafe(32)
            expires = self.clock() + self.lease_seconds
            db.execute("INSERT INTO assignments VALUES (?, ?, ?, ?, ?, 'active', NULL)",
                       (assignment, job['id'], worker_id, token, expires))
            db.execute("UPDATE jobs SET status='assigned' WHERE id=?", (job['id'],))
            return {"protocol_version": 1, "assignment_id": assignment, "lease_token": token,
                    "lease_expires_at": expires, "heartbeat_seconds": self.lease_seconds / 3,
                    "job": {"id": job['id'], "kind": "model.generate", "model": job['model'],
                            "statement": job['statement'], "imports": json.loads(job['imports']),
                            "max_output_tokens": job['max_output_tokens'], "timeout_seconds": 120,
                            "messages": [{"role": "system", "content": "Return only a Lean tactic proof body."},
                                         {"role": "user", "content": job['statement']}]}}

    def _assignment(self, db, assignment, token):
        row = db.execute("SELECT * FROM assignments WHERE id=?", (assignment,)).fetchone()
        if row is None or not secrets.compare_digest(row['token'], token):
            raise Conflict("Unknown assignment or invalid lease token")
        return row

    def heartbeat(self, assignment, token):
        with self.transaction() as db:
            row = self._assignment(db, assignment, token)
            if row['status'] != 'active' or row['expires'] <= self.clock():
                raise Conflict("Assignment is no longer active")
            expires = self.clock() + self.lease_seconds
            db.execute("UPDATE assignments SET expires=? WHERE id=?", (expires, assignment))
            return {"lease_expires_at": expires}

    def result(self, assignment, payload):
        canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        with self.transaction() as db:
            row = self._assignment(db, assignment, payload['lease_token'])
            if row['result'] is not None:
                if row['result'] != canonical:
                    raise Conflict("Assignment already has a different result")
                return {"accepted": True}
            if row['status'] != 'active' or row['expires'] <= self.clock():
                raise Conflict("Assignment expired")
            db.execute("UPDATE assignments SET status='completed', result=? WHERE id=?", (canonical, assignment))
            if payload['status'] == 'completed':
                job = db.execute("SELECT model FROM jobs WHERE id=?", (row['job_id'],)).fetchone()
                db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, ?)",
                           (identifier(), assignment, payload['output']['text'], job['model'], json.dumps(payload.get('usage', {}))))
                db.execute("UPDATE jobs SET status='verifying' WHERE id=?", (row['job_id'],))
            else:
                self._retry(db, row['job_id'])
            self._refresh(db)
            return {"accepted": True}

    def pending(self):
        with self.connect() as db:
            row = db.execute("""SELECT t.id, t.candidate, p.statement, p.imports FROM attempts t
              JOIN assignments a ON a.id=t.assignment_id JOIN jobs j ON j.id=a.job_id
              JOIN runs r ON r.id=j.run_id JOIN problems p ON p.id=r.problem_id
              LEFT JOIN verifications v ON v.attempt_id=t.id WHERE v.attempt_id IS NULL LIMIT 1""").fetchone()
            return dict(row) if row else None

    def verified(self, attempt, result):
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO verifications VALUES (?, ?, ?, ?)",
                       (attempt, result.status, result.diagnostics, result.elapsed_ms))
            job = db.execute("""SELECT j.* FROM jobs j JOIN assignments a ON a.job_id=j.id
              JOIN attempts t ON t.assignment_id=a.id WHERE t.id=?""", (attempt,)).fetchone()
            db.execute("UPDATE jobs SET status='done' WHERE id=?", (job['id'],))
            if result.verified:
                db.execute("UPDATE runs SET status='solved' WHERE id=?", (job['run_id'],))
            self._refresh(db)

    def run(self, run_id):
        with self.connect() as db:
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                return None
            result = dict(run)
            problem = db.execute("SELECT * FROM problems WHERE id=?", (run['problem_id'],)).fetchone()
            result['problem'] = dict(problem)
            result['problem']['imports'] = json.loads(problem['imports'])
            result['jobs'] = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,))]
            result['attempts'] = [dict(r) for r in db.execute("""SELECT t.*, v.status AS verification_status,
              v.diagnostics, v.elapsed_ms FROM attempts t JOIN assignments a ON a.id=t.assignment_id
              JOIN jobs j ON j.id=a.job_id LEFT JOIN verifications v ON v.attempt_id=t.id
              WHERE j.run_id=?""", (run_id,))]
            result['assignments'] = [dict(r) for r in db.execute("""SELECT a.id, a.job_id, a.worker_id,
              a.expires, a.status, json_extract(a.result, '$.error') AS error
              FROM assignments a JOIN jobs j ON j.id=a.job_id WHERE j.run_id=?""", (run_id,))]
            for attempt in result['attempts']:
                attempt['usage'] = json.loads(attempt['usage'])
            return result
