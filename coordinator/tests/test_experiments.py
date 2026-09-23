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
from solvenet.store import Store


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
            return response.status, json.load(response)

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


if __name__ == '__main__':
    unittest.main()
