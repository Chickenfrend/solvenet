"""Same compiled daemon, real coordinator claims, synthetic loopback credentials."""

import json
import os
import subprocess
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from solvenet.server import Coordinator, make_server
from solvenet.store import Store
from solvenet.verifier import LeanVerifier
import test_group_collaboration as fixed
from test_site_worker_lean import ROOT, serving, until


class OpenAIRecoveryTests(unittest.TestCase):
    setUpClass = classmethod(fixed.GroupCollaborationTests.setUpClass.__func__)
    tearDownClass = classmethod(fixed.GroupCollaborationTests.tearDownClass.__func__)

    def test_same_daemon_recovers_through_coordinator_claims(self):
        for profile in ('chat-json', 'responses-reasoning'):
            for failure in ('429', 'malformed', 'duplicate-proof', 'admission'):
                with self.subTest(profile=profile, failure=failure), tempfile.TemporaryDirectory() as directory:
                    store = Store(Path(directory) / 'state.db')
                    coordinator = Coordinator(store, LeanVerifier(ROOT / 'lean'))
                    calls = []

                    class Provider(BaseHTTPRequestHandler):
                        def do_POST(self):
                            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                            calls.append(body)
                            if len(calls) == 1 and failure == '429':
                                self.send_response(429)
                                self.end_headers()
                                return
                            raw = json.dumps({'proof': r'exact (show "\n" = "\n" from rfl)'})
                            if len(calls) == 1 and failure == 'malformed':
                                raw = '{"proof":'
                            if len(calls) == 1 and failure == 'duplicate-proof':
                                raw = r'{"proof":"synthetic-recovery-key","proof":"rfl"}'
                            if profile == 'chat-json':
                                reply = dict(choices=[dict(finish_reason='stop', message=dict(content=raw))])
                            else:
                                reply = dict(status='completed', output=[dict(type='message', role='assistant',
                                    status='completed', content=[dict(type='output_text', text=raw)])])
                            self.send_response(200)
                            self.end_headers()
                            self.wfile.write(json.dumps(reply).encode())

                    first = store.submit(r': "\n" = "\n"', ['Init'], attempts=1,
                        model='openai/fixture', max_output_tokens=1024 if failure == 'admission' else 128,
                        max_assignments=2)['run_id']
                    with serving(make_server(coordinator, ('127.0.0.1', 0))) as url, \
                            serving(ThreadingHTTPServer(('127.0.0.1', 0), Provider)) as provider:
                        process = subprocess.Popen([self.worker, '-coordinator', url, '-id', 'recovery',
                            '-provider', 'openai', '-model', 'fixture', '-openai-profile', profile,
                            '-openai-url', provider, '-openai-max-output', '512'],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env={**os.environ, 'OPENAI_API_KEY': 'synthetic-recovery-key', 'OPENAI_API_KEY_FILE': ''})
                        try:
                            def terminal(run):
                                coordinator.tick()
                                state = store.run(run)
                                return state if state['status'] != 'running' else None
                            initial = until('first run terminal', lambda: terminal(first))
                            self.assertEqual(initial['status'], 'solved' if failure == '429' else 'exhausted')
                            self.assertEqual(len(calls), {'429': 2, 'malformed': 1, 'duplicate-proof': 1, 'admission': 0}[failure])
                            if failure == 'duplicate-proof':
                                self.assertEqual(initial['assignments'][0]['generation']['raw_response'],
                                    '[raw output unavailable: malformed or opaque JSON]')
                                self.assertNotIn('synthetic-recovery-key', json.dumps(initial))
                            # Enqueue after the failure: eligibility belongs to this same
                            # live process, rather than being reset by a new -once worker.
                            second = store.submit(r': "\n" = "\n"', ['Init'], attempts=1,
                                model='openai/fixture', max_output_tokens=128)['run_id']
                            recovered = until('unrelated run verified', lambda: terminal(second))
                            self.assertEqual(recovered['status'], 'solved')
                            self.assertEqual(recovered['attempts'][0]['candidate'], r'exact (show "\n" = "\n" from rfl)')
                            self.assertEqual(len(calls), {'429': 3, 'malformed': 2, 'duplicate-proof': 2, 'admission': 1}[failure])
                            self.assertIsNone(process.poll())
                        except AssertionError as error:
                            self.fail(f'{error}; calls={len(calls)} state={store.run(first)}')
                        finally:
                            process.terminate()
                            process.communicate(timeout=5)
