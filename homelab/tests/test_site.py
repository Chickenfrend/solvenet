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
from urllib.request import urlopen, Request
from urllib.error import HTTPError
import re

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
        elif self.path.startswith('/v1/fixture-sets/core/versions/1?limit=100&offset='):
            offset = int(self.path.rsplit('=', 1)[-1])
            body = json.dumps({'problems': [{'id': 'lemma-one', 'title': '<Lemma>'}]
                               if offset == 0 else [{'id': 'lemma-two', 'title': 'Second'}],
                               'sha256': 'a' * 64, 'environment': 'Lean',
                               'next_offset': 100 if self.server.paginated and offset == 0 else None}).encode()
        elif self.path == '/v1/fixture-sets/core/versions/1/problems/lemma-one':
            body = json.dumps({'id': 'lemma-one', 'title': '<Lemma>', 'statement': ': True',
                               'set_id': 'core', 'version': 1,
                               'imports': ['Init'], 'environment': 'Lean', 'sha256': 'a' * 64,
                               'reference_proof': 'SECRET_PROOF'}).encode()
        elif self.path == '/v1/runs/' + 'a' * 32:
            body = json.dumps(self.server.run_detail).encode()
        elif self.path == '/v1/runs/' + 'a' * 32 + '/status':
            run = self.server.run_detail
            body = json.dumps({'id': run['id'], 'status': run['status'],
                               **{key: len(run[key]) for key in ('jobs', 'assignments', 'attempts')}}).encode()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', self.server.content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.paths.append(self.path)
        self.server.submissions.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        body = json.dumps({'run_id': 'a' * 32}).encode()
        self.send_response(self.server.post_status)
        self.send_header('Content-Type', 'application/json')
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
        server.paginated = False
        server.post_status = 201
        server.paths = []
        server.submissions = []
        server.run_detail = {'id': 'a' * 32, 'status': 'running', 'jobs': [],
                             'assignments': [], 'attempts': []}
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
        self.assertIn('href="/static/site.css"', html)
        self.assertIn('<a href="/" aria-current="page">Models</a>', html)
        self.assertIn('<a href="/problems">Problems</a>', html)
        with urlopen(self.site_url + '/static/site.css') as response:
            self.assertEqual(response.headers['Content-Type'], 'text/css; charset=utf-8')
            css = response.read().decode()
        self.assertIn('@media (min-width: 42rem)', css)
        self.assertIn('grid-template-columns: minmax(0, 1fr)', css)
        self.assertIn('overflow-wrap: anywhere', css)
        self.assertIn(':focus-visible', css)
        stub.activity['ollama/a'] = {'model': 'ollama/a', 'status': 'idle'}
        self.assertIn('Idle — worker recently checked in', self.page())
        stub.activity['ollama/a'] = {'model': 'ollama/a', 'status': 'working', 'job_id': '<job>'}
        html = self.page()
        self.assertIn('Unknown — configured; no worker signal', html)
        self.assertNotIn('Working on job', html)

    def test_shared_navigation_and_empty_states(self):
        stub, url = self.stub(catalog=b'{"items":[]}', runs=b'{"items":[],"next_cursor":null}')
        self.settings.select(url)
        models = self.page()
        self.assertIn('No models configured yet.', models)
        self.assertIn('No fixture sets available.', models)
        self.assertIn('No recent runs.', models)
        self.assertIn('aria-current="page">Models</a>', models)
        with urlopen(self.site_url + '/problems') as response:
            listing = response.read().decode()
        self.assertIn('No problems available.', listing)
        self.assertIn('aria-current="page">Problems</a>', listing)
        self.assertIn('href="/static/site.css"', listing)
        self.assertNotIn('<style>', listing)
        stub.shutdown()
        self.assertIn('role="alert"', self.page())
        with urlopen(self.site_url + '/problems') as response:
            self.assertIn('role="alert"', response.read().decode())

    def test_refuse_coordinator_database(self):
        other = self.db.parent / 'other.db'
        with sqlite3.connect(other) as db:
            db.execute('CREATE TABLE runs (id TEXT)')
        with self.assertRaisesRegex(ValueError, 'separate'):
            Settings(other)
        with sqlite3.connect(other) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('runs',)])

    def test_problem_submission_validation_csrf_and_confirmation(self):
        stub, url = self.stub()
        self.settings.select(url)
        self.settings.add_model('ollama/a', 'Local', 'Ollama', 'On this device')
        stub.activity['ollama/a'] = {'model': 'ollama/a', 'status': 'idle'}
        with urlopen(self.site_url + '/problems') as response:
            listing = response.read().decode()
        self.assertIn('&lt;Lemma&gt;', listing)
        self.assertIn('<a href="/problems" aria-current="page">Problems</a>', listing)
        self.assertIn('<a href="/">Models</a>', listing)
        stub.paginated = True
        with urlopen(self.site_url + '/problems') as response:
            self.assertIn('Second', response.read().decode())
        path = '/problems/core/1/lemma-one'
        with urlopen(self.site_url + path) as response:
            detail = response.read().decode()
            cookie = response.headers['Set-Cookie'].split(';')[0]
        self.assertIn(': True', detail)
        self.assertIn('Init', detail)
        self.assertNotIn('SECRET_PROOF', detail)
        token = re.search(r'name="csrf" value="([0-9a-f]+)"', detail).group(1)
        def post(fields, cookie_value=cookie):
            from urllib.parse import urlencode
            request = Request(self.site_url + path, data=urlencode(fields).encode(),
                              headers={'Content-Type': 'application/x-www-form-urlencoded',
                                       'Cookie': cookie_value})
            try:
                with urlopen(request) as response:
                    return response.status, response.read().decode()
            except HTTPError as error:
                with error:
                    return error.code, error.read().decode()
        values = {'csrf': token, 'model': 'ollama/a', 'strategy': 'repair', 'attempts': '2',
                  'max_output_tokens': '512', 'generation_timeout_seconds': '60'}
        self.assertEqual(post(values, '')[0], 403)
        self.assertEqual(post({**values, 'csrf': 'wrong'})[0], 403)
        for field, invalid in [('model', 'unconfigured'), ('strategy', 'bad'), ('attempts', '0'),
                               ('max_output_tokens', '999999'), ('generation_timeout_seconds', 'abc')]:
            status, html = post({**values, field: invalid})
            self.assertEqual(status, 400)
            self.assertIn('role="alert"', html)
        self.assertEqual(stub.submissions, [])
        status, html = post(values)
        self.assertEqual(status, 200)  # urllib follows the 303 to the confirmation page
        self.assertIn('Run status: running', html)
        self.assertEqual(stub.submissions, [{'set_id': 'core', 'version': 1, 'sha256': 'a' * 64,
                                            'problem_id': 'lemma-one', 'model': 'ollama/a',
                                            'attempts': 2, 'max_output_tokens': 512,
                                            'generation_timeout_seconds': 60, 'max_repairs': 2}])
        self.assertNotIn('SECRET_PROOF', json.dumps(stub.submissions))
        stub.post_status = 200
        status, html = post(values)
        self.assertEqual(status, 503)
        self.assertIn('incompatible response', html)
        stub.post_status = 201
        long_path = '/problems/core/' + '9' * 5000 + '/lemma-one'
        for method in ('GET', 'POST'):
            request = Request(self.site_url + long_path, method=method,
                              data=b'csrf=x' if method == 'POST' else None)
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=2)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
        stub.shutdown()
        status, html = post(values)
        self.assertEqual(status, 503)
        self.assertIn('Check the coordinator and retry', html)

    def test_run_detail_solved_repairs_usage_and_escaped_large_text(self):
        stub, url = self.stub()
        self.settings.select(url)
        payload = '<script>alert("x")</script>' + 'X' * 100000
        parent = 'b' * 32
        child = 'c' * 32
        stub.run_detail = {
            'id': 'a' * 32, 'status': 'solved', 'fixture_set_id': 'core',
            'fixture_version': 1, 'fixture_problem_id': 'lemma-one',
            'jobs': [{'id': 'd' * 32, 'status': 'completed', 'model': '<model>',
                      'repair_depth': 0, 'parent_attempt_id': None},
                     {'id': 'e' * 32, 'status': 'completed', 'model': 'local',
                      'repair_depth': 1, 'parent_attempt_id': parent}],
            'assignments': [{'id': 'f' * 32, 'job_id': 'd' * 32, 'worker_id': 'worker',
                             'status': 'completed', 'usage': {'input_tokens': None}},
                            {'id': '1' * 32, 'job_id': 'e' * 32, 'worker_id': 'worker',
                             'status': 'completed', 'usage': {'output_tokens': 20}}],
            'attempts': [{'id': parent, 'job_id': 'd' * 32, 'assignment_id': 'f' * 32,
                          'parent_attempt_id': None, 'model': '<model>', 'candidate': payload,
                          'verification_status': 'rejected', 'diagnostics': payload,
                          'usage': {'input_tokens': None, 'output_tokens': 12,
                                    '<metric>': None}},
                         {'id': child, 'job_id': 'e' * 32, 'assignment_id': '1' * 32,
                          'parent_attempt_id': parent, 'model': 'local', 'candidate': 'by trivial',
                          'verification_status': 'verified', 'diagnostics': '', 'usage': {}}]}
        with urlopen(self.site_url + '/runs/' + 'a' * 32) as response:
            html = response.read().decode()
        self.assertIn('<h1>&lt;Lemma&gt;</h1>', html)
        self.assertLess(html.index('<h1>&lt;Lemma&gt;</h1>'), html.index('Run status: solved'))
        self.assertLess(html.index('Run status: solved'), html.index('Run ID:'))
        self.assertIn('Run status: solved', html)
        self.assertIn('2 jobs · 2 leased assignments · 2 completed candidate attempts', html)
        self.assertIn('Lean verification: verified', html)
        self.assertIn('Lean verification: rejected', html)
        self.assertIn('href="#attempt-' + parent + '"', html)
        self.assertIn('href="/problems/core/1/lemma-one"', html)
        self.assertIn(escape(payload), html)
        self.assertNotIn('<script>alert', html)
        self.assertIn('Input tokens</dt><dd>Unknown', html)
        self.assertIn('Output tokens</dt><dd>12', html)
        self.assertIn('&lt;metric&gt;</dt><dd>Unknown', html)
        self.assertIn('href="/static/site.css"', html)
        self.assertIn('<a href="/problems" aria-current="page">Problems</a>', html)
        self.assertIn('name="viewport"', html)
        self.assertNotIn('hx-trigger=', html)
        self.assertEqual(stub.paths, ['/v1/runs/' + 'a' * 32,
                                      '/v1/fixture-sets/core/versions/1/problems/lemma-one'])

    def test_run_polling_stops_on_terminal_and_manual_refresh_works_without_js(self):
        stub, url = self.stub()
        self.settings.select(url)
        path = '/runs/' + 'a' * 32
        with urlopen(self.site_url + path) as response:
            html = response.read().decode()
        self.assertIn('hx-get="' + path + '/status" hx-trigger="every 5s" hx-swap="outerHTML"', html)
        self.assertIn('href="' + path + '">Refresh run details</a>', html)
        self.assertIn('<script src="/static/htmx.min.js" defer></script>', html)
        with urlopen(self.site_url + '/static/htmx.min.js') as response:
            self.assertIn(b'htmx', response.read())
        with urlopen(self.site_url + path + '/status') as response:
            fragment = response.read().decode()
        self.assertTrue(fragment.startswith('<section id="run-status"'))
        self.assertIn('hx-trigger="every 5s"', fragment)
        self.assertNotIn('<html', fragment)
        stub.run_detail['status'] = 'exhausted'
        stub.run_detail['jobs'] = [{'id': 'd' * 32, 'status': 'failed', 'repair_depth': 0}]
        stub.run_detail['assignments'] = [{'id': 'e' * 32, 'job_id': 'd' * 32,
                                           'status': 'failed', 'error': '<failure>', 'usage': {}}]
        with urlopen(self.site_url + path + '/status') as response:
            fragment = response.read().decode()
        self.assertIn('Run status: exhausted', fragment)
        self.assertNotIn('hx-trigger=', fragment)
        with urlopen(self.site_url + path) as response:
            html = response.read().decode()
        self.assertIn('Job ' + 'd' * 32 + ' — failed', html)
        self.assertIn('Failure: &lt;failure&gt;', html)
        self.assertIn('No candidate attempts yet.', html)
        self.assertNotIn('hx-trigger=', html)
        self.assertEqual(stub.paths, ['/v1/runs/' + 'a' * 32,
                                      '/v1/runs/' + 'a' * 32 + '/status',
                                      '/v1/runs/' + 'a' * 32 + '/status',
                                      '/v1/runs/' + 'a' * 32])
        stub.shutdown()
        with urlopen(self.site_url + path + '/status') as response:
            fragment = response.read().decode()
        self.assertIn('hx-trigger="every 5s"', fragment)
        self.assertIn('unavailable', fragment)


if __name__ == '__main__':
    unittest.main()
