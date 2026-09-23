import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from solvenet.server import Coordinator, make_server
from solvenet.store import Conflict, Store, SCHEMA
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

    def test_migration_preserves_v1_history(self):
        path = Path(self.temp.name) / 'legacy.db'
        with sqlite3.connect(path) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO problems VALUES ('p', ': True', '[\"Init\"]')")
            db.execute("INSERT INTO runs VALUES ('r', 'p', 'solved')")
            db.execute("INSERT INTO jobs VALUES ('j', 'r', 'done', 'scripted', 2048, 3)")
            db.execute("INSERT INTO assignments VALUES ('a', 'j', 'w', 'token', 2000, 'completed', NULL)")
            db.execute("INSERT INTO attempts VALUES ('t', 'a', 'trivial', 'scripted', '{}')")
            db.execute("INSERT INTO verifications VALUES ('t', 'verified', '', 10)")
        migrated = Store(path)
        run = migrated.run('r')
        self.assertEqual(run['status'], 'solved')
        self.assertEqual(run['attempts'][0]['candidate'], 'trivial')
        self.assertEqual(run['attempts'][0]['generation'], {})
        with migrated.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)
        # Opening again must not repeat ALTER TABLE; new work must still function.
        migrated = Store(path, clock=lambda: self.now)
        new = migrated.submit(': True', ['Init'], attempts=1)['run_id']
        a = migrated.claim('w', ['scripted'])
        payload = self.payload(a)
        payload['generation'] = {'raw_response': '{"proof":"rfl"}', 'model': 'reported', 'eval_duration_ns': 100}
        payload['usage'] = {'input_tokens': 0, 'output_tokens': 12}
        migrated.result(a['assignment_id'], payload)
        migrated.result(a['assignment_id'], payload)
        saved = Store(path).run(new)['attempts']
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['generation'], payload['generation'])
        self.assertEqual(saved[0]['model'], 'scripted')
        self.assertEqual(saved[0]['usage'], payload['usage'])


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
        for invalid in (-1, 3, True, '2', None, 1.5):
            self.assertEqual(self.request('/v1/runs', {'statement': ': True', 'max_repairs': invalid})[0], 400)
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

    def test_failed_generation_metadata_validation_and_retention(self):
        _, run = self.request('/v1/runs', {'statement': ': True', 'attempts': 1})
        _, a = self.request('/v1/claim', {'worker_id': 'w', 'models': ['scripted']})
        route = '/v1/assignments/' + a['assignment_id'] + '/result'
        payload = {'lease_token': a['lease_token'], 'status': 'failed', 'error': 'Invalid proof JSON'}
        for generation in ([], {'raw_response': 3}, {'raw_response': 'x' * (128 * 1024 + 1)},
                           {'eval_duration_ns': -1}, {'eval_duration_ns': True},
                           {'raw_response_truncated': 'yes'}, {'model': 'x'*257}):
            with self.subTest(generation_type=type(generation)):
                self.assertEqual(self.request(route, {**payload, 'generation': generation})[0], 400)
        self.assertEqual(self.request(route, {**payload, 'usage': {'output_tokens': -1}})[0], 400)
        # Escaped Unicode can exceed the old 256 KiB HTTP envelope limit.
        payload['generation'] = {'raw_response': 'é' * 60000, 'model': 'reported', 'finish_reason': 'length'}
        payload['usage'] = {'output_tokens': 2048, 'input_tokens': None}
        self.assertEqual(self.request(route, payload)[0], 200)
        self.assertEqual(self.request(route, payload)[0], 200)
        outcome = self.request('/v1/runs/' + run['run_id'])[1]
        self.assertEqual(outcome['attempts'], [])
        self.assertEqual(outcome['jobs'][0]['status'], 'queued')
        self.assertEqual(outcome['assignments'][0]['generation'], payload['generation'])
        self.assertEqual(outcome['assignments'][0]['usage'], payload['usage'])

    @unittest.skipUnless(shutil.which('go'), 'Go required')
    def test_go_ollama_worker_success_and_format_failure(self):
        replies = [json.dumps({'proof': '```lean\nrfl\n```'}), json.dumps({'proof': 'refl'}), 'not valid JSON']

        class OllamaHandler(BaseHTTPRequestHandler):
            def do_POST(handler):
                self.assertEqual(handler.path, '/api/chat')
                request = json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
                self.assertEqual(request['model'], 'test:7b')
                self.assertEqual(request['options']['num_predict'], 64)
                body = json.dumps({'done': True, 'model': 'test:7b-reported', 'done_reason': 'stop',
                                   'message': {'content': replies.pop(0)}, 'prompt_eval_count': 52,
                                   'eval_count': 10, 'eval_duration': 2000000}).encode()
                handler.send_response(200)
                handler.send_header('Content-Length', str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

        ollama = ThreadingHTTPServer(('127.0.0.1', 0), OllamaHandler)
        thread = threading.Thread(target=ollama.serve_forever)
        thread.start()
        try:
            if shutil.which('lake'):
                self.coordinator.verifier = LeanVerifier(ROOT / 'lean')
            for expected in ('solved', 'exhausted', 'running'):
                _, run = self.request('/v1/runs', {'statement': '(n : Nat) : n + 0 = n', 'attempts': 1,
                                                 'model': 'ollama/test:7b', 'max_output_tokens': 64})
                subprocess.run(['go', 'run', './cmd/solvenet-worker', '-coordinator', self.url,
                                '-provider', 'ollama', '-model', 'test:7b', '-ollama-url',
                                f'http://127.0.0.1:{ollama.server_port}', '-once'],
                               cwd=ROOT / 'worker', timeout=120, check=True, capture_output=True)
                self.coordinator.tick()
                outcome = self.request('/v1/runs/' + run['run_id'])[1]
                self.assertEqual(outcome['status'], expected, outcome)
                assignment = outcome['assignments'][0]
                self.assertEqual(assignment['generation']['model'], 'test:7b-reported')
                self.assertEqual(assignment['usage']['output_tokens'], 10)
                if expected in ('solved', 'exhausted'):
                    attempt = outcome['attempts'][0]
                    self.assertEqual(attempt['candidate'], 'rfl' if expected == 'solved' else 'refl')
                    self.assertEqual(attempt['verification_status'], 'verified' if expected == 'solved' else 'rejected')
                    self.assertEqual(attempt['model'], 'ollama/test:7b')
                    self.assertEqual(attempt['generation']['eval_duration_ns'], 2000000)
                else:
                    self.assertEqual(assignment['generation']['raw_response'], 'not valid JSON')
                    self.assertIn('proof format', assignment['error'])
                    self.assertEqual(outcome['attempts'], [])
        finally:
            ollama.shutdown()
            thread.join()
            ollama.server_close()

    @unittest.skipUnless(shutil.which('go') and shutil.which('lake'), 'Go and Lake required')
    def test_go_ollama_repairs_with_real_lean_feedback(self):
        self.coordinator.verifier = LeanVerifier(ROOT / 'lean')
        _, run = self.request('/v1/runs', {'statement': '(n : Nat) : n + 0 = n', 'attempts': 1,
                                         'max_repairs': 2, 'model': 'ollama/test:7b', 'max_output_tokens': 64})
        replies = ['rw zero_add', 'rw [Nat.zero_add]', 'rfl']
        requests = []

        class OllamaHandler(BaseHTTPRequestHandler):
            def do_POST(handler):
                request = json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
                requests.append(request)
                body = json.dumps({'done': True, 'model': 'test:7b', 'done_reason': 'stop',
                                   'message': {'content': json.dumps({'proof': replies[len(requests)-1]})},
                                   'eval_count': 12}).encode()
                handler.send_response(200)
                handler.send_header('Content-Length', str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

        ollama = ThreadingHTTPServer(('127.0.0.1', 0), OllamaHandler)
        thread = threading.Thread(target=ollama.serve_forever)
        thread.start()
        try:
            previous = None
            for depth in range(3):
                subprocess.run(['go', 'run', './cmd/solvenet-worker', '-coordinator', self.url,
                                '-provider', 'ollama', '-model', 'test:7b', '-ollama-url',
                                f'http://127.0.0.1:{ollama.server_port}', '-once'],
                               cwd=ROOT / 'worker', timeout=120, check=True, capture_output=True)
                messages = requests[-1]['messages']
                if previous:
                    prompt = '\n'.join(m['content'] for m in messages)
                    self.assertIn(previous['candidate'], prompt)
                    self.assertIn(previous['diagnostics'], prompt)
                    self.assertIn(previous['candidate'], messages[-1]['content'])
                    self.assertIn(previous['diagnostics'], messages[-1]['content'])
                    self.assertIn('Do not repeat', messages[-1]['content'])
                self.coordinator.tick()
                current = self.request('/v1/runs/' + run['run_id'])[1]
                attempt = current['attempts'][-1]
                self.assertEqual(attempt['repair_depth'], depth)
                self.assertEqual(attempt['parent_attempt_id'], previous['id'] if previous else None)
                self.assertEqual(attempt['verification_status'], 'verified' if depth == 2 else 'rejected')
                self.assertEqual(requests[-1]['options']['num_predict'], 64)
                previous = attempt
            self.assertEqual(current['status'], 'solved')
            self.assertEqual(len(current['jobs']), 3)
            self.assertEqual(current['max_repairs'], 2)
            self.assertIsNone(self.store.claim('extra', ['ollama/test:7b']))
        finally:
            ollama.shutdown()
            thread.join()
            ollama.server_close()

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
