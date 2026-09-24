"""Small server-rendered private homelab overview."""

import argparse
import hmac
import secrets
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import urlsplit, parse_qs

from .client import Client, CoordinatorInvalid, CoordinatorOffline
from .settings import LOCATIONS, Settings
from . import problems


def render(url, models, catalog, runs, error=None, activity=None):
    def text(value):
        return escape(str(value), quote=True)

    if error:
        overview = f'<p role="status">{text(error)}. Check the selected coordinator and try refreshing.</p>'
    else:
        sets = ''.join(f'<li>{text(row["set_id"])} v{text(row["version"])} — {text(row["problem_count"])} problems</li>'
                       for row in catalog)
        recent = ''.join(f'<li>Run {text(row["run_id"])} — {text(row["status"])}</li>' for row in runs)
        overview = (f'<h2>Fixture sets</h2><ul>{sets}</ul><h2>Recent runs</h2><ul>{recent}</ul>')
    activity = activity or {}
    cards = []
    for model_id, name, provider, location in sorted(
            models, key=lambda row: (row[3] != 'On this device' or row[2].casefold() != 'ollama',
                                     row[1].casefold(), row[0])):
        signal = activity.get(model_id, {})
        status = signal.get('status', 'unknown')
        if status == 'working':
            state = f'Working on job {text(signal["job_id"])} (run {text(signal["run_id"])})'
        elif status == 'idle':
            state = 'Idle — worker recently checked in'
        elif status == 'offline':
            state = 'Offline — worker signal is stale'
        else:
            state = 'Unknown — configured; no worker signal'
        cards.append(f'''<article class="model-card"><span class="location">{text(location)}</span>
<h3>{text(name)}</h3><p class="provider">{text(provider)}</p>
<p class="status">{state}</p><p class="model-id">Model ID: {text(model_id)}</p></article>''')
    configured = ''.join(cards) or '<p>No models configured yet.</p>'
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Models · SolveNet</title><style>
* {{ box-sizing: border-box }}
body {{ max-width: 76rem; margin: 0 auto; padding: 1.25rem; background: #171717; color: #f8f8f8;
font: 1rem/1.5 system-ui, sans-serif; overflow-wrap: anywhere }}
h1, h2 {{ color: #ffac52 }} h1 {{ margin: 0 }}
.top {{ display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: .75rem }}
a {{ color: #ffbd72; min-height: 2.75rem; display: inline-flex; align-items: center }}
.grid {{ display: grid; grid-template-columns: minmax(0, 1fr); gap: 1rem }}
.model-card, .overview {{ border: 1px solid #ed912c; border-radius: .75rem; padding: 1.25rem; min-width: 0 }}
.model-card {{ display: flex; flex-direction: column; background: #222 }}
.model-card h3 {{ font-size: 1.3rem; margin: 1rem 0 .15rem }}
.model-card p {{ margin: .4rem 0 }}
.location {{ align-self: flex-start; border: 2px solid #ffa44b; border-radius: .4rem;
color: #fff; background: #513016; padding: .35rem .75rem; font-weight: 700 }}
.status {{ font-weight: 650; margin-top: auto !important; padding-top: .75rem }}
.model-id, .selected {{ color: #d8d8d8; font-size: .9rem }}
.overview {{ margin-top: 2rem }} li {{ margin: .6rem 0 }}
@media (min-width: 42rem) {{ .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)) }}
.model-card {{ min-height: 17rem }} }}
@media (min-width: 68rem) {{ .grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)) }} }}
</style></head><body><header class="top"><h1>Models</h1><nav><a href="/problems">Problems</a> · <a href="/">Refresh status</a></nav></header>
<p class="selected">Selected coordinator: {text(url)}</p>
<main><div class="grid">{configured}</div>
<section class="overview"><h2>Coordinator activity</h2>{overview}</section></main></body></html>'''


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
            if path != '/' and path != '/problems' and not path.startswith('/problems/') and not path.startswith('/runs/'):
                self.send_error(404)
                return
            url = settings.selected()
            models = settings.models(url)
            client = Client(url, timeout)
            if path != '/':
                parts = path.strip('/').split('/')
                if parts[0] == 'runs' and len(parts) == 2 and problems.RUN_ID.fullmatch(parts[1]):
                    try:
                        run = client.run(parts[1])
                        body = problems.page('Run started', f'<p><a href="/runs/{parts[1]}">Run {parts[1]}</a> — {escape(str(run["status"]))}</p>'
                                             '<p>Refresh this page to check status.</p>')
                    except (CoordinatorOffline, CoordinatorInvalid) as exc:
                        body = problems.page('Run', f'<p role="alert">{escape(str(exc))}. Refresh to retry.</p>')
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
                    available = [m for m in models if activity.get(m[0], {}).get('status') in ('working', 'idle')]
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
            self.send_page(render(url, models, catalog, runs, error, activity))

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
                available = [m for m in models if activity.get(m[0], {}).get('status') in ('working', 'idle')]
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
