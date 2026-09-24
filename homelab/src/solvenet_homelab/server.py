"""Small server-rendered private homelab overview."""

import argparse
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .client import Client, CoordinatorInvalid, CoordinatorOffline
from .settings import LOCATIONS, Settings


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
</style></head><body><header class="top"><h1>Models</h1><a href="/">Refresh status</a></header>
<p class="selected">Selected coordinator: {text(url)}</p>
<main><div class="grid">{configured}</div>
<section class="overview"><h2>Coordinator activity</h2>{overview}</section></main></body></html>'''


def make_server(settings, address, timeout=2):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if urlsplit(self.path).path != '/':
                self.send_error(404)
                return
            url = settings.selected()
            models = settings.models(url)
            client = Client(url, timeout)
            try:
                catalog, runs = client.overview()
                error = None
            except (CoordinatorOffline, CoordinatorInvalid) as exc:
                catalog, runs, error = [], [], str(exc)
            try:
                activity = client.model_activity([row[0] for row in models]) if error is None else {}
            except (CoordinatorOffline, CoordinatorInvalid):
                activity = {}
            body = render(url, models, catalog, runs, error, activity).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
