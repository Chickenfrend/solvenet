import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from solvenet.server import Coordinator, make_server
from solvenet.store import Conflict, Store
from solvenet.verifier import LeanVerifier, VerificationResult, VerificationStatus

ROOT = Path(__file__).resolve().parents[2]


class FakeVerifier:
    def verify(self, statement, candidate, *, imports):
        status = VerificationStatus.VERIFIED if candidate == 'rfl' else VerificationStatus.REJECTED
        return VerificationResult(status, 'scripted test verifier', 0)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.db'
        self.now = 1000
        self.store = Store(self.path, clock=lambda: self.now, lease_seconds=3)
        self.run = self.store.submit('(n : Nat) : n + 0 = n', ['Init'], attempts=1)['run_id']

    def payload(self, assignment, proof='rfl'):
        return {'lease_token': assignment['lease_token'], 'status': 'completed', 'output': {'text': proof}}

    def test_restart_and_duplicate_result(self):
        a = self.store.claim('worker', ['scripted'])
        restarted = Store(self.path, clock=lambda: self.now)
        payload = self.payload(a)
        self.assertEqual(restarted.result(a['assignment_id'], payload), {'accepted': True})
        self.assertEqual(restarted.result(a['assignment_id'], payload), {'accepted': True})
        with self.assertRaises(Conflict):
            restarted.result(a['assignment_id'], self.payload(a, 'trivial'))
        self.assertEqual(len(restarted.run(self.run)['attempts']), 1)
        Coordinator(restarted, FakeVerifier()).tick()
        self.assertEqual(Store(self.path).run(self.run)['status'], 'solved')

    def test_atomic_claim(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda i: self.store.claim(str(i), ['scripted']), range(8)))
        self.assertEqual(sum(a is not None for a in claims), 1)

    def test_expiry_and_stale_result(self):
        a = self.store.claim('a', ['scripted'])
        self.now += 4
        b = self.store.claim('b', ['scripted'])
        self.assertEqual(a['job']['id'], b['job']['id'])
        self.assertNotEqual(a['lease_token'], b['lease_token'])
        with self.assertRaises(Conflict):
            self.store.result(a['assignment_id'], self.payload(a))
        self.store.result(b['assignment_id'], self.payload(b))

    def test_heartbeat_and_token(self):
        a = self.store.claim('a', ['scripted'])
        with self.assertRaises(Conflict):
            self.store.heartbeat(a['assignment_id'], 'wrong')
        self.now += 2
        self.store.heartbeat(a['assignment_id'], a['lease_token'])
        self.now += 2
        self.assertIsNone(self.store.claim('b', ['scripted']))
        self.now += 2
        self.assertIsNotNone(self.store.claim('b', ['scripted']))

    def test_retry_limit(self):
        for _ in range(3):
            a = self.store.claim('a', ['scripted'])
            self.store.result(a['assignment_id'], {'lease_token': a['lease_token'], 'status': 'failed', 'error': 'provider unavailable'})
        self.assertIsNone(self.store.claim('a', ['scripted']))
        self.assertEqual(self.store.run(self.run)['status'], 'exhausted')

    def test_rejected_proof_is_not_retried(self):
        a = self.store.claim('a', ['scripted'])
        self.store.result(a['assignment_id'], self.payload(a, 'bad'))
        Coordinator(self.store, FakeVerifier()).tick()
        self.assertEqual(self.store.run(self.run)['status'], 'exhausted')

    def test_verifier_error_stops_run(self):
        a = self.store.claim('a', ['scripted'])
        self.store.result(a['assignment_id'], self.payload(a))
        self.store.verified(self.store.pending()['id'], VerificationResult(VerificationStatus.VERIFIER_ERROR, 'broken environment', 0))
        self.assertEqual(self.store.run(self.run)['status'], 'error')

    def test_solved_run_cancels_queued_jobs(self):
        other = self.store.submit(': True', ['Init'], attempts=3)['run_id']
        first = self.store.claim('a', ['scripted'])
        self.store.result(first['assignment_id'], self.payload(first))
        Coordinator(self.store, FakeVerifier()).tick()
        a = self.store.claim('a', ['scripted'])
        self.store.result(a['assignment_id'], self.payload(a))
        Coordinator(self.store, FakeVerifier()).tick()
        run = self.store.run(other)
        self.assertEqual(run['status'], 'solved')
        self.assertEqual([j['status'] for j in run['jobs']].count('cancelled'), 2)


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'state.db')
        self.coordinator = Coordinator(self.store, FakeVerifier())
        self.server = make_server(self.coordinator, ('127.0.0.1', 0))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path, data=None):
        req = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                      headers={'Content-Type': 'application/json'})
        try:
            response = urlopen(req, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None

    def test_http_lifecycle_and_validation(self):
        self.assertEqual(self.request('/v1/runs', {'statement': ': True', 'attempts': True})[0], 400)
        code, run = self.request('/v1/runs', {'statement': '(n : Nat) : n + 0 = n', 'attempts': 1})
        self.assertEqual(code, 201)
        self.assertEqual(self.request('/v1/claim', {'worker_id': 'w', 'models': ['unknown']})[0], 204)
        _, a = self.request('/v1/claim', {'worker_id': 'w', 'models': ['scripted']})
        route = '/v1/assignments/' + a['assignment_id']
        self.assertEqual(self.request(route + '/heartbeat', {'lease_token': a['lease_token']})[0], 200)
        payload = {'lease_token': a['lease_token'], 'status': 'completed', 'output': {'text': 'rfl'}}
        self.assertEqual(self.request(route + '/result', payload)[0], 200)
        self.assertEqual(self.request(route + '/result', payload)[0], 200)
        self.coordinator.tick()
        self.assertEqual(self.request('/v1/runs/' + run['run_id'])[1]['status'], 'solved')

    @unittest.skipUnless(shutil.which('go') and shutil.which('lake'), 'Go and Lake required')
    def test_go_worker_to_real_lean(self):
        self.coordinator.verifier = LeanVerifier(ROOT / 'lean')
        _, run = self.request('/v1/runs', {'statement': '(n : Nat) : n + 0 = n', 'attempts': 1})
        subprocess.run(['go', 'run', './cmd/solvenet-worker', '-coordinator', self.url, '-once'],
                       cwd=ROOT / 'worker', timeout=120, check=True, capture_output=True)
        self.coordinator.tick()
        outcome = self.request('/v1/runs/' + run['run_id'])[1]
        self.assertEqual(outcome['status'], 'solved', outcome)
        self.assertEqual(outcome['attempts'][0]['verification_status'], 'verified')


if __name__ == '__main__':
    unittest.main()
