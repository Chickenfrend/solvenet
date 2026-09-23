"""Local coordinator HTTP API and single verification loop."""

import argparse
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlsplit

from . import protocol_limits as limits
from .experiment_summary import markdown
from .problem_set import load_experiment_set
from .sandbox import (
    DEFAULT_CONTAINER_TIMEOUT_SECONDS,
    ContainerVerifier,
    ContainerVerifierConfig,
)
from .store import (
    DEFAULT_FAILURE_CLASS,
    DEFAULT_INITIAL_ATTEMPTS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MODEL,
    FAILURE_CLASSES,
    FAILURE_CATEGORIES,
    DEFAULT_MAX_ASSIGNMENTS,
    MAX_ASSIGNMENTS,
    MAX_INITIAL_JOBS,
    MAX_REPAIRS,
    REJECTION_KINDS,
    Conflict,
    Store,
    validate_settings,
)
from .verifier import (
    DEFAULT_LEAN_TIMEOUT_SECONDS,
    LeanVerifier,
    LeanVerifierConfig,
    VerificationResult,
    VerificationStatus,
)

LOG = logging.getLogger(__name__)
IDENTIFIER_RE = re.compile(r'^[0-9a-f]{32}$')


def text(value, field, limit):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise ValueError(f"{field} must be a nonempty string of at most {limit} bytes")
    return value


def integer(value, field, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{field} must be an integer between 1 and {maximum}")
    return value


def initial_job_options(data, default_attempts=DEFAULT_INITIAL_ATTEMPTS, default_model=DEFAULT_MODEL):
    """Parse either the legacy initial-job fields or explicit job groups."""
    if 'initial_jobs' in data:
        if any(field in data for field in ('attempts', 'model', 'max_output_tokens')):
            raise ValueError('initial_jobs cannot be combined with attempts, model, or max_output_tokens')
        if data['initial_jobs'] is None:
            raise ValueError('initial_jobs must be a nonempty list of groups')
        return DEFAULT_INITIAL_ATTEMPTS, DEFAULT_MODEL, DEFAULT_MAX_OUTPUT_TOKENS, data['initial_jobs']
    return (integer(data.get('attempts', default_attempts), 'attempts', MAX_INITIAL_JOBS),
            text(data.get('model', default_model), 'model', limits.MAX_MODEL_BYTES),
            integer(data.get('max_output_tokens', DEFAULT_MAX_OUTPUT_TOKENS),
                    'max_output_tokens', limits.MAX_OUTPUT_TOKENS), None)


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
    strings = {'raw_response': limits.MAX_RAW_RESPONSE_BYTES,
               'model': limits.MAX_MODEL_BYTES, 'finish_reason': limits.MAX_FINISH_REASON_BYTES,
               'model_digest': 256}
    numbers = {'total_duration_ns', 'load_duration_ns', 'prompt_eval_duration_ns',
               'eval_duration_ns', 'context_length', 'max_output_tokens'}
    for key, value in generation.items():
        if key in strings:
            if not isinstance(value, str) or len(value.encode()) > strings[key]:
                raise ValueError(f'generation.{key} must be a string of at most {strings[key]} bytes')
            if key == 'model_digest' and not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
                raise ValueError('generation.model_digest must be a SHA-256 digest')
        elif key == 'raw_response_truncated':
            if type(value) is not bool:
                raise ValueError('generation.raw_response_truncated must be boolean')
        elif key in ('temperature', 'seed'):
            validate_settings({key: value})
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

        def respond(self, code, value=None, headers=None):
            body = json.dumps(value).encode() if value is not None else b''
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            for name, header_value in (headers or {}).items():
                self.send_header(name, header_value)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def respond_markdown(self, value):
            body = value.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/markdown; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def send_error(self, code, message=None, explain=None):
            """Keep errors generated by BaseHTTPRequestHandler on the JSON API."""
            if code == 501:
                code = 405
                message = 'Method not allowed'
            message = message or self.responses.get(code, ('HTTP error',))[0]
            headers = {'Allow': 'GET, HEAD, POST'} if code == 405 else None
            self.respond(code, {'error': message}, headers)

        def segments(self):
            try:
                raw_path = urlsplit(self.path).path
            except ValueError as error:
                raise ValueError('Malformed request target') from error
            raw_segments = raw_path.split('/')[1:] if raw_path.startswith('/') else raw_path.split('/')
            segments = []
            for raw_segment in raw_segments:
                if re.search(r'%(?![0-9A-Fa-f]{2})', raw_segment):
                    raise ValueError('Malformed percent encoding in path')
                try:
                    segment = unquote_to_bytes(raw_segment).decode('utf-8')
                except UnicodeDecodeError as error:
                    raise ValueError('Path must use valid UTF-8') from error
                segments.append(segment)
            return segments

        def identifier(self, value):
            return value if IDENTIFIER_RE.fullmatch(value) else None

        def read_json(self, limit):
            raw_size = self.headers.get('Content-Length')
            if raw_size is None:
                self.respond(411, {'error': 'Content-Length is required'})
                return None
            try:
                size = int(raw_size)
            except ValueError:
                self.respond(400, {'error': 'Content-Length must be an integer'})
                return None
            if size <= 0:
                self.respond(400, {'error': 'Body must not be empty'})
                return None
            if size > limit:
                self.respond(413, {'error': f'Body must be at most {limit} bytes'})
                return None
            try:
                data = json.loads(self.rfile.read(size))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.respond(400, {'error': 'Body must be valid UTF-8 JSON'})
                return None
            if not isinstance(data, dict):
                self.respond(400, {'error': 'Expected JSON object'})
                return None
            return data

        def do_GET(self):
            try:
                parts = self.segments()
                if parts == ['health']:
                    return self.respond(200, {'status': 'ok'})
                if parts == ['ready']:
                    readiness = coordinator.verifier.readiness()
                    value = {'status': 'ready' if readiness.ready else 'unavailable'}
                    if readiness.diagnostics:
                        value['diagnostics'] = readiness.diagnostics
                    return self.respond(200 if readiness.ready else 503, value)
                if (len(parts) == 3 and parts[:2] == ['v1', 'runs']
                        and self.identifier(parts[2])):
                    run = coordinator.store.run(parts[2])
                    return self.respond(200 if run else 404,
                                        run or {'error': 'Unknown run'})
                if (len(parts) == 3 and parts[:2] == ['v1', 'experiments']
                        and self.identifier(parts[2])):
                    experiment = coordinator.store.experiment(parts[2])
                    return self.respond(200 if experiment else 404,
                                        experiment or {'error': 'Unknown experiment'})
                if (len(parts) == 4 and parts[:2] == ['v1', 'experiments']
                        and self.identifier(parts[2]) and parts[3] in ('summary', 'summary.md')):
                    report = coordinator.store.experiment_summary(parts[2])
                    if report is None:
                        return self.respond(404, {'error': 'Unknown experiment'})
                    if parts[3] == 'summary.md':
                        return self.respond_markdown(markdown(report))
                    return self.respond(200, report)
                self.respond(404, {'error': 'Unknown endpoint'})
            except ValueError as error:
                self.respond(400, {'error': str(error)})
            except Exception:
                LOG.exception('HTTP request failed')
                self.respond(500, {'error': 'Internal coordinator error'})

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            try:
                parts = self.segments()
                # Candidate + raw response may both expand under JSON escaping.
                is_result = (len(parts) == 4 and parts[:2] == ['v1', 'assignments']
                             and self.identifier(parts[2]) and parts[3] == 'result')
                limit = limits.MAX_RESULT_REQUEST_BYTES if is_result else limits.MAX_REQUEST_BYTES
                data = self.read_json(limit)
                if data is None:
                    return
                if parts == ['v1', 'experiments']:
                    allowed = {'idempotency_key', 'set_id', 'version', 'sha256', 'strategy',
                                'model', 'initial_jobs', 'attempts', 'max_repairs',
                                'max_output_tokens', 'generation_timeout_seconds', 'max_assignments',
                                'generation_settings'}
                    unknown = set(data) - allowed
                    if unknown:
                        raise ValueError(f'Unknown experiment fields: {", ".join(sorted(unknown))}')
                    key = text(data.get('idempotency_key'), 'idempotency_key', 256)
                    fixture = load_experiment_set(data.get('set_id'), data.get('version'),
                                                  data.get('sha256'))
                    strategy = data.get('strategy')
                    if strategy not in ('independent', 'repair'):
                        raise ValueError('strategy must be independent or repair')
                    depth = data.get('max_repairs', 0 if strategy == 'independent' else 2)
                    if type(depth) is not int or not 0 <= depth <= MAX_REPAIRS or (
                            strategy == 'independent' and depth != 0) or (
                            strategy == 'repair' and depth == 0):
                        raise ValueError('max_repairs must match strategy (0 for independent, 1-2 for repair)')
                    attempts, model, budget, groups = initial_job_options(
                        data, default_attempts=DEFAULT_INITIAL_ATTEMPTS if strategy == 'independent' else 1,
                        default_model=None)
                    if groups is None:
                        groups = [{'model': model, 'count': attempts, 'max_output_tokens': budget}]
                    config = {'set_id': fixture.set_id, 'version': fixture.version,
                              'sha256': fixture.sha256, 'environment': fixture.environment,
                              'strategy': strategy, 'initial_jobs': groups, 'max_repairs': depth,
                              'generation_timeout_seconds': integer(
                                  data.get('generation_timeout_seconds', 120),
                                  'generation_timeout_seconds', limits.MAX_GENERATION_TIMEOUT_SECONDS),
                              'max_assignments': integer(data.get('max_assignments', DEFAULT_MAX_ASSIGNMENTS),
                                                          'max_assignments', MAX_ASSIGNMENTS)}
                    if 'generation_settings' in data:
                        config['generation_settings'] = data['generation_settings']
                    experiment, created = coordinator.store.create_experiment(key, config, fixture.problems)
                    return self.respond(201 if created else 200, experiment)
                if parts == ['v1', 'runs']:
                    if 'generation_settings' in data and data['generation_settings'] is None:
                        raise ValueError('generation_settings must be an object')
                    statement = text(data.get('statement'), 'statement', limits.MAX_STATEMENT_BYTES)
                    imports = data.get('imports', ['Init'])
                    if not isinstance(imports, list) or not 1 <= len(imports) <= limits.MAX_IMPORTS:
                        raise ValueError(f'imports must be a nonempty list of up to {limits.MAX_IMPORTS} modules')
                    for module in imports:
                        text(module, 'import', limits.MAX_IMPORT_BYTES)
                    attempts, model, budget, initial_jobs = initial_job_options(data)
                    return self.respond(201, coordinator.store.submit(
                        statement, imports,
                        # v1 `attempts` counts initial search chains/jobs, not
                        # completed candidates in run inspection's attempts[].
                        attempts, model, budget,
                        max_repairs=data.get('max_repairs', 0),
                        generation_timeout_seconds=integer(
                            data.get('generation_timeout_seconds', 120),
                            'generation_timeout_seconds', limits.MAX_GENERATION_TIMEOUT_SECONDS),
                        max_assignments=integer(
                            data.get('max_assignments', DEFAULT_MAX_ASSIGNMENTS),
                            'max_assignments', MAX_ASSIGNMENTS),
                        initial_jobs=initial_jobs,
                        generation_settings=data.get('generation_settings')))
                if parts == ['v1', 'claim']:
                    worker = text(data.get('worker_id'), 'worker_id', limits.MAX_IDENTIFIER_BYTES)
                    models = data.get('models')
                    if not isinstance(models, list) or not 1 <= len(models) <= limits.MAX_CLAIM_MODELS:
                        raise ValueError(f'models must be a nonempty list of up to {limits.MAX_CLAIM_MODELS} identifiers')
                    for model in models:
                        text(model, 'model', limits.MAX_MODEL_BYTES)
                    capabilities = data.get('capabilities', [])
                    if (not isinstance(capabilities, list) or
                            capabilities not in ([], ['generation_settings'])):
                        raise ValueError('capabilities must be [] or ["generation_settings"]')
                    claim = coordinator.store.claim(
                        worker, models,
                        supports_generation_settings='generation_settings' in capabilities)
                    return self.respond(200 if claim else 204, claim)
                if (len(parts) == 4 and parts[:2] == ['v1', 'assignments']
                        and self.identifier(parts[2])
                        and parts[3] in ('heartbeat', 'result')):
                    token = text(data.get('lease_token'), 'lease_token', limits.MAX_IDENTIFIER_BYTES)
                    if parts[3] == 'heartbeat':
                        return self.respond(200, coordinator.store.heartbeat(parts[2], token))
                    if parts[3] == 'result':
                        validate_generation(data)
                        if data.get('status') == 'completed':
                            if not isinstance(data.get('output'), dict):
                                raise ValueError('output must be an object')
                            text(data['output'].get('text'), 'output.text', limits.MAX_CANDIDATE_BYTES)
                        elif data.get('status') == 'failed':
                            text(data.get('error'), 'error', limits.MAX_ERROR_BYTES)
                            failure_class = data.get('failure_class', DEFAULT_FAILURE_CLASS)
                            if failure_class not in FAILURE_CLASSES:
                                raise ValueError('failure_class must be transient or permanent')
                            if ('failure_category' in data and
                                    data['failure_category'] not in FAILURE_CATEGORIES):
                                raise ValueError('failure_category must be provider_failure, formatting_failure, or other_failure')
                        elif data.get('status') == 'rejected':
                            text(data.get('error'), 'error', limits.MAX_ERROR_BYTES)
                            if data.get('rejection_kind') not in REJECTION_KINDS:
                                raise ValueError(
                                    'rejection_kind must be malformed_assignment or unsupported_protocol')
                        else:
                            raise ValueError('status must be completed, failed, or rejected')
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
