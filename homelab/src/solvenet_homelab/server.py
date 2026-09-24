"""Small server-rendered private homelab overview."""

import argparse
import hmac
import secrets
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from importlib.resources import files
from urllib.parse import urlsplit, parse_qs

from .client import Client, CoordinatorInvalid, CoordinatorOffline
from .settings import LOCATIONS, Settings
from . import layout, problems, runs as run_pages


def model_cards(models, activity, titles=None):
    def text(value):
        return escape(str(value), quote=True)
    activity = activity or {}
    titles = titles or {}
    cards = []
    for model_id, name, provider, location in sorted(
            models, key=lambda row: (row[3] != 'On this device' or row[2].casefold() != 'ollama',
                                     row[1].casefold(), row[0])):
        signal = activity.get(model_id, {})
        status = signal.get('status', 'unknown')
        if status == 'working':
            run_id = signal['run_id']
            title = titles.get(model_id)
            label = f'Working on {text(title)}' if title else f'Working on job {text(signal["job_id"])}'
            state = f'{label}<br><a href="/runs/{text(run_id)}">View run</a>' if problems.RUN_ID.fullmatch(run_id) else label
        elif status == 'idle':
            state = 'Idle — ready' if signal.get('ready') is True else 'Idle — provider health unobserved'
        elif status == 'unavailable':
            state = f'Provider unavailable — {text(signal.get("reason") or "Check the worker provider")}'
        elif status == 'offline':
            state = 'Offline/stale — worker signal is stale'
        else:
            state = 'Configured but unobserved — no worker signal'
        cards.append(f'''<article class="model-card"><h2>{text(name)}</h2>
<span class="location">{text(location)}</span><p class="provider">{text(provider)}</p>
<p class="status">{state}</p><p class="model-id">Model ID: {text(model_id)}</p></article>''')
    configured = ''.join(cards) or '<p class="empty">No models configured yet. Add a model to see its status here.</p>'
    checked = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    return (f'<section id="model-activity" hx-get="/models/activity" hx-trigger="every 10s" '
            f'hx-swap="outerHTML"><div class="grid">{configured}</div>'
            f'<p class="muted">Last checked: <time>{checked}</time> · <a href="/">Refresh status</a></p></section>')


def render(url, models, catalog, runs, error=None, activity=None, titles=None):
    def text(value):
        return escape(str(value), quote=True)

    if error:
        overview = f'<p role="alert">{text(error)}. Check the selected coordinator and try refreshing.</p>'
    else:
        sets = ''.join(f'<li>{text(row["set_id"])} v{text(row["version"])} — {text(row["problem_count"])} problems</li>'
                       for row in catalog)
        recent = ''.join(f'<li>Run {text(row["run_id"])} — {text(row["status"])}</li>' for row in runs)
        overview = (f'<h3>Fixture sets</h3><ul>{sets or "<li>No fixture sets available.</li>"}</ul>'
                    f'<h3>Recent runs</h3><ul>{recent or "<li>No recent runs.</li>"}</ul>')
    return layout.page('Models',
                       '<p class="lede">Configured models and worker activity.</p>'
                       f'<p class="selected">Selected coordinator: {text(url)}</p>'
                       f'{model_cards(models, activity, titles)}'
                       f'<section class="panel overview"><h2>Coordinator activity</h2>{overview}</section>',
                       section='Models', htmx=True)


def fixture_titles(client, activity):
    titles = {}
    cache = {}
    for model, signal in activity.items():
        if signal.get('status') != 'working':
            continue
        key = (signal.get('fixture_set_id'), signal.get('fixture_version'), signal.get('fixture_problem_id'))
        if (not isinstance(key[0], str) or not problems.ID.fullmatch(key[0])
                or type(key[1]) is not int or not isinstance(key[2], str)
                or not problems.ID.fullmatch(key[2])):
            continue
        if key not in cache:
            try:
                cache[key] = client.problem(*key)['title']
            except (CoordinatorOffline, CoordinatorInvalid):
                cache[key] = None
        titles[model] = cache[key]
    return titles


def selectable_model(model, activity):
    model_id = model[0]
    signal = activity.get(model_id, {})
    return (signal.get('status') == 'working' or signal.get('ready') is True
            or (signal.get('status') == 'idle' and not model_id.startswith('ollama/')))


def make_server(settings, address, timeout=2):
    secret = secrets.token_bytes(32)

    class Handler(BaseHTTPRequestHandler):
        def send_page(self, body, status=200, cookie=None):
            body = body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            if cookie:
                self.send_header('Set-Cookie', f'csrf_session={cookie}; HttpOnly; SameSite=Strict; Path=/')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def csrf_cookie(self):
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get('Cookie', ''))
            except Exception:
                pass
            value = cookies.get('csrf_session')
            if value and len(value.value) == 64 and all(c in '0123456789abcdef' for c in value.value):
                return value.value, None
            value = secrets.token_hex(32)
            return value, value

        def token(self, cookie):
            return hmac.digest(secret, cookie.encode(), 'sha256').hex()

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ('/static/htmx.min.js', '/static/site.css'):
                body = files('solvenet_homelab').joinpath('static', path.rsplit('/', 1)[-1]).read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/css; charset=utf-8' if path.endswith('.css') else 'text/javascript; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path not in ('/', '/models/activity', '/problems') and not path.startswith('/problems/') and not path.startswith('/runs/'):
                self.send_error(404)
                return
            url = settings.selected()
            models = settings.models(url)
            client = Client(url, timeout)
            if path == '/models/activity':
                try:
                    activity = client.model_activity([row[0] for row in models])
                    body = model_cards(models, activity, fixture_titles(client, activity))
                except (CoordinatorOffline, CoordinatorInvalid):
                    body = model_cards(models, {})
                    body = body.replace('<div class="grid">', '<p role="alert">Coordinator activity unavailable. Refresh to retry.</p><div class="grid">', 1)
                return self.send_page(body)
            if path != '/':
                parts = path.strip('/').split('/')
                if parts[0] == 'runs' and len(parts) in (2, 3) and problems.RUN_ID.fullmatch(parts[1]) and (len(parts) == 2 or parts[2] == 'status'):
                    try:
                        run = client.run_status(parts[1]) if len(parts) == 3 else client.run(parts[1])
                        if len(parts) == 3:
                            body = run_pages.summary(run)
                        else:
                            title = None
                            if (run.get('fixture_set_id') and type(run.get('fixture_version')) is int
                                    and run.get('fixture_problem_id')):
                                try:
                                    title = client.problem(run['fixture_set_id'], run['fixture_version'],
                                                           run['fixture_problem_id'])['title']
                                except (CoordinatorOffline, CoordinatorInvalid):
                                    pass  # The run remains inspectable if the catalog is unavailable.
                            body = run_pages.detail(run, title)
                    except (CoordinatorOffline, CoordinatorInvalid) as exc:
                        message = f'<p role="alert">{escape(str(exc))}. Refresh to retry.</p>'
                        body = (f'<section id="run-status" hx-get="/runs/{parts[1]}/status" '
                                f'hx-trigger="every 5s" hx-swap="outerHTML">{message}</section>'
                                if len(parts) == 3 else problems.page('Run', message))
                    return self.send_page(body)
                if path == '/problems':
                    try:
                        catalog, runs = client.overview()
                        rows = []
                        for fixture in catalog:
                            if not problems.ID.fullmatch(str(fixture['set_id'])) or type(fixture['version']) is not int:
                                raise CoordinatorInvalid('Coordinator returned an invalid response')
                            listing = client.fixture_set(fixture['set_id'], fixture['version'])
                            rows.extend((fixture, p) for p in listing['problems'])
                        body = problems.list_page(catalog, rows, runs)
                    except (CoordinatorOffline, CoordinatorInvalid) as exc:
                        body = problems.list_page([], [], [], str(exc))
                    return self.send_page(body)
                if len(parts) != 4 or parts[0] != 'problems' or not problems.ID.fullmatch(parts[1]) or not parts[2].isascii() or not parts[2].isdecimal() or len(parts[2]) > 18 or not problems.ID.fullmatch(parts[3]):
                    self.send_error(404)
                    return
                try:
                    fixture = client.problem(parts[1], int(parts[2]), parts[3])
                    runs = client.overview()[1]
                    activity = client.model_activity([m[0] for m in models])
                    available = [m for m in models if selectable_model(m, activity)]
                    cookie, new_cookie = self.csrf_cookie()
                    return self.send_page(problems.detail(fixture, fixture, available, runs,
                                                          self.token(cookie)), cookie=new_cookie)
                except (CoordinatorOffline, CoordinatorInvalid) as exc:
                    return self.send_page(problems.page('Problem', f'<p role="alert">{escape(str(exc))}. Refresh to retry.</p>'), 503)
            try:
                catalog, runs = client.overview()
                error = None
            except (CoordinatorOffline, CoordinatorInvalid) as exc:
                catalog, runs, error = [], [], str(exc)
            try:
                activity = client.model_activity([row[0] for row in models]) if error is None else {}
            except (CoordinatorOffline, CoordinatorInvalid):
                activity = {}
            self.send_page(render(url, models, catalog, runs, error, activity, fixture_titles(client, activity)))

        def do_POST(self):
            parts = urlsplit(self.path).path.strip('/').split('/')
            if (len(parts) != 4 or parts[0] != 'problems' or not problems.ID.fullmatch(parts[1])
                    or not parts[2].isascii() or not parts[2].isdecimal() or len(parts[2]) > 18 or not problems.ID.fullmatch(parts[3])):
                self.send_error(404)
                return
            if self.headers.get('Content-Type') != 'application/x-www-form-urlencoded':
                self.send_error(415)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 1 <= size <= 4096:
                    raise ValueError()
                fields = parse_qs(self.rfile.read(size).decode('utf-8'), keep_blank_values=True,
                                  strict_parsing=True, max_num_fields=8)
            except (ValueError, UnicodeError):
                self.send_error(400)
                return
            cookie, new_cookie = self.csrf_cookie()
            if (new_cookie or len(fields.get('csrf', [])) != 1
                    or not hmac.compare_digest(fields['csrf'][0], self.token(cookie))):
                self.send_error(403)
                return
            url = settings.selected()
            client = Client(url, timeout)
            models = settings.models(url)
            try:
                fixture = client.problem(parts[1], int(parts[2]), parts[3])
                activity = client.model_activity([m[0] for m in models])
                available = [m for m in models if selectable_model(m, activity)]
                values = problems.validate(fields, {m[0] for m in available})
                payload = {'set_id': parts[1], 'version': int(parts[2]), 'sha256': fixture['sha256'],
                           'problem_id': parts[3], 'model': values['model'], 'attempts': values['attempts'],
                           'max_output_tokens': values['max_output_tokens'],
                           'generation_timeout_seconds': values['generation_timeout_seconds'],
                           'max_repairs': 0 if values['strategy'] == 'independent' else 2}
                run_id = client.start_fixture_run(payload)
            except ValueError as exc:
                return self.send_page(problems.detail(fixture, fixture, available, [],
                                      self.token(cookie), str(exc), {k: v[0] for k, v in fields.items()}), 400)
            except (CoordinatorOffline, CoordinatorInvalid) as exc:
                return self.send_page(problems.page('Problem', f'<p role="alert">{escape(str(exc))}. Check the coordinator and retry.</p>'), 503)
            self.send_response(303)
            self.send_header('Location', '/runs/' + run_id)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, format, *args):
            # Never log request targets or upstream exceptions (which may contain secrets).
            pass

    return ThreadingHTTPServer(address, Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Private SolveNet homelab service')
    parser.add_argument('--db', default='homelab.db', help='site SQLite path (not coordinator DB)')
    commands = parser.add_subparsers(dest='command', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8081)
    select = commands.add_parser('set-coordinator')
    select.add_argument('url')
    model = commands.add_parser('add-model')
    model.add_argument('model_id')
    model.add_argument('display_name')
    model.add_argument('provider')
    model.add_argument('location', choices=LOCATIONS)
    args = parser.parse_args(argv)
    settings = Settings(args.db)
    try:
        if args.command == 'set-coordinator':
            settings.select(args.url)
        elif args.command == 'add-model':
            settings.add_model(args.model_id, args.display_name, args.provider, args.location)
        else:
            server = make_server(settings, (args.host, args.port))
            try:
                server.serve_forever()
            finally:
                server.server_close()
    except ValueError as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
