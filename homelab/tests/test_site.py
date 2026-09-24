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
from solvenet_homelab.progress import Progress, MAX_ENTRIES, MAX_OUTPUT, fragment
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
        elif self.path.startswith('/v1/fixture-sets/core/versions/1?limit=10&offset='):
            query = parse_qs(urlsplit(self.path).query)
            offset = int(query['offset'][0])
            body = json.dumps({'problems': [{'id': 'lemma-one', 'title': '<Lemma>',
                                             'category': '<Logic>', 'description': '<script>bad</script>',
                                             'statement': ': True -- <img src=x>', 'imports': ['Init', '<Import>']}]
                               if offset == 0 else [{'id': 'lemma-two', 'title': 'Second',
                                                      'statement': ': True', 'imports': ['Init']}],
                               'sha256': 'a' * 64, 'environment': 'Lean',
                               'next_offset': 10 if self.server.paginated and offset == 0 else None}).encode()
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
                                **{key: len(run[key]) for key in ('jobs', 'assignments', 'attempts')},
                                'active_assignment': next(({'id': row['id'], 'job_id': row['job_id']}
                                                           for row in run['assignments'] if row['status'] == 'active'), None)}).encode()
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

    def test_authenticated_bounded_live_progress_and_small_fragment(self):
        coordinator, url = self.stub()
        self.settings.select(url)
        site = make_server(self.settings, ('127.0.0.1', 0), timeout=.3, progress_token='x' * 40)
        self.start(site)
        origin = f'http://127.0.0.1:{site.server_port}'
        run = 'a' * 32
        payload = {'run_id': run, 'job_id': 'b' * 32, 'assignment_id': 'c' * 32, 'output': ''}
        coordinator.run_detail['assignments'] = [{'id': 'c' * 32, 'job_id': 'b' * 32, 'status': 'active'}]

        def post(data, token='x' * 40):
            request = Request(origin + '/internal/progress', data=json.dumps(data).encode(),
                              headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
            try:
                with urlopen(request) as response:
                    return response.status
            except HTTPError as error:
                error.close()
                return error.code

        self.assertEqual(post(payload, 'wrong'), 403)
        self.assertEqual(post({**payload, 'output': 'x' * (MAX_OUTPUT + 1)}), 400)
        self.assertEqual(post(payload), 204)
        self.assertEqual(post(payload), 429)
        with urlopen(origin + '/runs/' + run + '/progress') as response:
            self.assertIn('Waiting for model output', response.read().decode())
        time.sleep(.21)
        self.assertEqual(post({**payload, 'output': '<script>part 1</script>'}), 204)
        with urlopen(origin + '/runs/' + run + '/progress') as response:
            html = response.read().decode()
        self.assertIn('&lt;script&gt;part 1&lt;/script&gt;', html)
        self.assertIn('Partial, unverified', html)
        self.assertNotIn('<script>part', html)
        self.assertIn('hx-get="/runs/' + run + '/progress"', html)
        self.assertEqual(coordinator.paths[-1], '/v1/runs/' + run + '/status')
        self.assertIn('Refresh run details', html)
        coordinator.run_detail['assignments'] = []
        with urlopen(origin + '/runs/' + run + '/progress') as response:
            self.assertIn('Waiting for the next assignment', response.read().decode())
        coordinator.run_detail['assignments'] = [{'id': 'c' * 32, 'job_id': 'b' * 32, 'status': 'active'}]
        time.sleep(.21)
        self.assertEqual(post({**payload, 'output': '<script>part 1</script> more'}), 204)
        with urlopen(origin + '/runs/' + run + '/progress') as response:
            self.assertIn('part 1&lt;/script&gt; more', response.read().decode())
        time.sleep(.21)
        self.assertEqual(post({**payload, 'output': '<script>part 1</script> more complete'}), 204)
        with urlopen(origin + '/runs/' + run + '/progress') as response:
            self.assertIn('more complete', response.read().decode())
        coordinator.run_detail['status'] = 'exhausted'
        with urlopen(origin + '/runs/' + run + '/progress') as response:
            self.assertNotIn('part 1', response.read().decode())
        coordinator.shutdown()
        with urlopen(origin + '/runs/' + run + '/progress') as response:
            self.assertIn('hx-get="/runs/' + run + '/progress"', response.read().decode())

    def test_progress_eviction_stall_and_restart(self):
        store = Progress()
        for i in range(MAX_ENTRIES + 2):
            store.put(f'{i:032x}', 'b' * 32, 'c' * 32, 'text')
        self.assertEqual(len(store.entries), MAX_ENTRIES)
        self.assertIsNone(store.snapshot(f'{0:032x}'))
        run = 'a' * 32
        self.assertIn('Waiting for model output', fragment(run, 'running', Progress().snapshot(run)))
        store.put(run, 'b' * 32, 'c' * 32, 'partial')
        with store.lock:
            key = (run, 'b' * 32, 'c' * 32)
            updated, started, output = store.entries[key]
            store.entries[key] = (updated - 15, started, output)
        self.assertIn('expired or stalled', fragment(run, 'running', store.snapshot(run)))

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
            'ollama/a': {'model': 'ollama/a', 'status': 'working', 'job_id': '<job>', 'run_id': 'a' * 32,
                         'fixture_set_id': 'core', 'fixture_version': 1, 'fixture_problem_id': 'lemma-one'},
            'ollama/b': {'model': 'ollama/b', 'status': 'offline'},
        }
        html = self.page()
        self.assertIn('Working on &lt;Lemma&gt;', html)
        self.assertIn('href="/runs/' + 'a' * 32 + '">View run</a>', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('Offline/stale — worker signal is stale', html)
        self.assertIn('Configured but unobserved — no worker signal', html)
        self.assertIn('Last checked:', html)
        self.assertIn('hx-get="/models/activity"', html)
        self.assertNotIn('/v1/runs/' + 'a' * 32, stub.paths)
        previous = list(stub.paths)
        with urlopen(self.site_url + '/models/activity') as response:
            fragment = response.read().decode()
        self.assertIn('Working on &lt;Lemma&gt;', fragment)
        self.assertNotIn('Coordinator activity</h2>', fragment)
        self.assertEqual(stub.paths[len(previous):], ['/v1/model-activity?model=ollama%2Fa&model=cloud%2Fc&model=ollama%2Fb',
                                                       '/v1/fixture-sets/core/versions/1/problems/lemma-one'])
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
        stub.activity['ollama/a'] = {'model': 'ollama/a', 'status': 'idle', 'ready': True}
        self.assertIn('Idle — ready', self.page())
        stub.activity['ollama/a'] = {'model': 'ollama/a', 'status': 'unavailable', 'reason': 'Ollama service unreachable'}
        self.assertIn('Provider unavailable — Ollama service unreachable', self.page())
        stub.activity['ollama/a'] = {'model': 'ollama/a', 'status': 'idle'}
        self.assertIn('Idle — provider health unobserved', self.page())
        stub.activity['ollama/a'] = {'model': 'ollama/a', 'status': 'working',
                                      'job_id': '<job>', 'run_id': 'a' * 32}
        self.assertIn('Working on job &lt;job&gt;', self.page())
        stub.activity['ollama/a'] = {'model': 'ollama/a', 'status': 'working', 'job_id': '<job>'}
        html = self.page()
        self.assertIn('Configured but unobserved — no worker signal', html)
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

    def test_idle_hosted_model_remains_selectable_without_ollama_health(self):
        stub, url = self.stub()
        self.settings.select(url)
        self.settings.add_model('openai/gpt-4o-mini', 'Hosted', 'OpenAI', 'Cloud API')
        self.settings.add_model('ollama/tiny', 'Ollama', 'Ollama', 'On this device')
        stub.activity['openai/gpt-4o-mini'] = {'model': 'openai/gpt-4o-mini', 'status': 'idle'}
        stub.activity['ollama/tiny'] = {'model': 'ollama/tiny', 'status': 'idle'}
        with urlopen(self.site_url + '/problems/core/1/lemma-one') as response:
            page = response.read().decode()
        self.assertIn('value="openai/gpt-4o-mini"', page)
        self.assertNotIn('value="ollama/tiny"', page)

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
        stub.activity['ollama/a'] = {'model': 'ollama/a', 'status': 'idle', 'ready': True}
        with urlopen(self.site_url + '/problems') as response:
            listing = response.read().decode()
        self.assertIn('&lt;Lemma&gt;', listing)
        self.assertIn('<details><summary><h2 class="problem-heading">', listing)
        self.assertIn('&lt;Logic&gt; · No recent runs', listing)
        self.assertIn('&lt;script&gt;bad&lt;/script&gt;', listing)
        self.assertIn(': True -- &lt;img src=x&gt;', listing)
        self.assertIn('&lt;Import&gt;', listing)
        self.assertNotIn('<script>bad</script>', listing)
        self.assertNotIn('SECRET_PROOF', listing)
        self.assertIn('<a href="/problems/core/1/lemma-one">View problem / start work</a>', listing)
        self.assertNotIn('<a ', listing.split('<summary>', 1)[1].split('</summary>', 1)[0])
        self.assertNotIn('hx-get=', listing)
        self.assertNotIn('<script ', listing)
        self.assertEqual(stub.paths, ['/v1/fixture-sets', '/v1/runs?limit=10',
                                       '/v1/fixture-sets/core/versions/1?limit=10&offset=0&preview=1'])
        self.assertIn('<a href="/problems" aria-current="page">Problems</a>', listing)
        self.assertIn('<a href="/">Models</a>', listing)
        stub.paginated = True
        with urlopen(self.site_url + '/problems') as response:
            self.assertIn('Second', response.read().decode())
        self.assertEqual(stub.paths[-2:], ['/v1/fixture-sets/core/versions/1?limit=10&offset=0&preview=1',
                                            '/v1/fixture-sets/core/versions/1?limit=10&offset=10&preview=1'])
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

    def test_problem_preview_recent_run_link(self):
        run_id = 'a' * 32
        stub, url = self.stub(runs=json.dumps({'items': [
            {'run_id': run_id, 'status': '<solved>', 'fixture_set_id': 'core',
             'fixture_version': 1, 'fixture_problem_id': 'lemma-one'}],
            'next_cursor': None}).encode())
        self.settings.select(url)
        with urlopen(self.site_url + '/problems') as response:
            listing = response.read().decode()
        self.assertIn('Recent activity: &lt;solved&gt; · <a href="/runs/' + run_id + '">View recent run</a>', listing)
        self.assertIn('&lt;Logic&gt; · &lt;solved&gt;', listing)
        self.assertEqual(len(stub.paths), 3)

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
        self.assertLess(html.index('Run status: solved'), html.index('Run configuration and identifiers'))
        self.assertIn('Run status: solved', html)
        self.assertIn('2 jobs · 2 leased assignments · 2 completed candidate attempts', html)
        self.assertIn('Lean verification: verified', html)
        self.assertIn('Lean verification: rejected', html)
        self.assertIn('Outcome: Repaired proof verified', html)
        self.assertIn('Selected model: &lt;model&gt;, local', html)
        self.assertIn('Job 1: queued → completed', html)
        self.assertIn('Leased work → completed', html)
        self.assertIn('Generated candidate → Lean verification: verified', html)
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
        self.assertIn('Job 1: queued → failed', html)
        self.assertIn('Failure: &lt;failure&gt;', html)
        self.assertIn('Outcome: zero candidates; provider/worker failure', html)
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

    def test_nine_failed_leases_zero_candidates_are_not_proof_failures(self):
        stub, url = self.stub()
        self.settings.select(url)
        self.settings.add_model('ollama', 'Local prover', 'ollama', 'On this device')
        stub.run_detail.update(status='exhausted',
            jobs=[{'id': f'{i:032x}', 'model': 'ollama', 'status': 'failed'} for i in range(9)],
            assignments=[{'id': f'{i+100:032x}', 'job_id': f'{i:032x}', 'status': 'failed',
                          'error': 'dial tcp: connection refused <offline>',
                          'failure_class': 'transient', 'usage': {},
                          'generation': {'raw_response': ''}} for i in range(9)])
        with urlopen(self.site_url + '/runs/' + 'a' * 32) as response:
            html = response.read().decode()
        self.assertIn('Selected model: Local prover (ollama)', html)
        self.assertIn('Outcome: zero candidates; provider unreachable', html)
        self.assertIn('9 failed leased assignment(s); no proof reached Lean', html)
        self.assertNotIn('candidate(s) rejected by Lean', html)
        self.assertEqual(html.count('Leased work → failed'), 9)
        self.assertEqual(html.count('Lease details, raw output and usage'), 9)
        self.assertIn('connection refused &lt;offline&gt;', html)
        self.assertNotIn('<offline>', html)

    def test_initial_verification_and_rejected_proof_have_distinct_outcomes(self):
        stub, url = self.stub()
        self.settings.select(url)
        attempt = {'id': 'c' * 32, 'job_id': 'b' * 32, 'assignment_id': 'd' * 32,
                   'model': 'm', 'candidate': 'by trivial <proof>',
                   'diagnostics': '<error>' * 500, 'usage': {},
                   'generation': {'raw_response': '<raw response>'}}
        stub.run_detail.update(status='solved',
            jobs=[{'id': 'b' * 32, 'model': 'm', 'status': 'completed'}],
            assignments=[{'id': 'd' * 32, 'job_id': 'b' * 32, 'status': 'completed'}],
            attempts=[{**attempt, 'verification_status': 'verified', 'diagnostics': ''}])
        path = self.site_url + '/runs/' + 'a' * 32
        with urlopen(path) as response:
            verified = response.read().decode()
        self.assertIn('Outcome: Initial proof verified', verified)
        self.assertNotIn('Live model output', verified)
        self.assertEqual(verified.count('Refresh run details'), 1)
        self.assertNotIn('Run finished. Refresh for the latest details.', verified)
        self.assertNotIn('Repaired proof verified', verified)
        self.assertEqual(verified.count('by trivial &lt;proof&gt;'), 1)
        self.assertIn('Input tokens</dt><dd>Unknown', verified)
        self.assertIn('&lt;raw response&gt;', verified)
        stub.run_detail.update(status='exhausted', attempts=[{**attempt, 'verification_status': 'rejected'}])
        with urlopen(path) as response:
            rejected = response.read().decode()
        self.assertIn('Outcome: 1 candidate(s) rejected by Lean', rejected)
        self.assertNotIn('zero candidates', rejected)
        self.assertIn(escape(attempt['diagnostics']), rejected)
        self.assertEqual(rejected.count(escape(attempt['diagnostics'])), 1)
        self.assertNotIn('<error>', rejected)
        stub.run_detail.update(status='error', attempts=[{**attempt, 'verification_status': 'verifier_error'}])
        with urlopen(path) as response:
            verifier_error = response.read().decode()
        self.assertIn('Outcome: Lean verification error', verifier_error)
        self.assertNotIn('zero candidates', verifier_error)
        stub.run_detail.update(status='running', attempts=[{**attempt, 'verification_status': None}])
        with urlopen(path) as response:
            pending = response.read().decode()
        self.assertIn('Outcome: Verification pending', pending)
        self.assertNotIn('No candidate has completed', pending)
        stub.run_detail.update(status='solved', attempts=[{**attempt, 'verification_status': 'verified',
                                                            'parent_attempt_id': 'e' * 32}])
        with urlopen(path) as response:
            repaired = response.read().decode()
        self.assertIn('Outcome: Repaired proof verified', repaired)


if __name__ == '__main__':
    unittest.main()
