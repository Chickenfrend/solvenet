import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from solvenet.store import (
    MAX_REPAIRS,
    MIGRATION_2,
    MIGRATION_3,
    MIGRATION_4,
    MIGRATION_5,
    SCHEMA,
    Store,
)
from solvenet.verifier import VerificationResult, VerificationStatus


def outcome(status='rejected', diagnostics="expected '['"):
    return VerificationResult(VerificationStatus(status), diagnostics, 12)


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.db'
        self.now = 1000
        self.store = Store(self.path, clock=lambda: self.now)
        self.run = self.store.submit('(n : Nat) : n + 0 = n', ['Init'], attempts=1,
                                     model='ollama/test', max_output_tokens=256, max_repairs=2,
                                     generation_timeout_seconds=321, max_assignments=2)['run_id']

    def claim(self):
        return self.store.claim('test-worker', ['ollama/test'])

    def submit(self, claim, proof='rw zero_add'):
        self.store.result(claim['assignment_id'], {'lease_token': claim['lease_token'],
                          'status': 'completed', 'output': {'text': proof}})
        return self.store.pending()['id']

    def test_repair_context_link_and_restart_idempotency(self):
        first = self.claim()
        self.assertEqual(first['job']['repair_depth'], 0)
        self.assertIsNone(first['job']['parent_attempt_id'])
        self.assertEqual(first['job']['messages'], [])
        attempt = self.submit(first)
        # Restart with a pending verification, then deliver it concurrently twice.
        self.store = Store(self.path, clock=lambda: self.now)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.store.verified(attempt, outcome()), range(2)))
        self.store = Store(self.path, clock=lambda: self.now)
        self.assertEqual(len(self.store.run(self.run)['jobs']), 2)
        repair = self.claim()['job']
        self.assertEqual(repair['parent_attempt_id'], attempt)
        self.assertEqual(repair['repair_depth'], 1)
        self.assertEqual(repair['model'], first['job']['model'])
        self.assertEqual(repair['max_output_tokens'], 256)
        self.assertEqual(self.store.run(self.run)['jobs'][1]['max_assignments'], 2)
        self.assertEqual(repair['timeout_seconds'], 321)
        self.assertEqual(repair['statement'], first['job']['statement'])
        self.assertEqual(repair['imports'], ['Init'])
        self.assertEqual(len(repair['messages']), 1)
        self.assertEqual(repair['messages'][0]['role'], 'user')
        prompt = repair['messages'][-1]['content']
        self.assertIn('rw zero_add', prompt)
        self.assertIn("expected '['", prompt)
        self.assertIn('Do not repeat the previous candidate unchanged', prompt)
        self.assertIn('Correct the specific error reported by Lean', prompt)
        # A conflicting verification replay must not change the saved outcome.
        self.store.verified(attempt, outcome('verified'))
        self.assertEqual(self.store.run(self.run)['status'], 'running')

    def test_budget_exhausted_after_two_repairs(self):
        parent = None
        for depth in range(3):
            claim = self.claim()
            self.assertEqual(claim['job']['repair_depth'], depth)
            self.assertEqual(claim['job']['parent_attempt_id'], parent)
            parent = self.submit(claim, f'bad_{depth}')
            self.store.verified(parent, outcome())
        run = self.store.run(self.run)
        self.assertEqual(run['status'], 'exhausted')
        self.assertEqual(len(run['jobs']), 3)
        self.assertEqual([a['repair_depth'] for a in run['attempts']], [0, 1, 2])
        self.assertIsNone(self.claim())

    def test_application_repair_bound(self):
        self.assertIsNotNone(self.store.submit(': True', ['Init'], max_repairs=MAX_REPAIRS))
        with self.assertRaisesRegex(ValueError, f'between 0 and {MAX_REPAIRS}'):
            self.store.submit(': True', ['Init'], max_repairs=MAX_REPAIRS + 1)

    def test_success_stops_chain(self):
        self.store.verified(self.submit(self.claim()), outcome())
        self.store.verified(self.submit(self.claim(), 'rfl'), outcome('verified'))
        self.assertEqual(self.store.run(self.run)['status'], 'solved')
        self.assertEqual(len(self.store.run(self.run)['jobs']), 2)
        self.assertIsNone(self.claim())

    def test_infrastructure_error_does_not_generate_repair(self):
        self.store.verified(self.submit(self.claim()), outcome('verifier_error'))
        run = self.store.run(self.run)
        self.assertEqual(run['status'], 'error')
        self.assertEqual(len(run['jobs']), 1)

    def test_verification_timeout_ends_chain(self):
        self.store.verified(self.submit(self.claim()), outcome('timeout'))
        self.assertEqual(self.store.run(self.run)['status'], 'exhausted')
        self.assertEqual(len(self.store.run(self.run)['jobs']), 1)

    def test_execution_failure_retries_same_repair_job(self):
        self.store.verified(self.submit(self.claim()), outcome())
        repair = self.claim()
        self.store.result(repair['assignment_id'], {'lease_token': repair['lease_token'],
                          'status': 'failed', 'error': 'Ollama unavailable'})
        retry = self.claim()
        self.assertEqual(retry['job'], repair['job'])
        self.assertNotEqual(retry['assignment_id'], repair['assignment_id'])
        # An expired repair assignment is also retried without increasing depth.
        self.now += 31
        self.assertIsNone(self.claim())
        self.assertEqual(self.store.run(self.run)['status'], 'exhausted')
        self.assertEqual(len(self.store.run(self.run)['jobs']), 2)

    def test_terminal_run_does_not_spawn_late_repair(self):
        for terminal in ('verified', 'verifier_error'):
            with self.subTest(terminal=terminal):
                run_id = self.store.submit(': True', ['Init'], attempts=2, model='other', max_repairs=2)['run_id']
                a = self.store.claim('w', ['other'])
                b = self.store.claim('w', ['other'])
                self.store.verified(self.submit(a), outcome(terminal))
                self.store.verified(self.submit(b), outcome())
                run = self.store.run(run_id)
                self.assertEqual(len(run['jobs']), 2)
                self.assertEqual(run['status'], 'solved' if terminal == 'verified' else 'error')

    def test_diagnostic_excerpt_is_bounded_and_original_retained(self):
        diagnostics = 'é' * 10000
        parent = self.submit(self.claim())
        self.store.verified(parent, outcome(diagnostics=diagnostics))
        repair = self.claim()
        prompt = repair['job']['messages'][-1]['content']
        self.assertIn('[diagnostics truncated for repair prompt]', prompt)
        self.assertLess(len(prompt.encode()), 9000)
        self.assertEqual(self.store.run(self.run)['attempts'][0]['diagnostics'], diagnostics)

    def test_version_2_migration_leaves_existing_runs_independent(self):
        path = Path(self.temp.name) / 'v2.db'
        with sqlite3.connect(path) as db:
            db.executescript(SCHEMA + MIGRATION_2)
            db.execute("INSERT INTO problems VALUES ('p', ': True', '[\"Init\"]')")
            db.execute("INSERT INTO runs VALUES ('r', 'p', 'running')")
            db.execute("INSERT INTO jobs VALUES ('j', 'r', 'queued', 'scripted', 256, 3)")
        migrated = Store(path)
        run = migrated.run('r')
        self.assertEqual(run['max_repairs'], 0)
        self.assertEqual(run['jobs'][0]['repair_depth'], 0)
        self.assertIsNone(run['jobs'][0]['parent_attempt_id'])
        claim = migrated.claim('w', ['scripted'])
        migrated.result(claim['assignment_id'], {'lease_token': claim['lease_token'],
                        'status': 'completed', 'output': {'text': 'refl'}})
        migrated.verified(migrated.pending()['id'], outcome())
        self.assertEqual(migrated.run('r')['status'], 'exhausted')
        self.assertEqual(len(Store(path).run('r')['jobs']), 1)

    def test_version_5_migration_preserves_repair_chain_and_constraints(self):
        path = Path(self.temp.name) / 'v5.db'
        with sqlite3.connect(path) as db:
            db.executescript(SCHEMA + MIGRATION_2 + MIGRATION_3 + MIGRATION_4 + MIGRATION_5)
            db.execute("INSERT INTO problems VALUES ('p', ': True', '[\"Init\"]')")
            db.execute("INSERT INTO runs VALUES ('r', 'p', 'running', 2, 321, 4)")
            db.execute("INSERT INTO jobs VALUES ('j0', 'r', 'done', 'scripted', 256, 4, NULL, 0, 321)")
            db.execute("INSERT INTO assignments VALUES ('a0', 'j0', 'w', 'token0', 2000, 'completed', NULL)")
            db.execute("INSERT INTO attempts VALUES ('t0', 'a0', 'bad0', 'scripted', '{}', '{}')")
            db.execute("INSERT INTO verifications VALUES ('t0', 'rejected', 'first error', 10)")
            db.execute("INSERT INTO jobs VALUES ('j1', 'r', 'done', 'scripted', 256, 4, 't0', 1, 321)")
            db.execute("INSERT INTO assignments VALUES ('a1', 'j1', 'w', 'token1', 2001, 'completed', NULL)")
            db.execute("INSERT INTO attempts VALUES ('t1', 'a1', 'bad1', 'scripted', '{}', '{}')")
            db.execute("INSERT INTO verifications VALUES ('t1', 'rejected', 'second error', 11)")
            db.execute("INSERT INTO jobs VALUES ('j2', 'r', 'queued', 'scripted', 256, 4, 't1', 2, 321)")

        migrated = Store(path)
        run = migrated.run('r')
        self.assertEqual(run['max_repairs'], 2)
        self.assertEqual(run['generation_timeout_seconds'], 321)
        self.assertEqual(run['max_assignments'], 4)
        self.assertEqual([job['id'] for job in run['jobs']], ['j0', 'j1', 'j2'])
        self.assertEqual([job['parent_attempt_id'] for job in run['jobs']], [None, 't0', 't1'])
        self.assertEqual([job['repair_depth'] for job in run['jobs']], [0, 1, 2])
        self.assertEqual(run['initial_jobs'], [
            {'model': 'scripted', 'count': 1, 'max_output_tokens': 256}])
        self.assertEqual([attempt['id'] for attempt in run['attempts']], ['t0', 't1'])
        with migrated.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 8)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            indexes = {row['name'] for row in db.execute("PRAGMA index_list('jobs')")}
            self.assertIn('jobs_status', indexes)
            self.assertIn('jobs_parent_attempt', indexes)
            self.assertEqual(
                {row['table'] for row in db.execute("PRAGMA foreign_key_list('runs')")},
                {'problems', 'experiments'},
            )
            self.assertEqual(
                {row['table'] for row in db.execute("PRAGMA foreign_key_list('jobs')")},
                {'runs', 'attempts'},
            )
            self.assertEqual(
                {row['table'] for row in db.execute("PRAGMA foreign_key_list('assignments')")},
                {'jobs'},
            )
            runs_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'").fetchone()[0]
            jobs_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()[0]
            self.assertIn('max_repairs >= 0', runs_sql)
            self.assertIn('repair_depth >= 0', jobs_sql)
            self.assertNotIn('max_repairs BETWEEN 0 AND 2', runs_sql)
            self.assertNotIn('repair_depth BETWEEN 0 AND 2', jobs_sql)

        # Values above the current API policy are structurally valid, while
        # negative values and duplicate repair parents remain impossible.
        with migrated.transaction() as db:
            db.execute("UPDATE runs SET max_repairs=? WHERE id='r'", (MAX_REPAIRS + 1,))
            db.execute("UPDATE jobs SET repair_depth=? WHERE id='j2'", (MAX_REPAIRS + 1,))
        with self.assertRaises(sqlite3.IntegrityError):
            with migrated.transaction() as db:
                db.execute("UPDATE runs SET max_repairs=-1 WHERE id='r'")
        with self.assertRaises(sqlite3.IntegrityError):
            with migrated.transaction() as db:
                db.execute("UPDATE jobs SET repair_depth=-1 WHERE id='j2'")
        with self.assertRaises(sqlite3.IntegrityError):
            with migrated.transaction() as db:
                db.execute("INSERT INTO jobs VALUES ('duplicate', 'r', 'queued', 'scripted', 256, 4, 't1', 3, 321)")
