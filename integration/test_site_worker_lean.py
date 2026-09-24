"""Process-level site → coordinator → Ollama worker → pinned Lean contract."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from solvenet.server import Coordinator, make_server as coordinator_server
from solvenet.problem_set import load
from solvenet.store import Store
from solvenet.verifier import LeanVerifier
from solvenet_homelab.server import make_server as site_server
from solvenet_homelab.settings import Settings


ROOT = Path(__file__).resolve().parents[1]
MODEL = 'ollama/integration:latest'
PROBLEM = '/problems/core/1/nat-add-right-zero'


@contextmanager
def serving(server):
    server.RequestHandlerClass.log_message = lambda self, *args: None
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        if thread.is_alive():
            raise RuntimeError('HTTP server did not stop')


def get(url):
    with urlopen(url, timeout=3) as response:
        return response.read().decode()


def until(description, callback, seconds=20):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        last = callback()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f'Timed out waiting for {description}; last observation: {str(last)[:800]}')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class SiteWorkerLeanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for command in ('go', 'lake'):
            if not shutil.which(command):
                raise RuntimeError(f'IT1 setup: {command} is required on PATH (pinned Lean/Lake and Go required)')
        cls.build_dir = tempfile.TemporaryDirectory(prefix='solvenet-it1-build-')
        cls.worker = str(Path(cls.build_dir.name) / 'solvenet-worker')
        try:
            subprocess.run(['go', 'build', '-o', cls.worker, './cmd/solvenet-worker'],
                           cwd=ROOT / 'worker', timeout=120, check=True, capture_output=True)
            readiness = LeanVerifier(ROOT / 'lean').readiness()
            if not readiness.ready:
                raise RuntimeError(f'IT1 setup: pinned local Lean is unavailable: {readiness.diagnostics[:1000]}')
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            cls.build_dir.cleanup()
            raise RuntimeError(f'IT1 setup: could not build Go worker: {error}') from error
        except Exception:
            cls.build_dir.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.build_dir.cleanup()

    def test_site_worker_real_lean(self):
        for proof, expected in (('exact Nat.add_zero n', 'verified'),
                                ('exact Nat.add_comm n n', 'rejected')):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory(prefix='solvenet-it1-') as directory:
                self.run_case(Path(directory), proof, expected)

    def run_case(self, directory, proof, expected):
        site_db, coordinator_db = directory / 'site.db', directory / 'coordinator.db'
        self.assertNotEqual(site_db.resolve(), coordinator_db.resolve())
        requests = []
        claims = []
        fixture = next(p for p in load().problems if p.id == 'nat-add-right-zero')

        class Ollama(BaseHTTPRequestHandler):
            def do_GET(handler):
                if handler.path not in ('/api/tags', '/api/show'):
                    handler.send_error(404)
                    return
                body = b'{"models":[{"name":"integration:latest"}]}'
                handler.send_response(200)
                handler.send_header('Content-Length', str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

            def do_POST(handler):
                body = handler.rfile.read(int(handler.headers['Content-Length']))
                requests.append((handler.path, body))
                if handler.path == '/api/show':
                    reply = b'{}'
                elif handler.path == '/api/chat':
                    reply = json.dumps({'done': True, 'model': 'integration:latest',
                                        'done_reason': 'stop', 'message': {'content': json.dumps({'proof': proof})},
                                        'prompt_eval_count': 17, 'eval_count': 9}).encode()
                else:
                    handler.send_error(404)
                    return
                handler.send_response(200)
                handler.send_header('Content-Type', 'application/json')
                handler.send_header('Content-Length', str(len(reply)))
                handler.end_headers()
                handler.wfile.write(reply)

            def log_message(self, *args):
                pass

        store = Store(coordinator_db)
        original_claim = store.claim

        def record_claim(*args, **kwargs):
            response = original_claim(*args, **kwargs)
            if response is not None:
                claims.append(response)  # Exactly the object serialized to the claiming worker.
            return response

        store.claim = record_claim
        coordinator = Coordinator(store, LeanVerifier(ROOT / 'lean'))
        stop = threading.Event()
        with serving(coordinator_server(coordinator, ('127.0.0.1', 0))) as coordinator_url, \
                serving(ThreadingHTTPServer(('127.0.0.1', 0), Ollama)) as ollama_url:
            scheduler = threading.Thread(target=coordinator.loop, args=(stop,))
            scheduler.start()
            settings = Settings(site_db)
            settings.select(coordinator_url)
            settings.add_model(MODEL, 'Integration model', 'Ollama', 'On this device')
            try:
                with serving(site_server(settings, ('127.0.0.1', 0))) as site_url:
                    with (directory / 'worker.log').open('w+') as log:
                        worker = subprocess.Popen([self.worker, '-coordinator', coordinator_url,
                                                   '-provider', 'ollama', '-model', 'integration:latest',
                                                   '-ollama-url', ollama_url], cwd=ROOT / 'worker',
                                                  stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                                  env={**os.environ, 'OPENAI_API_KEY': '', 'OPENAI_API_KEY_FILE': ''})
                        run_id = None
                        try:
                            def available():
                                self.assertIsNone(worker.poll(), 'worker exited before model became available')
                                with urlopen(coordinator_url + '/v1/model-activity?model=' + MODEL, timeout=3) as response:
                                    items = json.load(response)['items']
                                return items and items[0].get('ready') is True

                            until('worker model health signal', available, 12)
                            listing = get(site_url + '/problems')
                            self.assertIn('Addition by zero on the right', listing)
                            self.assertIn(PROBLEM, listing)
                            with urlopen(site_url + PROBLEM, timeout=3) as response:
                                detail = response.read().decode()
                                cookie = response.headers.get('Set-Cookie', '').split(';', 1)[0]
                            token = re.search(r'name="csrf" value="([0-9a-f]+)"', detail)
                            self.assertIsNotNone(token, detail[:800])
                            self.assertIn(f'value="{MODEL}"', detail)
                            form = urlencode({'csrf': token.group(1), 'model': MODEL,
                                              'strategy': 'independent', 'attempts': 1,
                                              'max_output_tokens': 64, 'generation_timeout_seconds': 30}).encode()
                            request = Request(site_url + PROBLEM, data=form, headers={
                                'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': cookie})
                            opener = build_opener(NoRedirect)
                            try:
                                response = opener.open(request, timeout=3)
                            except HTTPError as error:
                                if error.code != 303:
                                    raise
                                response = error
                            with response:
                                self.assertEqual(response.status, 303)
                                location = response.headers['Location']
                            self.assertRegex(location, r'^/runs/[0-9a-f]{32}$')
                            run_id = location.rsplit('/', 1)[-1]

                            def finished():
                                self.assertIsNone(worker.poll(), 'worker exited before run completed')
                                with urlopen(coordinator_url + '/v1/runs/' + run_id, timeout=3) as response:
                                    state = json.load(response)
                                return state if state['status'] != 'running' else None

                            outcome = until(f'run {run_id} terminal status', finished, 25)
                            self.assertEqual(outcome['status'], 'solved' if expected == 'verified' else 'exhausted', outcome)
                            self.assertEqual((outcome['fixture_set_id'], outcome['fixture_version'],
                                              outcome['fixture_problem_id']), ('core', 1, 'nat-add-right-zero'))
                            self.assertEqual(outcome['jobs'][0]['model'], MODEL)
                            self.assertEqual(len(outcome['assignments']), 1, outcome)
                            self.assertEqual(outcome['assignments'][0]['status'], 'completed', outcome)
                            self.assertEqual(len(outcome['attempts']), 1, outcome)
                            attempt = outcome['attempts'][0]
                            self.assertEqual(attempt['candidate'], proof)
                            self.assertEqual(attempt['model'], MODEL)
                            self.assertEqual(attempt['verification_status'], expected, outcome)
                            self.assertEqual(attempt['usage'], {'input_tokens': 17, 'output_tokens': 9})
                            self.assertEqual(len(claims), 1)
                            self.assertEqual(claims[0]['job']['statement'], fixture.statement)
                            self.assertNotIn('reference_proof', json.dumps(claims[0]))
                            self.assertNotIn(fixture.reference_proof, json.dumps(claims[0]))
                            chat = [json.loads(body) for path, body in requests if path == '/api/chat']
                            self.assertEqual(len(chat), 1, requests)
                            self.assertEqual(chat[0]['model'], 'integration:latest')
                            self.assertIs(chat[0]['stream'], True)
                            self.assertEqual(chat[0]['format']['required'], ['proof'])
                            self.assertIs(chat[0]['format']['additionalProperties'], False)
                            self.assertEqual(chat[0]['format']['properties']['proof']['type'], 'string')
                            self.assertEqual(chat[0]['options']['num_predict'], 64)
                            self.assertEqual([message['role'] for message in chat[0]['messages'][:2]],
                                             ['system', 'user'])
                            self.assertIn(fixture.statement, chat[0]['messages'][1]['content'])
                            self.assertIn('JSON', chat[0]['messages'][0]['content'])
                            self.assertNotIn('reference_proof', json.dumps(chat[0]))
                            self.assertNotIn(fixture.reference_proof, json.dumps(chat[0]))
                            page = get(site_url + location)
                            self.assertIn('Run status: ' + outcome['status'], page)
                            self.assertIn('candidate(s) rejected by Lean' if expected == 'rejected'
                                          else 'Lean verified this candidate', page)
                            self.assertIn(proof, page)
                            self.assertIn('Input tokens</dt><dd>17', page)
                            self.assertIn('Output tokens</dt><dd>9', page)
                        except Exception as error:
                            log.flush()
                            log.seek(0)
                            status = None
                            if run_id:
                                try:
                                    with urlopen(coordinator_url + '/v1/runs/' + run_id, timeout=3) as response:
                                        status = json.load(response)
                                except Exception as lookup_error:
                                    status = f'could not fetch status: {lookup_error}'
                            self.fail(f'IT1 {expected} run={run_id} status={str(status)[:1500]} '
                                      f'worker_exit={worker.poll()} worker_log={log.read()[-3000:]!r}: {error}')
                        finally:
                            worker.terminate()
                            try:
                                worker.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                worker.kill()
                                worker.wait(timeout=5)
            finally:
                stop.set()
                scheduler.join(timeout=15)
                if scheduler.is_alive():
                    raise RuntimeError('coordinator scheduler did not stop')
