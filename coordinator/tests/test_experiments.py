import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from solvenet.problem_set import load
from solvenet.server import Coordinator, make_server
from solvenet.store import (MIGRATION_2, MIGRATION_3, MIGRATION_4, MIGRATION_5,
                            MIGRATION_6, MIGRATION_7, SCHEMA, Store)
from solvenet.verifier import VerificationResult, VerificationStatus


class ExperimentsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'coordinator.db'
        self.store = Store(self.path)
        self.server = make_server(Coordinator(self.store, None), ('127.0.0.1', 0))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.fixture = load()

    def stop_server(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, path, data=None):
        request = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                          headers={'Content-Type': 'application/json'})
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response) if response.status != 204 else None

    def config(self, strategy='repair', **overrides):
        return dict(idempotency_key='baseline-repair', set_id=self.fixture.set_id,
                    version=self.fixture.version, sha256=self.fixture.sha256,
                    strategy=strategy, model='scripted', **overrides)

    def test_create_restart_and_linkage(self):
        status, created = self.request('/v1/experiments', self.config())
        self.assertEqual(status, 201)
        self.assertEqual(len(created['runs']), len(self.fixture.problems))
        self.assertEqual(created['config']['environment'], self.fixture.environment)
        self.assertEqual(created['config']['sha256'], self.fixture.sha256)
        self.assertEqual(created['config']['initial_jobs'], [
            {'model': 'scripted', 'count': 1, 'max_output_tokens': 2048}])
        self.assertEqual(created['config']['max_repairs'], 2)
        run_id = created['runs'][0]['run_id']
        run = self.request('/v1/runs/' + run_id)[1]
        self.assertEqual((run['experiment_id'], run['fixture_problem_id']),
                         (created['id'], self.fixture.problems[0].id))
        self.assertEqual(len(run['jobs']), 1)
        self.assertEqual(self.request('/v1/experiments/' + created['id']), (200, created))
        self.store = Store(self.path)
        self.assertEqual(self.store.experiment(created['id']), created)
        self.assertEqual(self.request('/v1/experiments', self.config()), (200, created))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM runs WHERE experiment_id=?',
                                        (created['id'],)).fetchone()[0], len(self.fixture.problems))
        raw = self.path.read_bytes()
        self.assertNotIn(self.fixture.problems[0].reference_proof.encode(), raw)
        claim = self.store.claim('worker', ['scripted'])
        self.assertNotIn('reference_proof', json.dumps(claim))
        self.assertNotIn(self.fixture.problems[0].reference_proof, json.dumps(claim))

    def test_defaults_and_validation(self):
        independent = self.config('independent')
        status, created = self.request('/v1/experiments', independent)
        self.assertEqual(status, 201)
        self.assertEqual(created['config']['max_repairs'], 0)
        self.assertEqual(len(self.store.run(created['runs'][0]['run_id'])['jobs']), 3)
        for update in ({'sha256': '0' * 64}, {'version': 2}, {'max_repairs': 1},
                       {'temperature': 0}, {'attempts': True}):
            with self.subTest(update=update):
                self.assertEqual(self.request('/v1/experiments', independent | update)[0], 400)
        self.assertEqual(self.request('/v1/experiments', independent | {'model': 'other'})[0], 409)
        self.assertEqual(self.request('/v1/runs', {'statement': ': True'})[0], 201)

    def test_generation_settings_on_experiment_and_ad_hoc_runs(self):
        settings = {'temperature': 0, 'seed': 42}
        config = self.config(generation_settings=settings)
        config['model'] = 'ollama/test'
        status, experiment = self.request('/v1/experiments', config)
        self.assertEqual(status, 201)
        self.assertEqual(experiment['config']['generation_settings'], settings)
        run = self.request('/v1/runs/' + experiment['runs'][0]['run_id'])[1]
        self.assertEqual(run['generation_settings'], settings)
        self.assertEqual(run['jobs'][0]['generation_settings'], settings)
        old_claim = {'worker_id': 'old-worker', 'models': ['ollama/test']}
        self.assertEqual(self.request('/v1/claim', old_claim)[0], 204)
        self.assertEqual(self.request('/v1/runs/' + experiment['runs'][0]['run_id'])[1]['assignments'], [])
        self.assertEqual(self.request('/v1/claim', old_claim | {
            'capabilities': ['unrecognized']})[0], 400)
        status, claimed = self.request('/v1/claim', old_claim | {
            'capabilities': ['generation_settings']})
        self.assertEqual(status, 200)
        self.assertEqual(claimed['job']['generation_settings'], settings)
        status, _ = self.request('/v1/runs', {
            'statement': ': True', 'model': 'ollama/test', 'attempts': 1})
        self.assertEqual(status, 201)
        self.assertEqual(self.request('/v1/claim', old_claim)[1]['job']['generation_settings'], {})
        status, ad_hoc = self.request('/v1/runs', {
            'statement': ': True', 'model': 'ollama/test', 'attempts': 1,
            'generation_settings': settings})
        self.assertEqual(status, 201)
        self.assertEqual(self.request('/v1/runs/' + ad_hoc['run_id'])[1]['generation_settings'], settings)
        for invalid in ({'seed': True}, {'seed': -1}, {'temperature': 3},
                        {'top_p': 0.9}, None):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.request('/v1/runs', {
                    'statement': ': True', 'generation_settings': invalid})[0], 400)
        self.assertEqual(self.request('/v1/experiments', self.config(
            generation_settings=settings))[0], 400)

    def test_heterogeneous_and_concurrent_idempotency(self):
        config = self.config()
        config.pop('model')
        config['initial_jobs'] = [{'model': 'one', 'count': 2, 'max_output_tokens': 64},
                                  {'model': 'two', 'count': 1, 'max_output_tokens': 128}]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.request('/v1/experiments', config), range(2)))
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        experiment = results[0][1]
        self.assertEqual(self.store.run(experiment['runs'][0]['run_id'])['initial_jobs'],
                         config['initial_jobs'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0],
                             len(self.fixture.problems))

    def test_failed_insert_rolls_back_experiment_and_runs(self):
        insert = self.store._insert_run
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('simulated failure')
            return insert(*args, **kwargs)

        with patch.object(self.store, '_insert_run', side_effect=fail_second):
            self.assertEqual(self.request('/v1/experiments', self.config())[0], 500)
        with self.store.connect() as db:
            for table in ('experiments', 'runs', 'jobs', 'problems'):
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)
        self.assertEqual(self.request('/v1/experiments', self.config())[0], 201)

    def test_summary_reconciles_retries_usage_depth_and_export(self):
        now = [1000.0]
        self.store.clock = lambda: now[0]
        experiment = self.request('/v1/experiments', self.config())[1]
        run_id = experiment['runs'][0]['run_id']
        # Keep the fixture set intact, but leave only this problem schedulable.
        with self.store.transaction() as db:
            db.execute("UPDATE runs SET status='exhausted' WHERE experiment_id=? AND id!=?",
                       (experiment['id'], run_id))
            db.execute("UPDATE jobs SET status='cancelled' WHERE run_id!=?", (run_id,))
        first = self.store.claim('w', ['scripted'])
        self.store.result(first['assignment_id'], {
            'lease_token': first['lease_token'], 'status': 'failed',
            'failure_class': 'transient', 'error': 'Ollama request: offline',
            'usage': {'output_tokens': 3}, 'generation': {'total_duration_ns': 80}})
        second = self.store.claim('w', ['scripted'])
        self.store.result(second['assignment_id'], {
            'lease_token': second['lease_token'], 'status': 'completed',
            'output': {'text': 'bad'}, 'usage': {'input_tokens': 10, 'output_tokens': 4},
            'generation': {'total_duration_ns': 120}})
        parent = self.store.pending()['id']
        self.store.verified(parent, VerificationResult(VerificationStatus.REJECTED, 'diagnostic secret', 12))
        repair = self.store.claim('w', ['scripted'])
        self.store.result(repair['assignment_id'], {
            'lease_token': repair['lease_token'], 'status': 'completed',
            'output': {'text': 'proof secret'}, 'usage': {'input_tokens': None}})
        now[0] = 1017.5
        proof = self.store.pending()['id']
        self.store.verified(proof, VerificationResult(VerificationStatus.VERIFIED, '', 17))
        report = self.request('/v1/experiments/' + experiment['id'] + '/summary')[1]
        self.assertEqual((report['problem_count'], report['problems_solved']),
                         (len(self.fixture.problems), 1))
        self.assertEqual(report['requests']['completed'], 2)
        self.assertEqual(report['requests']['provider_failure'], 1)
        self.assertEqual(sum(report['requests'].values()), 3)
        self.assertEqual(report['usage']['input_tokens']['known_total'], 10)
        self.assertEqual(report['usage']['input_tokens']['unknown_count'], 2)
        self.assertEqual(report['usage']['output_tokens']['known_total'], 7)
        self.assertEqual(report['usage']['output_tokens']['unknown_count'], 1)
        self.assertEqual(report['provider_generation_time']['known_total'], 200)
        self.assertEqual(report['lean_verification_time']['known_total'], 29)
        self.assertEqual(report['successes'], {'initial': 0, 'repair': 1})
        self.assertEqual(report['per_depth']['0']['requests']['provider_failure'], 1)
        self.assertEqual(report['per_depth']['1']['verified_attempts'], 1)
        self.assertEqual(report['runs'][0]['time_to_first_verified_proof_seconds'], 17.5)
        self.assertEqual(report['runs'][0]['first_verified_attempt_id'], proof)
        self.assertEqual(report['runs'][0]['attempts'][1]['parent_attempt_id'], parent)
        self.assertNotIn('proof secret', json.dumps(report))
        self.assertNotIn('diagnostic secret', json.dumps(report))
        with urlopen(self.url + '/v1/experiments/' + experiment['id'] + '/summary.md') as response:
            exported = response.read().decode()
            self.assertEqual(response.headers['Content-Type'], 'text/markdown; charset=utf-8')
        self.assertIn(self.fixture.sha256, exported)
        self.assertIn('/v1/runs/' + run_id, exported)
        self.assertIn('17.5', exported)
        self.assertNotIn('proof secret', exported)

    def test_history_and_failure_classification(self):
        experiment = self.request('/v1/experiments', self.config('independent'))[1]
        run_id = experiment['runs'][0]['run_id']
        a = self.store.claim('w', ['scripted'])
        self.store.result(a['assignment_id'], {'lease_token': a['lease_token'],
                          'status': 'failed', 'failure_class': 'permanent',
                          'error': 'Ollama proof format: missing proof',
                          'usage': {'input_tokens': 0}})
        b = self.store.claim('w', ['scripted'])
        self.store.result(b['assignment_id'], {'lease_token': b['lease_token'],
                          'status': 'rejected', 'rejection_kind': 'malformed_assignment',
                          'error': 'bad claim'})
        c = self.store.claim('w', ['scripted'])
        self.store.result(c['assignment_id'], {'lease_token': c['lease_token'],
                          'status': 'completed', 'output': {'text': 'rfl'}})
        self.store.verified(self.store.pending()['id'], VerificationResult(
            VerificationStatus.VERIFIED, '', 5))
        # Simulate timestamps absent on records written before migration 8.
        with self.store.transaction() as db:
            db.execute('UPDATE runs SET created_at=NULL WHERE id=?', (run_id,))
            db.execute('UPDATE verifications SET verified_at=NULL')
        report = self.store.experiment_summary(experiment['id'])
        self.assertEqual(report['requests']['formatting_failure'], 1)
        self.assertEqual(report['requests']['rejected'], 1)
        self.assertEqual(report['successes']['initial'], 1)
        self.assertEqual(report['usage']['input_tokens']['known_count'], 1)
        self.assertEqual(report['usage']['input_tokens']['known_total'], 0)
        self.assertEqual(report['usage']['input_tokens']['unknown_count'], 2)
        self.assertEqual(report['runs'][0]['time_to_first_verified_proof_seconds'], None)
        self.assertEqual(report['time_to_first_verified_proof']['unknown_count'], 1)
        self.assertEqual(self.request('/v1/experiments/' + '0' * 32 + '/summary')[0], 404)

    def test_v7_migration_keeps_historical_proofs_without_inventing_time(self):
        old = Path(self.temp.name) / 'old.db'
        with sqlite3.connect(old) as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.executescript(SCHEMA + MIGRATION_2 + MIGRATION_3 + MIGRATION_4 +
                             MIGRATION_5 + MIGRATION_6 + MIGRATION_7)
            config = {'strategy': 'independent', 'set_id': 'core', 'version': 1,
                      'sha256': 'oldhash', 'environment': 'leanprover/lean4:v4.19.0'}
            db.execute('INSERT INTO experiments VALUES (?, ?, ?, ?)',
                       ('e', 'key', json.dumps(config), 100.0))
            db.execute("INSERT INTO problems VALUES ('p', ': True', '[\"Init\"]')")
            db.execute("""INSERT INTO runs
                (id, problem_id, status, experiment_id, fixture_problem_id)
                VALUES ('r', 'p', 'solved', 'e', 'example')""")
            db.execute("INSERT INTO jobs (id, run_id, status, model, max_output_tokens, max_assignments) VALUES ('j', 'r', 'done', 'scripted', 64, 3)")
            db.execute("INSERT INTO assignments VALUES ('a', 'j', 'w', 'token', 200, 'completed', NULL)")
            db.execute("INSERT INTO attempts (id, assignment_id, candidate, model, usage) VALUES ('t', 'a', 'rfl', 'scripted', '{}')")
            db.execute("INSERT INTO verifications VALUES ('t', 'verified', '', 11)")
        migrated = Store(old)
        report = migrated.experiment_summary('e')
        self.assertEqual(report['problems_solved'], 1)
        self.assertEqual(report['runs'][0]['first_verified_attempt_id'], 't')
        self.assertIsNone(report['runs'][0]['time_to_first_verified_proof_seconds'])
        self.assertEqual(report['time_to_first_verified_proof']['unknown_count'], 1)
        with migrated.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 9)
            self.assertIsNone(db.execute("SELECT created_at FROM runs WHERE id='r'").fetchone()[0])
            self.assertIsNone(db.execute("SELECT verified_at FROM verifications WHERE attempt_id='t'").fetchone()[0])

    def test_expired_lease_has_unknown_usage_and_no_measured_total(self):
        now = [1000]
        self.store.clock = lambda: now[0]
        experiment = self.request('/v1/experiments', self.config())[1]
        self.store.claim('w', ['scripted'])
        now[0] += 31
        self.store.expire()
        report = self.store.experiment_summary(experiment['id'])
        self.assertEqual(report['requests']['expired'], 1)
        self.assertEqual(report['usage']['input_tokens']['unknown_count'], 1)
        self.assertIsNone(report['usage']['input_tokens']['known_total'])
        self.assertIsNone(report['provider_generation_time']['known_total'])


if __name__ == '__main__':
    unittest.main()
