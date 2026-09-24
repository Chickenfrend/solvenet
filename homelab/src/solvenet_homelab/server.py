"""Small server-rendered private homelab overview."""

import argparse
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .client import Client, CoordinatorInvalid, CoordinatorOffline
from .settings import LOCATIONS, Settings


def render(url, models, catalog, runs, error=None):
    def text(value):
        return escape(str(value), quote=True)

    if error:
        activity = f'<p role="status">{text(error)}. Check the selected coordinator and try refreshing.</p>'
    else:
        sets = ''.join(f'<li>{text(row["set_id"])} v{text(row["version"])} — {text(row["problem_count"])} problems</li>'
                       for row in catalog)
        recent = ''.join(f'<li>Run {text(row["run_id"])} — {text(row["status"])}</li>' for row in runs)
        activity = (f'<h2>Fixture sets</h2><ul>{sets}</ul><h2>Recent runs</h2><ul>{recent}</ul>')
    configured = ''.join(f'<li><strong>{text(name)}</strong> ({text(model_id)}) — {text(provider)}'
                         f' · {text(location)} · Configured; availability unknown</li>'
                         for model_id, name, provider, location in models)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SolveNet homelab</title><style>
body {{ max-width: 52rem; margin: 2rem auto; padding: 0 1rem; background: #171717; color: #f8f8f8;
font: 1rem/1.6 system-ui, sans-serif; overflow-wrap: anywhere }}
section {{ border: 1px solid #ed912c; border-radius: .6rem; padding: 1rem; margin: 1rem 0 }}
h1, h2 {{ color: #ffac52 }} li {{ margin: .6rem 0 }}
</style></head><body><h1>SolveNet homelab</h1><p>Selected coordinator: {text(url)}</p>
<section><h2>Configured models</h2><ul>{configured}</ul></section>
<section><h2>Coordinator activity</h2>{activity}</section></body></html>'''


def make_server(settings, address, timeout=2):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if urlsplit(self.path).path != '/':
                self.send_error(404)
                return
            url = settings.selected()
            models = settings.models(url)
            try:
                catalog, runs = Client(url, timeout).overview()
                error = None
            except (CoordinatorOffline, CoordinatorInvalid) as exc:
                catalog, runs, error = [], [], str(exc)
            body = render(url, models, catalog, runs, error).encode('utf-8')
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
