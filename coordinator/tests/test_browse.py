import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
from urllib.request import Request

from solvenet.problem_set import EXPERIMENT_SETS, load
from solvenet.server import Coordinator, make_server
from solvenet.store import Store


class BrowseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'browse.db')
        self.server = make_server(Coordinator(self.store, None), ('127.0.0.1', 0))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.close)
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def get(self, path):
        try:
            response = urlopen(self.url + path, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_fixture_catalog_pages_and_proof_exclusion(self):
        status, catalog = self.get('/v1/fixture-sets')
        self.assertEqual(status, 200)
        self.assertEqual([(item['set_id'], item['version']) for item in catalog['items']],
                         sorted(EXPERIMENT_SETS))
        for (set_id, version), path in EXPERIMENT_SETS.items():
            fixture = load(path)
            base = f'/v1/fixture-sets/{set_id}/versions/{version}'
            first = self.get(base + '?limit=2')[1]
            second = self.get(base + f'?limit=2&offset={first["next_offset"]}')[1]
            self.assertEqual([p['id'] for p in first['problems'] + second['problems']],
                             [p.id for p in fixture.problems[:4]])
            self.assertEqual(first['sha256'], fixture.sha256)
            self.assertEqual(first['problem_count'], len(fixture.problems))
            self.assertNotIn('statement', first['problems'][0])
            self.assertEqual(self.get(base + '?offset=999')[1]['problems'], [])
            self.assertIsNone(self.get(base + '?offset=999')[1]['next_offset'])
            for problem in fixture.problems:
                detail = self.get(base + '/problems/' + problem.id)[1]
                self.assertEqual(detail['title'], problem.title)
                self.assertEqual(detail['statement'], problem.statement)
                self.assertEqual(detail['imports'], list(problem.imports))
                for response in (catalog, first, second, detail):
                    encoded = json.dumps(response)
                    self.assertNotIn('reference_proof', encoded)
                    self.assertNotIn(problem.reference_proof, encoded)
            self.assertEqual(self.get(base + '/problems/unknown')[0], 404)
        for path in ('/v1/fixture-sets/unknown/versions/1',
                     '/v1/fixture-sets/core/versions/2',
                     '/v1/fixture-sets/core/versions/true'):
            self.assertEqual(self.get(path)[0], 404)

    def test_recent_activity_cursor_bound_order_and_linkage(self):
        fixture = load()
        run_ids = [self.store.submit(f': True -- {i}', ['Init'], attempts=1)['run_id']
                   for i in range(4)]
        config = {'set_id': fixture.set_id, 'version': fixture.version,
                  'sha256': fixture.sha256, 'environment': fixture.environment,
                  'strategy': 'independent', 'initial_jobs': [
                      {'model': 'scripted', 'count': 1, 'max_output_tokens': 128}],
                  'max_repairs': 0, 'generation_timeout_seconds': 120, 'max_assignments': 3}
        experiments = [self.store.create_experiment(f'key-{i}', config, fixture.problems[:1])[0]
                       for i in range(3)]
        latest = experiments[-1]['runs'][0]['run_id']
        expected = [e['runs'][0]['run_id'] for e in reversed(experiments)] + list(reversed(run_ids))
        page = self.get('/v1/runs?limit=2')[1]
        self.assertEqual([r['run_id'] for r in page['items']], expected[:2])
        self.assertEqual(page['items'][0]['fixture_problem_id'], fixture.problems[0].id)
        self.assertEqual(page['items'][0]['fixture_set_id'], fixture.set_id)
        self.assertEqual(page['items'][0]['fixture_version'], fixture.version)
        self.assertEqual(page['items'][0]['experiment_id'], experiments[-1]['id'])
        self.assertEqual(page['items'][0]['models'], ['scripted'])
        self.assertIsNone(self.get('/v1/runs?limit=100')[1]['items'][-1]['fixture_problem_id'])
        self.assertEqual(self.get('/v1/runs/' + latest)[0], 200)
        # Inserting a newer run between pages must not repeat or displace old entries.
        self.store.submit(': True', ['Init'], attempts=1)
        seen = [r['run_id'] for r in page['items']]
        while page['next_cursor'] is not None:
            page = self.get(f'/v1/runs?limit=2&before={page["next_cursor"]}')[1]
            self.assertLessEqual(len(page['items']), 2)
            seen.extend(r['run_id'] for r in page['items'])
        self.assertEqual(seen, expected)

        page = self.get('/v1/experiments?limit=1')[1]
        self.assertEqual(page['items'][0]['id'], experiments[-1]['id'])
        self.assertEqual(page['items'][0]['run_count'], 1)
        self.assertEqual(page['items'][0]['strategy'], 'independent')
        self.assertEqual(page['items'][0]['set_id'], fixture.set_id)
        self.assertNotIn('config', page['items'][0])
        ids = []
        while True:
            ids.extend(e['id'] for e in page['items'])
            if page['next_cursor'] is None:
                break
            page = self.get(f'/v1/experiments?limit=1&before={page["next_cursor"]}')[1]
        self.assertEqual(ids, [e['id'] for e in reversed(experiments)])
        self.assertEqual(self.get('/v1/experiments/' + experiments[-1]['id'])[0], 200)

    def test_invalid_pagination_and_unknown_ids(self):
        for path in ('/v1/runs', '/v1/experiments', '/v1/fixture-sets/core/versions/1'):
            for query in ('limit=0', 'limit=101', 'limit=-1', 'limit=true',
                          'limit=1&limit=2', 'bogus=1'):
                with self.subTest(path=path, query=query):
                    self.assertEqual(self.get(path + '?' + query)[0], 400)
        self.assertEqual(self.get('/v1/runs?before=0')[0], 400)
        self.assertEqual(self.get('/v1/experiments?before=-1')[0], 400)
        self.assertEqual(self.get('/v1/fixture-sets/core/versions/1?offset=-1')[0], 400)
        self.assertEqual(self.get('/v1/runs/' + 'a' * 32)[0], 404)
        self.assertEqual(self.get('/v1/experiments/' + 'a' * 32)[0], 404)

    def test_model_activity_uses_recent_contact_and_live_lease(self):
        now = [1000.0]
        self.store.clock = lambda: now[0]
        first = self.store.submit(': True', ['Init'], model='ollama/a', attempts=1)['run_id']
        second = self.store.submit(': True', ['Init'], model='ollama/b', attempts=1)['run_id']
        def activity():
            return self.get('/v1/model-activity?model=ollama%2Fa&model=ollama%2Fb&model=missing')[1]['items']
        self.assertEqual([row['status'] for row in activity()], ['unknown', 'unknown', 'unknown'])
        def claim(worker, model):
            payload = json.dumps({'worker_id': worker, 'models': [model]}).encode()
            with urlopen(Request(self.url + '/v1/claim', payload,
                                 {'Content-Type': 'application/json'}), timeout=10) as response:
                return json.load(response) if response.status == 200 else None
        a = claim('worker-a', 'ollama/a')
        b = claim('worker-b', 'ollama/b')
        rows = activity()
        self.assertEqual([row['status'] for row in rows], ['working', 'working', 'unknown'])
        self.assertEqual([(row['job_id'], row['run_id']) for row in rows[:2]],
                         [(a['job']['id'], first), (b['job']['id'], second)])
        self.assertNotIn('worker_id', json.dumps(rows))
        now[0] += 20
        payload = json.dumps({'lease_token': a['lease_token']}).encode()
        with urlopen(Request(self.url + '/v1/assignments/' + a['assignment_id'] + '/heartbeat', payload,
                             {'Content-Type': 'application/json'}), timeout=10):
            pass
        now[0] += 11
        self.assertEqual([row['status'] for row in activity()], ['working', 'offline', 'unknown'])
        now[0] += 20
        self.assertEqual([row['status'] for row in activity()], ['offline', 'offline', 'unknown'])
        # A claim with no available job is positive worker contact, but no assignment.
        self.assertIsNone(claim('worker-c', 'missing'))
        self.assertEqual([row['status'] for row in activity()], ['offline', 'offline', 'idle'])
        now[0] += 86401
        self.assertIsNone(claim('worker-c', 'missing'))
        self.assertEqual([row['status'] for row in activity()], ['offline', 'offline', 'idle'])
        self.assertEqual(self.get('/v1/model-activity?model=ollama%2Fa&model=ollama%2Fa')[0], 400)


if __name__ == '__main__':
    unittest.main()
