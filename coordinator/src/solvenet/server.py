"""Local coordinator HTTP API and single verification loop."""

import argparse
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .sandbox import (
    DEFAULT_CONTAINER_TIMEOUT_SECONDS,
    ContainerVerifier,
    ContainerVerifierConfig,
)
from .store import MAX_GENERATION_TIMEOUT_SECONDS, Conflict, Store
from .verifier import (
    DEFAULT_LEAN_TIMEOUT_SECONDS,
    LeanVerifier,
    LeanVerifierConfig,
    VerificationResult,
    VerificationStatus,
)

LOG = logging.getLogger(__name__)


def text(value, field, limit=65536):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise ValueError(f"{field} must be a nonempty string of at most {limit} bytes")
    return value


def integer(value, field, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{field} must be an integer between 1 and {maximum}")
    return value


def validate_generation(data):
    """Metadata is accepted for successes AND execution/formatting failures."""
    usage = data.get('usage', {})
    if not isinstance(usage, dict):
        raise ValueError('usage must be an object')
    for key, value in usage.items():
        if key not in ('input_tokens', 'output_tokens') or (value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1)):
            raise ValueError('usage must contain nonnegative token counts or null')
    generation = data.get('generation', {})
    if not isinstance(generation, dict):
        raise ValueError('generation must be an object')
    strings = {'raw_response': 128 * 1024, 'model': 256, 'finish_reason': 256}
    numbers = {'total_duration_ns', 'load_duration_ns', 'prompt_eval_duration_ns',
               'eval_duration_ns', 'context_length', 'max_output_tokens'}
    for key, value in generation.items():
        if key in strings:
            if not isinstance(value, str) or len(value.encode()) > strings[key]:
                raise ValueError(f'generation.{key} must be a string of at most {strings[key]} bytes')
        elif key == 'raw_response_truncated':
            if type(value) is not bool:
                raise ValueError('generation.raw_response_truncated must be boolean')
        elif key in numbers:
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                raise ValueError(f'generation.{key} must be a nonnegative integer')
        else:
            raise ValueError(f'Unknown generation field: {key}')


class Coordinator:
    def __init__(self, store, verifier):
        self.store = store
        self.verifier = verifier

    def tick(self):
        self.store.expire()
        attempt = self.store.pending()
        if not attempt:
            return False
        try:
            result = self.verifier.verify(attempt['statement'], attempt['candidate'],
                                          imports=json.loads(attempt['imports']))
        except Exception:
            LOG.exception("Verifier failed")
            result = VerificationResult(VerificationStatus.VERIFIER_ERROR, "Verifier raised an internal error; see coordinator logs", 0)
        self.store.verified(attempt['id'], result)
        return True

    def loop(self, stop):
        while not stop.is_set():
            try:
                if self.tick():
                    continue
            except Exception:
                LOG.exception("Scheduler iteration failed")
            stop.wait(0.25)


def make_server(coordinator, address=('127.0.0.1', 8080)):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def respond(self, code, value=None):
            body = json.dumps(value).encode() if value is not None else b''
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/health':
                return self.respond(200, {'status': 'ok'})
            if self.path.startswith('/v1/runs/'):
                run = coordinator.store.run(self.path.removeprefix('/v1/runs/'))
                return self.respond(200 if run else 404, run or {'error': 'Unknown run'})
            self.respond(404, {'error': 'Unknown endpoint'})

        def do_POST(self):
            try:
                size = int(self.headers.get('Content-Length', '0'))
                # Candidate + raw response may both expand under JSON escaping.
                limit = 2 * 1024 * 1024 if self.path.startswith('/v1/assignments/') and self.path.endswith('/result') else 256 * 1024
                if not 0 < size <= limit:
                    return self.respond(413, {'error': f'Body must be 1–{limit} bytes'})
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError('Expected JSON object')
                if self.path == '/v1/runs':
                    statement = text(data.get('statement'), 'statement')
                    imports = data.get('imports', ['Init'])
                    if not isinstance(imports, list) or not 1 <= len(imports) <= 32:
                        raise ValueError('imports must be a nonempty list of up to 32 modules')
                    for module in imports:
                        text(module, 'import', 256)
                    return self.respond(201, coordinator.store.submit(
                        statement, imports,
                        integer(data.get('attempts', 3), 'attempts', 100),
                        text(data.get('model', 'scripted'), 'model', 256),
                        integer(data.get('max_output_tokens', 2048), 'max_output_tokens', 32768),
                        max_repairs=data.get('max_repairs', 0),
                        generation_timeout_seconds=integer(
                            data.get('generation_timeout_seconds', 120),
                            'generation_timeout_seconds', MAX_GENERATION_TIMEOUT_SECONDS)))
                if self.path == '/v1/claim':
                    worker = text(data.get('worker_id'), 'worker_id', 256)
                    models = data.get('models')
                    if not isinstance(models, list) or not 1 <= len(models) <= 32:
                        raise ValueError('models must be a nonempty list of up to 32 identifiers')
                    for model in models:
                        text(model, 'model', 256)
                    claim = coordinator.store.claim(worker, models)
                    return self.respond(200 if claim else 204, claim)
                parts = self.path.strip('/').split('/')
                if len(parts) == 4 and parts[:2] == ['v1', 'assignments']:
                    token = text(data.get('lease_token'), 'lease_token', 256)
                    if parts[3] == 'heartbeat':
                        return self.respond(200, coordinator.store.heartbeat(parts[2], token))
                    if parts[3] == 'result':
                        validate_generation(data)
                        if data.get('status') == 'completed':
                            if not isinstance(data.get('output'), dict):
                                raise ValueError('output must be an object')
                            text(data['output'].get('text'), 'output.text', 128 * 1024)
                        elif data.get('status') == 'failed':
                            text(data.get('error'), 'error', 4096)
                        else:
                            raise ValueError('status must be completed or failed')
                        return self.respond(200, coordinator.store.result(parts[2], data))
                self.respond(404, {'error': 'Unknown endpoint'})
            except Conflict as error:
                self.respond(409, {'error': str(error)})
            except (ValueError, KeyError, TypeError) as error:
                self.respond(400, {'error': str(error)})
            except Exception:
                LOG.exception('HTTP request failed')
                self.respond(500, {'error': 'Internal coordinator error'})

    return ThreadingHTTPServer(address, Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path('solvenet.db'))
    parser.add_argument('--project', type=Path, default=Path('lean'))
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--verifier', choices=('docker', 'local'), default='docker')
    parser.add_argument('--image', default='solvenet-verifier:local')
    parser.add_argument(
        '--lean-timeout', type=float, default=DEFAULT_LEAN_TIMEOUT_SECONDS,
        help='total Lean verification deadline in seconds (default: 10)',
    )
    parser.add_argument(
        '--container-timeout', type=float, default=DEFAULT_CONTAINER_TIMEOUT_SECONDS,
        help='outer Docker deadline in seconds (default: 30)',
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        verifier_config = LeanVerifierConfig(timeout_seconds=args.lean_timeout)
    except ValueError as error:
        parser.error(str(error))
    if args.verifier == 'docker':
        try:
            verifier = ContainerVerifier(
                args.image,
                verifier_config=verifier_config,
                container_config=ContainerVerifierConfig(
                    deadline_seconds=args.container_timeout,
                ),
            )
        except ValueError as error:
            parser.error(str(error))
    else:
        verifier = LeanVerifier(args.project, config=verifier_config)
    coordinator = Coordinator(Store(args.db), verifier)
    stop = threading.Event()
    thread = threading.Thread(target=coordinator.loop, args=(stop,), daemon=True)
    server = make_server(coordinator, ('127.0.0.1', args.port))
    thread.start()
    LOG.info('Coordinator listening on http://127.0.0.1:%s', args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        stop.set()
        thread.join(timeout=40)


if __name__ == '__main__':
    main()
