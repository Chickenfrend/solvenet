"""Narrow, bounded coordinator HTTP client."""

import json
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen


class CoordinatorError(Exception):
    pass


class CoordinatorOffline(CoordinatorError):
    pass


class CoordinatorInvalid(CoordinatorError):
    pass


class Client:
    def __init__(self, origin, timeout=2):
        self.origin = origin
        self.timeout = timeout

    def get(self, path, required):
        request = Request(self.origin + path, headers={'Accept': 'application/json'})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                if response.headers.get_content_type() != 'application/json':
                    raise CoordinatorInvalid('Coordinator returned an invalid response')
                raw = response.read(256 * 1024 + 1)
        except HTTPError as error:
            error.close()
            if error.code < 500:
                raise CoordinatorInvalid('Coordinator returned an incompatible response') from error
            raise CoordinatorOffline('Coordinator is unavailable') from error
        except (HTTPException, URLError, TimeoutError, OSError) as error:
            raise CoordinatorOffline('Coordinator is unavailable') from error
        if len(raw) > 256 * 1024:
            raise CoordinatorInvalid('Coordinator returned an invalid response')
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise CoordinatorInvalid('Coordinator returned an invalid response') from error
        if not isinstance(result, dict) or not all(key in result for key in required):
            raise CoordinatorInvalid('Coordinator returned an invalid response')
        return result

    def overview(self):
        catalog = self.get('/v1/fixture-sets', ('items',))['items']
        runs = self.get('/v1/runs?limit=10', ('items', 'next_cursor'))['items']
        if (not isinstance(catalog, list) or not isinstance(runs, list)
                or len(catalog) > 100 or len(runs) > 10
                or any(not isinstance(row, dict) or not all(key in row for key in ('set_id', 'version', 'problem_count'))
                       for row in catalog)
                or any(not isinstance(row, dict) or not all(key in row for key in ('run_id', 'status'))
                       for row in runs)):
            raise CoordinatorInvalid('Coordinator returned an invalid response')
        return catalog, runs

    def model_activity(self, model_ids):
        activity = {}
        for start in range(0, len(model_ids), 10):
            batch = model_ids[start:start + 10]
            rows = self.get('/v1/model-activity?' + urlencode([('model', model) for model in batch]),
                            ('items',))['items']
            if (not isinstance(rows, list) or len(rows) != len(batch)
                    or any(not isinstance(row, dict) or row.get('model') not in batch
                           or row.get('status') not in ('working', 'idle', 'offline', 'unknown')
                           or (row.get('status') == 'working' and
                               (not isinstance(row.get('job_id'), str) or not row['job_id']
                                or not isinstance(row.get('run_id'), str) or not row['run_id']))
                           for row in rows)
                    or {row['model'] for row in rows} != set(batch)):
                raise CoordinatorInvalid('Coordinator returned an invalid response')
            activity.update((row['model'], row) for row in rows)
        return activity

    def fixture_set(self, set_id, version):
        path = f'/v1/fixture-sets/{quote(set_id, safe="")}/versions/{version}'
        problems = []
        offset = 0
        while True:
            result = self.get(path + f'?limit=100&offset={offset}',
                              ('problems', 'sha256', 'environment', 'next_offset'))
            page = result['problems']
            next_offset = result['next_offset']
            if (not isinstance(page, list) or len(page) > 100
                    or any(not isinstance(p, dict) or not isinstance(p.get('id'), str)
                           or not isinstance(p.get('title'), str) for p in page)
                    or (next_offset is not None and (type(next_offset) is not int
                        or next_offset != offset + 100 or next_offset > 10000))):
                raise CoordinatorInvalid('Coordinator returned an invalid response')
            problems.extend(page)
            if next_offset is None:
                return {**result, 'problems': problems}
            offset = next_offset

    def problem(self, set_id, version, problem_id):
        path = (f'/v1/fixture-sets/{quote(set_id, safe="")}/versions/{version}'
                f'/problems/{quote(problem_id, safe="")}')
        result = self.get(path, ('id', 'title', 'statement', 'imports', 'environment',
                                  'sha256', 'set_id', 'version'))
        if (result['id'] != problem_id or not isinstance(result['title'], str)
                or result['set_id'] != set_id or result['version'] != version
                or not isinstance(result['statement'], str) or not isinstance(result['sha256'], str)
                or not isinstance(result['imports'], list)
                or any(not isinstance(value, str) for value in result['imports'])):
            raise CoordinatorInvalid('Coordinator returned an invalid response')
        return result

    def run(self, run_id):
        return self.get('/v1/runs/' + quote(run_id, safe=''), ('id', 'status'))

    def start_fixture_run(self, payload):
        request = Request(self.origin + '/v1/fixture-runs', data=json.dumps(payload).encode(),
                          headers={'Accept': 'application/json', 'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                if response.status != 201:
                    raise CoordinatorInvalid('Coordinator returned an incompatible response')
                if response.headers.get_content_type() != 'application/json':
                    raise CoordinatorInvalid('Coordinator returned an invalid response')
                raw = response.read(256 * 1024 + 1)
        except HTTPError as error:
            error.close()
            if error.code == 400:
                raise CoordinatorInvalid('Coordinator rejected the fixture or settings; refresh and try again') from error
            if error.code < 500:
                raise CoordinatorInvalid('Coordinator returned an incompatible response') from error
            raise CoordinatorOffline('Coordinator is unavailable') from error
        except (HTTPException, URLError, TimeoutError, OSError) as error:
            raise CoordinatorOffline('Coordinator is unavailable') from error
        try:
            result = json.loads(raw) if len(raw) <= 256 * 1024 else None
        except (ValueError, UnicodeDecodeError) as error:
            raise CoordinatorInvalid('Coordinator returned an invalid response') from error
        if (not isinstance(result, dict) or not isinstance(result.get('run_id'), str)
                or len(result['run_id']) != 32 or any(c not in '0123456789abcdef' for c in result['run_id'])):
            raise CoordinatorInvalid('Coordinator returned an invalid response')
        return result['run_id']
