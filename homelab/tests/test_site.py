import sqlite3
import tempfile
import threading
import time
import unittest
import json
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from solvenet_homelab.server import make_server
from solvenet_homelab.settings import Settings


class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.paths.append(self.path)
        if self.path == '/v1/fixture-sets':
            body = self.server.catalog
        elif self.path == '/v1/runs?limit=10':
            body = self.server.runs
        elif urlsplit(self.path).path == '/v1/model-activity':
            models = parse_qs(urlsplit(self.path).query).get('model', [])
            body = json.dumps({'items': [self.server.activity.get(model, {'model': model, 'status': 'unknown'})
                                         for model in models]}).encode()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', self.server.content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class SiteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'site.db'
        self.settings = Settings(self.db)
        self.site = make_server(self.settings, ('127.0.0.1', 0), timeout=.3)
        self.start(self.site)
        self.site_url = f'http://127.0.0.1:{self.site.server_port}'

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join(), server.server_close()))

    def stub(self, catalog=b'{"items":[{"set_id":"core","version":1,"problem_count":3}]}',
             runs=b'{"items":[{"run_id":"run-a","status":"solved"}],"next_cursor":null}',
             content_type='application/json'):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
        server.catalog, server.runs, server.content_type = catalog, runs, content_type
        server.activity = {}
        server.paths = []
        self.start(server)
        return server, f'http://127.0.0.1:{server.server_port}'

    def page(self):
        with urlopen(self.site_url, timeout=2) as response:
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            return response.read().decode()

    def test_separate_storage_and_switching_coordinator(self):
        first, first_url = self.stub()
        second, second_url = self.stub(
            b'{"items":[{"set_id":"challenge","version":2,"problem_count":7}]}',
            b'{"items":[{"run_id":"run-b","status":"exhausted"}],"next_cursor":null}')
        self.settings.select(first_url)
        self.settings.add_model('local', '<Private>', 'Ollama', 'On this device')
        html = self.page()
        self.assertIn('core v1', html)
        self.assertIn('run-a', html)
        self.assertIn('&lt;Private&gt;', html)
        self.assertEqual(first.paths, ['/v1/fixture-sets', '/v1/runs?limit=10', '/v1/model-activity?model=local'])
        self.settings.select(second_url)
        self.settings.add_model('remote', 'Remote', 'Ollama', 'Local network')
        html = self.page()
        self.assertIn('challenge v2', html)
        self.assertIn('run-b', html)
        self.assertIn('Remote', html)
        self.assertNotIn('run-a', html)
        self.assertNotIn('Private', html)
        self.assertEqual(second.paths, ['/v1/fixture-sets', '/v1/runs?limit=10', '/v1/model-activity?model=remote'])
        self.settings.select(first_url)
        self.assertIn('&lt;Private&gt;', self.page())
        with sqlite3.connect(self.db) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {'settings', 'models'})
            self.assertEqual(db.execute('SELECT COUNT(*) FROM models').fetchone()[0], 2)

    def test_outage_and_invalid_response_keep_page_usable(self):
        stub, url = self.stub(catalog=b'<html>error</html>')
        self.settings.select(url)
        self.settings.add_model('m', 'Safe', 'Ollama', 'On this device')
        self.assertIn('invalid response', self.page())
        stub.catalog = b'{"items":{}}'
        self.assertIn('invalid response', self.page())
        stub.catalog = b'{"items":[]}'
        stub.runs = b'{"items":[]}'
        self.assertIn('invalid response', self.page())
        stub.shutdown()
        stub.server_close()
        self.assertIn('unavailable', self.page())
        self.assertIn('Safe', self.page())

    def test_slow_coordinator_times_out(self):
        class SlowStub(Stub):
            def do_GET(self):
                time.sleep(.6)
                try:
                    super().do_GET()
                except BrokenPipeError:
                    pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), SlowStub)
        server.paths, server.catalog, server.runs = [], b'{"items":[]}', b'{"items":[],"next_cursor":null}'
        server.content_type = 'application/json'
        self.start(server)
        self.settings.select(f'http://127.0.0.1:{server.server_port}')
        start = time.monotonic()
        self.assertIn('unavailable', self.page())
        self.assertLess(time.monotonic() - start, 1)

    def test_incompatible_and_truncated_coordinator_responses(self):
        class BrokenStub(Stub):
            def do_GET(self):
                if self.server.broken:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', '100')
                    self.end_headers()
                    self.wfile.write(b'{"items":[]')
                    self.wfile.flush()
                    self.connection.shutdown(1)
                    return
                self.send_error(404)

        server = ThreadingHTTPServer(('127.0.0.1', 0), BrokenStub)
        server.broken = False
        self.start(server)
        self.settings.select(f'http://127.0.0.1:{server.server_port}')
        self.assertIn('incompatible response', self.page())
        server.broken = True
        self.assertIn('invalid response', self.page())

    def test_secrets_not_accepted_or_displayed(self):
        for url in ('http://user:secret@localhost:8080',
                    'http://localhost:8080/?token=secret',
                    'http://localhost:8080/#secret', 'http://localhost:8080/?',
                    'file:///tmp/secret.db'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.settings.select(url)
        server, url = self.stub(
            b'{"items":[{"set_id":"<script>","version":1,"problem_count":2,"reference_proof":"secret"}]}',
            b'{"items":[{"run_id":"<img>","status":"queued","raw_response":"secret"}],"next_cursor":null}')
        self.settings.select(url)
        self.settings.add_model('m', '<script>', 'Ollama', 'Cloud API')
        html = self.page()
        self.assertIn(escape('<script>'), html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('secret', html)
        self.assertEqual(server.paths, ['/v1/fixture-sets', '/v1/runs?limit=10', '/v1/model-activity?model=m'])

    def test_cards_activity_escaping_and_mobile_layout(self):
        stub, url = self.stub()
        self.settings.select(url)
        self.settings.add_model('ollama/a', '<script>alert(1)</script>', 'Ollama', 'On this device')
        self.settings.add_model('ollama/b', 'LAN', 'Ollama', 'Local network')
        self.settings.add_model('cloud/c', 'Cloud', 'API', 'Cloud API')
        stub.activity = {
            'ollama/a': {'model': 'ollama/a', 'status': 'working', 'job_id': '<job>', 'run_id': 'run-1'},
            'ollama/b': {'model': 'ollama/b', 'status': 'offline'},
        }
        html = self.page()
        self.assertIn('Working on job &lt;job&gt; (run run-1)', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('Offline — worker signal is stale', html)
        self.assertIn('Unknown — configured; no worker signal', html)
        self.assertLess(html.index('On this device'), html.index('Local network'))
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', html)
        self.assertIn('grid-template-columns: minmax(0, 1fr)', html)
        self.assertIn('@media (min-width: 42rem)', html)
        self.assertIn('min-width: 0', html)
        self.assertIn('overflow-wrap: anywhere', html)
        stub.activity['ollama/a'] = {'model': 'ollama/a', 'status': 'idle'}
        self.assertIn('Idle — worker recently checked in', self.page())
        stub.activity['ollama/a'] = {'model': 'ollama/a', 'status': 'working', 'job_id': '<job>'}
        html = self.page()
        self.assertIn('Unknown — configured; no worker signal', html)
        self.assertNotIn('Working on job', html)

    def test_refuse_coordinator_database(self):
        other = self.db.parent / 'other.db'
        with sqlite3.connect(other) as db:
            db.execute('CREATE TABLE runs (id TEXT)')
        with self.assertRaisesRegex(ValueError, 'separate'):
            Settings(other)
        with sqlite3.connect(other) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('runs',)])


if __name__ == '__main__':
    unittest.main()
