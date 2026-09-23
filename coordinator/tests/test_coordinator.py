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
from solvenet.store import MAX_REPAIRS, Conflict, Store, SCHEMA
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

    def test_transient_failure_retries_and_is_exposed(self):
        a = self.store.claim('a', ['scripted'])
        self.store.result(a['assignment_id'], {
            'lease_token': a['lease_token'], 'status': 'failed',
            'failure_class': 'transient', 'error': 'service unavailable'})
        run = self.store.run(self.run)
        self.assertEqual(run['status'], 'running')
        self.assertEqual(run['jobs'][0]['status'], 'queued')
        self.assertEqual(run['assignments'][0]['failure_class'], 'transient')
        self.assertIsNotNone(self.store.claim('b', ['scripted']))

    def test_permanent_failure_terminates_job_immediately(self):
        a = self.store.claim('a', ['scripted'])
        self.store.result(a['assignment_id'], {
            'lease_token': a['lease_token'], 'status': 'failed',
            'failure_class': 'permanent', 'error': 'invalid worker configuration'})
        run = self.store.run(self.run)
        self.assertEqual(run['status'], 'exhausted')
        self.assertEqual(run['jobs'][0]['status'], 'failed')
        self.assertEqual(run['assignments'][0]['failure_class'], 'permanent')
        self.assertIsNone(self.store.claim('b', ['scripted']))

    def test_assignment_rejection_promptly_requeues_without_spending_budget(self):
        run_id = self.store.submit(
            ': True', ['Init'], attempts=1, model='reject-test', max_assignments=1)['run_id']
        first = self.store.claim('incompatible', ['reject-test'])
        self.store.result(first['assignment_id'], {
            'lease_token': first['lease_token'],
            'status': 'rejected',
            'rejection_kind': 'unsupported_protocol',
            'error': 'unsupported protocol_version 2',
        })

        # Recovery is immediate: the lease has not elapsed, and a one-assignment
        # budget remains because provider execution never began.
        second = self.store.claim('compatible', ['reject-test'])
        self.assertIsNotNone(second)
        self.assertEqual(second['job']['id'], first['job']['id'])
        run = self.store.run(run_id)
        self.assertEqual(run['jobs'][0]['status'], 'assigned')
        self.assertEqual(run['assignments'][0]['status'], 'rejected')
        self.assertEqual(
            run['assignments'][0]['rejection_kind'], 'unsupported_protocol')

    def test_assignment_rejection_requires_lease_token(self):
        first = self.store.claim('worker', ['scripted'])
        payload = {
            'lease_token': 'wrong',
            'status': 'rejected',
            'rejection_kind': 'malformed_assignment',
            'error': 'job.statement is missing',
        }
        with self.assertRaises(Conflict):
            self.store.result(first['assignment_id'], payload)
        self.assertIsNone(self.store.claim('other', ['scripted']))
        run = self.store.run(self.run)
        self.assertEqual(run['assignments'][0]['status'], 'active')
        self.assertEqual(run['jobs'][0]['status'], 'assigned')

    def test_old_worker_failure_defaults_to_transient(self):
        a = self.store.claim('a', ['scripted'])
        payload = {'lease_token': a['lease_token'], 'status': 'failed',
                   'error': 'legacy failure'}
        self.assertEqual(self.store.result(a['assignment_id'], payload), {'accepted': True})
        # Simulate a result persisted by a pre-classification coordinator.
        with self.store.transaction() as db:
            legacy = json.dumps(payload, sort_keys=True, separators=(',', ':'))
            db.execute("UPDATE assignments SET result=? WHERE id=?",
                       (legacy, a['assignment_id']))
        self.assertEqual(self.store.result(a['assignment_id'], payload), {'accepted': True})
        run = self.store.run(self.run)
        self.assertEqual(run['jobs'][0]['status'], 'queued')
        self.assertEqual(run['assignments'][0]['failure_class'], 'transient')
        with self.store.connect() as db:
            persisted = json.loads(db.execute(
                "SELECT result FROM assignments WHERE id=?", (a['assignment_id'],)).fetchone()[0])
        self.assertEqual(persisted['failure_class'], 'transient')

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

    def test_verified_proof_has_precedence_in_both_delivery_orders(self):
        for first, second in (
                (VerificationStatus.VERIFIER_ERROR, VerificationStatus.VERIFIED),
                (VerificationStatus.VERIFIED, VerificationStatus.VERIFIER_ERROR)):
            with self.subTest(first=first, second=second):
                run_id = self.store.submit(
                    ': True', ['Init'], attempts=2, model='terminal-order')['run_id']
                assignments = [
                    self.store.claim('worker', ['terminal-order']) for _ in range(2)]
                attempt_ids = []
                for assignment in assignments:
                    self.store.result(assignment['assignment_id'], self.payload(assignment))
                    attempt_ids.append(next(
                        attempt['id'] for attempt in self.store.run(run_id)['attempts']
                        if attempt['job_id'] == assignment['job']['id']))

                self.store.verified(
                    attempt_ids[0], VerificationResult(first, 'first outcome', 1))
                expected_first = 'solved' if first is VerificationStatus.VERIFIED else 'error'
                self.assertEqual(self.store.run(run_id)['status'], expected_first)
                self.store.verified(
                    attempt_ids[1], VerificationResult(second, 'second outcome', 1))

                run = self.store.run(run_id)
                self.assertEqual(run['status'], 'solved')
                self.assertCountEqual(
                    [attempt['verification_status'] for attempt in run['attempts']],
                    ['verified', 'verifier_error'])

    def test_already_assigned_job_finishes_after_terminal_state(self):
        for terminal, late, expected in (
                (VerificationStatus.VERIFIER_ERROR, VerificationStatus.VERIFIED, 'solved'),
                (VerificationStatus.VERIFIED, VerificationStatus.VERIFIER_ERROR, 'solved')):
            with self.subTest(terminal=terminal, late=late):
                run_id = self.store.submit(
                    ': True', ['Init'], attempts=2, model='late-terminal')['run_id']
                first = self.store.claim('first', ['late-terminal'])
                late_assignment = self.store.claim('late', ['late-terminal'])

                self.store.result(first['assignment_id'], self.payload(first))
                first_attempt = next(
                    attempt['id'] for attempt in self.store.run(run_id)['attempts']
                    if attempt['job_id'] == first['job']['id'])
                self.store.verified(
                    first_attempt, VerificationResult(terminal, 'terminal outcome', 1))

                # Work claimed before the terminal transition remains valid.
                self.assertEqual(
                    self.store.result(late_assignment['assignment_id'], self.payload(late_assignment)),
                    {'accepted': True})
                late_attempt = next(
                    attempt['id'] for attempt in self.store.run(run_id)['attempts']
                    if attempt['job_id'] == late_assignment['job']['id'])
                self.store.verified(late_attempt, VerificationResult(late, 'late outcome', 1))

                run = self.store.run(run_id)
                self.assertEqual(run['status'], expected)
                self.assertEqual([job['status'] for job in run['jobs']], ['done', 'done'])

    def test_concurrent_terminal_outcomes_and_duplicate_delivery_are_stable(self):
        run_id = self.store.submit(
            ': True', ['Init'], attempts=2, model='concurrent-terminal')['run_id']
        assignments = [
            self.store.claim('worker', ['concurrent-terminal']) for _ in range(2)]
        for assignment in assignments:
            self.store.result(assignment['assignment_id'], self.payload(assignment))
        attempts = self.store.run(run_id)['attempts']
        outcomes = (
            VerificationResult(VerificationStatus.VERIFIED, 'valid proof', 1),
            VerificationResult(VerificationStatus.VERIFIER_ERROR, 'broken verifier', 1),
        )
        deliveries = list(zip([attempt['id'] for attempt in attempts], outcomes))

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda delivery: self.store.verified(*delivery), deliveries * 2))

        run = self.store.run(run_id)
        self.assertEqual(run['status'], 'solved')
        self.assertCountEqual(
            [attempt['verification_status'] for attempt in run['attempts']],
            ['verified', 'verifier_error'])

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
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 6)
        self.assertEqual(run['generation_timeout_seconds'], 120)
        self.assertEqual(run['max_assignments'], 3)
        self.assertEqual(run['jobs'][0]['generation_timeout_seconds'], 120)
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

    def test_generation_timeout_is_persisted_across_restart(self):
        # Leave the default job assigned so the custom queued job is claimed next.
        self.store.claim('other-worker', ['scripted'])
        run_id = self.store.submit(': True', ['Init'], attempts=1,
                                   generation_timeout_seconds=321)['run_id']
        restarted = Store(self.path, clock=lambda: self.now)
        claim = restarted.claim('worker', ['scripted'])
        self.assertEqual(claim['job']['timeout_seconds'], 321)
        run = restarted.run(run_id)
        self.assertEqual(run['generation_timeout_seconds'], 321)
        self.assertEqual(run['jobs'][0]['generation_timeout_seconds'], 321)

    def test_custom_assignment_limit_is_persisted_across_restart(self):
        # Leave the default job assigned so the custom queued job is claimed next.
        self.store.claim('other-worker', ['scripted'])
        run_id = self.store.submit(': True', ['Init'], attempts=1, max_assignments=2)['run_id']
        restarted = Store(self.path, clock=lambda: self.now)
        claim = restarted.claim('worker', ['scripted'])
        restarted.result(claim['assignment_id'], {
            'lease_token': claim['lease_token'], 'status': 'failed',
            'error': 'provider unavailable'})
        restarted = Store(self.path, clock=lambda: self.now)
        claim = restarted.claim('worker', ['scripted'])
        restarted.result(claim['assignment_id'], {
            'lease_token': claim['lease_token'], 'status': 'failed',
            'error': 'provider unavailable'})
        self.assertIsNone(restarted.claim('worker', ['scripted']))
        run = Store(self.path).run(run_id)
        self.assertEqual(run['max_assignments'], 2)
        self.assertEqual(run['jobs'][0]['max_assignments'], 2)
        self.assertEqual(len(run['assignments']), 2)
        self.assertEqual(run['status'], 'exhausted')


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
        for invalid in (-1, MAX_REPAIRS + 1, True, '2', None, 1.5):
            self.assertEqual(self.request('/v1/runs', {'statement': ': True', 'max_repairs': invalid})[0], 400)
        for invalid in (0, 86401, True, '120', None, 1.5):
            code, error = self.request('/v1/runs', {
                'statement': ': True', 'generation_timeout_seconds': invalid})
            self.assertEqual(code, 400)
            self.assertIn('generation_timeout_seconds', error['error'])
        for invalid in (0, 101, True, '3', None, 1.5):
            code, error = self.request('/v1/runs', {
                'statement': ': True', 'max_assignments': invalid})
            self.assertEqual(code, 400)
            self.assertIn('max_assignments', error['error'])
        code, run = self.request('/v1/runs', {'statement': '(n : Nat) : n + 0 = n',
                                             'attempts': 1, 'generation_timeout_seconds': 321,
                                             'max_assignments': 2})
        self.assertEqual(code, 201)
        self.assertEqual(self.request('/v1/claim', {'worker_id': 'w', 'models': ['unknown']})[0], 204)
        _, a = self.request('/v1/claim', {'worker_id': 'w', 'models': ['scripted']})
        self.assertEqual(a['job']['timeout_seconds'], 321)
        route = '/v1/assignments/' + a['assignment_id']
        self.assertEqual(self.request(route + '/heartbeat', {'lease_token': a['lease_token']})[0], 200)
        payload = {'lease_token': a['lease_token'], 'status': 'completed', 'output': {'text': 'rfl'}}
        self.assertEqual(self.request(route + '/result', payload)[0], 200)
        self.assertEqual(self.request(route + '/result', payload)[0], 200)
        self.coordinator.tick()
        outcome = self.request('/v1/runs/' + run['run_id'])[1]
        self.assertEqual(outcome['status'], 'solved')
        self.assertEqual(outcome['generation_timeout_seconds'], 321)
        self.assertEqual(outcome['max_assignments'], 2)
        self.assertEqual(outcome['jobs'][0]['generation_timeout_seconds'], 321)
        self.assertEqual(outcome['jobs'][0]['max_assignments'], 2)

    def test_max_repairs_api_upper_bound_is_accepted(self):
        code, submitted = self.request(
            '/v1/runs', {'statement': ': True', 'max_repairs': MAX_REPAIRS})
        self.assertEqual(code, 201)
        outcome = self.request('/v1/runs/' + submitted['run_id'])[1]
        self.assertEqual(outcome['max_repairs'], MAX_REPAIRS)

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
        self.assertEqual(outcome['assignments'][0]['failure_class'], 'transient')

    def test_failure_class_validation(self):
        _, run = self.request('/v1/runs', {'statement': ': True', 'attempts': 1})
        _, a = self.request('/v1/claim', {'worker_id': 'w', 'models': ['scripted']})
        route = '/v1/assignments/' + a['assignment_id'] + '/result'
        base = {'lease_token': a['lease_token'], 'status': 'failed', 'error': 'bad config'}
        for invalid in ('retryable', '', None, 1, True):
            self.assertEqual(self.request(route, {**base, 'failure_class': invalid})[0], 400)
        self.assertEqual(self.request(route, {**base, 'failure_class': 'permanent'})[0], 200)
        outcome = self.request('/v1/runs/' + run['run_id'])[1]
        self.assertEqual(outcome['status'], 'exhausted')
        self.assertEqual(outcome['assignments'][0]['failure_class'], 'permanent')

    def test_rejection_validation_and_token_authentication(self):
        _, run = self.request('/v1/runs', {
            'statement': ': True', 'attempts': 1, 'max_assignments': 1})
        _, assignment = self.request(
            '/v1/claim', {'worker_id': 'w', 'models': ['scripted']})
        route = '/v1/assignments/' + assignment['assignment_id'] + '/result'
        base = {
            'status': 'rejected',
            'error': 'job.kind must be model.generate',
            'rejection_kind': 'malformed_assignment',
        }
        self.assertEqual(self.request(route, {
            **base, 'lease_token': assignment['lease_token'],
            'rejection_kind': 'other'})[0], 400)
        self.assertEqual(self.request(route, {
            **base, 'lease_token': 'wrong'})[0], 409)
        self.assertEqual(self.request(route, {
            **base, 'lease_token': assignment['lease_token']})[0], 200)
        self.assertEqual(self.request(route, {
            **base, 'lease_token': assignment['lease_token']})[0], 200)

        _, replacement = self.request(
            '/v1/claim', {'worker_id': 'replacement', 'models': ['scripted']})
        self.assertEqual(replacement['job']['id'], assignment['job']['id'])
        outcome = self.request('/v1/runs/' + run['run_id'])[1]
        self.assertEqual(outcome['assignments'][0]['status'], 'rejected')
        self.assertEqual(
            outcome['assignments'][0]['rejection_kind'], 'malformed_assignment')

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
            for index, expected in enumerate(('solved', 'exhausted', 'exhausted')):
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
                if index < 2:
                    attempt = outcome['attempts'][0]
                    self.assertEqual(attempt['candidate'], 'rfl' if expected == 'solved' else 'refl')
                    self.assertEqual(attempt['verification_status'], 'verified' if expected == 'solved' else 'rejected')
                    self.assertEqual(attempt['model'], 'ollama/test:7b')
                    self.assertEqual(attempt['generation']['eval_duration_ns'], 2000000)
                else:
                    self.assertEqual(assignment['generation']['raw_response'], 'not valid JSON')
                    self.assertIn('proof format', assignment['error'])
                    self.assertEqual(assignment['failure_class'], 'permanent')
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
