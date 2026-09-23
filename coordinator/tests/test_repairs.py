import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from solvenet.store import Store, SCHEMA, MIGRATION_2
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
