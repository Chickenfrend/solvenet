"""Durable, coordinator-private working state for local agent groups.

No scheduler or worker lease lives here. All writes are made inside Store's
immediate transactions, including the idempotency and budget checks.
"""

import json

from . import protocol_limits as limits


MIGRATION_14 = """
CREATE TABLE agent_groups (
 id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
 statement TEXT NOT NULL, imports TEXT NOT NULL, environment TEXT NOT NULL,
 max_work INTEGER NOT NULL CHECK (max_work BETWEEN 1 AND 256),
 remaining_work INTEGER NOT NULL CHECK (remaining_work >= 0),
 max_tasks INTEGER NOT NULL CHECK (max_tasks BETWEEN 1 AND 64),
 max_messages INTEGER NOT NULL CHECK (max_messages BETWEEN 1 AND 256),
 created_at REAL NOT NULL);
CREATE TABLE group_agents (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES agent_groups(id),
 request_key TEXT NOT NULL, role TEXT NOT NULL, state TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 0, UNIQUE(group_id, request_key), UNIQUE(group_id, id));
CREATE TABLE group_state_updates (
 agent_id TEXT NOT NULL REFERENCES group_agents(id), request_key TEXT NOT NULL,
 state TEXT NOT NULL, revision INTEGER NOT NULL,
 PRIMARY KEY(agent_id, request_key));
CREATE TABLE group_tasks (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES agent_groups(id),
 request_key TEXT NOT NULL, parent_id TEXT, creator_id TEXT NOT NULL,
 owner_id TEXT NOT NULL, description TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','blocked','cancelled')),
 budget INTEGER NOT NULL CHECK (budget BETWEEN 1 AND 32),
 remaining INTEGER NOT NULL CHECK (remaining >= 0), depth INTEGER NOT NULL CHECK (depth BETWEEN 0 AND 4),
 UNIQUE(group_id, request_key), UNIQUE(group_id, id),
 FOREIGN KEY(group_id, parent_id) REFERENCES group_tasks(group_id, id),
 FOREIGN KEY(group_id, creator_id) REFERENCES group_agents(group_id, id),
 FOREIGN KEY(group_id, owner_id) REFERENCES group_agents(group_id, id));
CREATE TABLE group_runs (
 group_id TEXT PRIMARY KEY REFERENCES agent_groups(id),
 run_id TEXT NOT NULL UNIQUE REFERENCES runs(id), environment TEXT NOT NULL,
 UNIQUE(group_id, environment));
CREATE TABLE group_jobs (
 job_id TEXT PRIMARY KEY REFERENCES jobs(id), group_id TEXT NOT NULL,
 request_key TEXT NOT NULL, task_id TEXT NOT NULL, agent_id TEXT NOT NULL,
 environment TEXT NOT NULL, cost INTEGER NOT NULL CHECK (cost BETWEEN 1 AND 32),
 UNIQUE(group_id, request_key), UNIQUE(job_id, group_id, task_id, agent_id),
 FOREIGN KEY(group_id, environment) REFERENCES group_runs(group_id, environment),
 FOREIGN KEY(group_id, task_id) REFERENCES group_tasks(group_id, id),
 FOREIGN KEY(group_id, agent_id) REFERENCES group_agents(group_id, id));
CREATE TABLE group_messages (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES agent_groups(id),
 request_key TEXT NOT NULL, agent_id TEXT NOT NULL, task_id TEXT NOT NULL,
 job_id TEXT, kind TEXT NOT NULL
 CHECK (kind IN ('message','finding','question','critique')),
 text TEXT NOT NULL, verification_status TEXT NOT NULL DEFAULT 'unverified'
 CHECK (verification_status='unverified'),
 review_status TEXT NOT NULL DEFAULT 'pending'
 CHECK (review_status IN ('pending','accepted','rejected','redirected')),
 reviewer_id TEXT, review_key TEXT, review_note TEXT,
 UNIQUE(group_id, request_key), UNIQUE(group_id, id),
 FOREIGN KEY(group_id, agent_id) REFERENCES group_agents(group_id, id),
 FOREIGN KEY(group_id, task_id) REFERENCES group_tasks(group_id, id),
 FOREIGN KEY(job_id, group_id, task_id, agent_id) REFERENCES group_jobs(job_id, group_id, task_id, agent_id),
 FOREIGN KEY(group_id, reviewer_id) REFERENCES group_agents(group_id, id));
PRAGMA user_version = 14;
"""

MAX_AGENTS = 8
MAX_DEPTH = 4
MAX_STATE_UPDATES = 256
MAX_STATE_BYTES = 4096
MAX_TEXT_BYTES = 8192


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > limit:
        raise ValueError(f'{label} must be nonempty and at most {limit} bytes')
    return value


def _key(value):
    return _text(value, 'request_key', 128)


def _number(value, label, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f'{label} must be between 1 and {maximum}')
    return value


def _require(db, table, group_id, row_id):
    row = db.execute(f'SELECT * FROM {table} WHERE group_id=? AND id=?', (group_id, row_id)).fetchone()
    if row is None:
        from .store import Conflict
        raise Conflict(f'Unknown {table} in group')
    return row


def _conflict(message):
    from .store import Conflict
    raise Conflict(message)


class GroupState:
    def create_group(self, request_key, statement, imports, environment, *, max_work=32,
                     max_tasks=32, max_messages=128):
        """Create an immutable target and a bounded pool of model-call work units."""
        serialized = self._validate_group(request_key, statement, imports, environment,
                                          max_work, max_tasks, max_messages)
        with self.transaction() as db:
            return self._create_group(db, request_key, statement, serialized, environment,
                                      max_work, max_tasks, max_messages)

    def _validate_group(self, request_key, statement, imports, environment,
                        max_work, max_tasks, max_messages):
        _key(request_key)
        _text(statement, 'statement', limits.MAX_STATEMENT_BYTES)
        if not isinstance(imports, list) or not 1 <= len(imports) <= limits.MAX_IMPORTS or any(
                not isinstance(item, str) or not item.strip() or
                len(item.encode()) > limits.MAX_IMPORT_BYTES for item in imports):
            raise ValueError('Invalid imports')
        _text(environment, 'environment', 1024)
        _number(max_work, 'max_work', 256)
        _number(max_tasks, 'max_tasks', 64)
        _number(max_messages, 'max_messages', 256)
        return json.dumps(imports)

    def _create_group(self, db, request_key, statement, serialized, environment,
                      max_work, max_tasks, max_messages):
        from .store import identifier
        old = db.execute('SELECT * FROM agent_groups WHERE request_key=?', (request_key,)).fetchone()
        if old:
            if (old['statement'], old['imports'], old['environment'], old['max_work'],
                    old['max_tasks'], old['max_messages']) != (
                    statement, serialized, environment, max_work, max_tasks, max_messages):
                _conflict('Group key reused with different target or limits')
            return old['id']
        group_id = identifier()
        db.execute('''INSERT INTO agent_groups VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                   (group_id, request_key, statement, serialized, environment,
                    max_work, max_work, max_tasks, max_messages, self.clock()))
        from .claim_graph import insert_claim
        db.execute('INSERT INTO group_graphs(group_id) VALUES (?)', (group_id,))
        root_id, _ = insert_claim(db, group_id, 'target-root', statement,
                                 json.loads(serialized), environment, reason='Group target')
        db.execute('UPDATE group_graphs SET root_id=? WHERE group_id=?', (root_id, group_id))
        return group_id

    def add_agent(self, group_id, request_key, role):
        from .store import identifier
        _key(request_key)
        _text(role, 'role', 64)
        with self.transaction() as db:
            _require_group(db, group_id)
            old = db.execute('SELECT * FROM group_agents WHERE group_id=? AND request_key=?',
                             (group_id, request_key)).fetchone()
            if old:
                if old['role'] != role:
                    _conflict('Agent key reused with different role')
                return old['id']
            if db.execute('SELECT count(*) FROM group_agents WHERE group_id=?', (group_id,)).fetchone()[0] >= MAX_AGENTS:
                _conflict('Agent limit reached')
            agent_id = identifier()
            db.execute('INSERT INTO group_agents(id, group_id, request_key, role) VALUES (?, ?, ?, ?)',
                       (agent_id, group_id, request_key, role))
            return agent_id

    def update_agent_state(self, group_id, agent_id, request_key, state, expected_revision):
        """Compare-and-set working state; retries return the original revision."""
        _key(request_key)
        if not isinstance(state, str) or len(state.encode()) > MAX_STATE_BYTES:
            raise ValueError('Agent state too large')
        if type(expected_revision) is not int or not 0 <= expected_revision < MAX_STATE_UPDATES:
            raise ValueError('Invalid expected_revision')
        with self.transaction() as db:
            agent = _require(db, 'group_agents', group_id, agent_id)
            old = db.execute('SELECT * FROM group_state_updates WHERE agent_id=? AND request_key=?',
                             (agent_id, request_key)).fetchone()
            if old:
                if old['state'] != state or old['revision'] != expected_revision + 1:
                    _conflict('State update key reused')
                return old['revision']
            if agent['revision'] != expected_revision:
                _conflict('Stale agent state')
            revision = expected_revision + 1
            db.execute('UPDATE group_agents SET state=?, revision=? WHERE id=?', (state, revision, agent_id))
            db.execute('INSERT INTO group_state_updates VALUES (?, ?, ?, ?)',
                       (agent_id, request_key, state, revision))
            return revision

    def add_group_task(self, group_id, request_key, creator_id, owner_id, description,
                       budget, *, parent_id=None, claim_id=None, action='investigate'):
        from .store import identifier
        _key(request_key)
        _text(description, 'description', MAX_TEXT_BYTES)
        _number(budget, 'budget', 32)
        if action not in ('investigate', 'critique', 'prove', 'synthesize'):
            raise ValueError('Invalid claim action')
        with self.transaction() as db:
            from .claim_graph import attach_task
            group = _require_group(db, group_id)
            if claim_id is None:
                claim_id = db.execute('SELECT root_id FROM group_graphs WHERE group_id=?',
                                      (group_id,)).fetchone()[0]
            _require(db, 'group_agents', group_id, creator_id)
            _require(db, 'group_agents', group_id, owner_id)
            parent = _require(db, 'group_tasks', group_id, parent_id) if parent_id else None
            depth = parent['depth'] + 1 if parent else 0
            old = db.execute('SELECT * FROM group_tasks WHERE group_id=? AND request_key=?',
                             (group_id, request_key)).fetchone()
            if old:
                if (old['creator_id'], old['owner_id'], old['parent_id'], old['description'], old['budget']) != (
                        creator_id, owner_id, parent_id, description, budget):
                    _conflict('Task key reused with different contents')
                if claim_id is not None:
                    attach_task(db, group_id, old['id'], claim_id, action)
                return old['id']
            if depth > MAX_DEPTH or (parent and parent['status'] != 'open'):
                _conflict('Task depth or parent status exceeded')
            if db.execute('SELECT count(*) FROM group_tasks WHERE group_id=?', (group_id,)).fetchone()[0] >= group['max_tasks']:
                _conflict('Task limit reached')
            if budget > group['remaining_work']:
                _conflict('Group work budget exceeded')
            task_id = identifier()
            db.execute('''INSERT INTO group_tasks
                (id, group_id, request_key, parent_id, creator_id, owner_id, description, budget, remaining, depth)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (task_id, group_id, request_key, parent_id, creator_id, owner_id,
                 description, budget, budget, depth))
            db.execute('UPDATE agent_groups SET remaining_work=remaining_work-? WHERE id=?', (budget, group_id))
            if claim_id is not None:
                attach_task(db, group_id, task_id, claim_id, action)
            return task_id

    def set_group_task_status(self, group_id, task_id, status):
        if status not in ('done', 'blocked', 'cancelled'):
            raise ValueError('Invalid task status')
        with self.transaction() as db:
            task = _require(db, 'group_tasks', group_id, task_id)
            if task['status'] == status:
                return
            if task['status'] != 'open':
                _conflict('Task already closed')
            db.execute('UPDATE group_tasks SET status=? WHERE id=?', (status, task_id))

    def enqueue_group_job(self, group_id, task_id, agent_id, request_key, environment,
                          model, task_type, messages, *, cost=1, max_output_tokens=512):
        """Atomically create a job and reserve ``cost`` possible assignments.

        Group runs are owned exclusively by this method. The environment is a
        declared execution identity, not a claim about a worker's actual runtime.
        """
        from .store import identifier, validate_task_request
        _key(request_key)
        _text(environment, 'environment', 1024)
        _number(cost, 'cost', 32)
        validate_task_request(model, task_type, messages, max_output_tokens)
        serialized = json.dumps(messages)
        with self.transaction() as db:
            group = _require_group(db, group_id)
            task = _require(db, 'group_tasks', group_id, task_id)
            _require(db, 'group_agents', group_id, agent_id)
            if environment != group['environment']:
                _conflict('Group environment mismatch')
            old = db.execute('''SELECT gj.*, j.model, j.task_type, j.messages, j.max_output_tokens
                FROM group_jobs gj JOIN jobs j ON j.id=gj.job_id
                WHERE gj.group_id=? AND gj.request_key=?''', (group_id, request_key)).fetchone()
            if old:
                if (old['task_id'], old['agent_id'], old['environment'], old['cost'],
                        old['model'], old['task_type'], old['messages'], old['max_output_tokens']) != (
                        task_id, agent_id, environment, cost, model, task_type, serialized, max_output_tokens):
                    _conflict('Job key reused with different contents')
                return old['job_id']
            if db.execute("SELECT 1 FROM group_loops WHERE group_id=? AND phase='stopped'",
                          (group_id,)).fetchone():
                _conflict('Group loop is stopped')
            if task['owner_id'] != agent_id:
                _conflict('Acting agent is not task owner')
            if task['status'] != 'open' or cost > task['remaining']:
                _conflict('Task is closed or work budget exceeded')
            group_run = db.execute('SELECT * FROM group_runs WHERE group_id=?', (group_id,)).fetchone()
            if group_run:
                if group_run['environment'] != environment:
                    _conflict('Group run environment mismatch')
                run_id = group_run['run_id']
                run = db.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()
                if run['status'] not in ('running', 'exhausted'):
                    _conflict('Group run is terminal')
            else:
                problem_id, run_id = identifier(), identifier()
                db.execute('INSERT INTO problems VALUES (?, ?, ?)',
                           (problem_id, group['statement'], group['imports']))
                db.execute('''INSERT INTO runs(id, problem_id, status, max_repairs, created_at)
                    VALUES (?, ?, 'running', 0, ?)''', (run_id, problem_id, self.clock()))
                db.execute('INSERT INTO group_runs VALUES (?, ?, ?)', (group_id, run_id, environment))
            job_id = identifier()
            db.execute('''INSERT INTO jobs
                (id, run_id, status, model, max_output_tokens, max_assignments,
                 generation_timeout_seconds, kind, task_type, messages)
                 VALUES (?, ?, 'queued', ?, ?, ?, 120, 'model.respond', ?, ?)''',
                 (job_id, run_id, model, max_output_tokens, cost, task_type, serialized))
            db.execute('''INSERT INTO group_jobs
                (job_id, group_id, request_key, task_id, agent_id, environment, cost)
                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (job_id, group_id, request_key, task_id, agent_id, environment, cost))
            db.execute('UPDATE group_tasks SET remaining=remaining-? WHERE id=?', (cost, task_id))
            if group_run and run['status'] == 'exhausted':
                db.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
            return job_id

    def add_group_message(self, group_id, request_key, agent_id, task_id, kind, text, *, job_id=None):
        from .store import identifier
        _key(request_key)
        if kind not in ('message', 'finding', 'question', 'critique'):
            raise ValueError('Invalid message kind')
        _text(text, 'text', MAX_TEXT_BYTES)
        with self.transaction() as db:
            group = _require_group(db, group_id)
            _require(db, 'group_agents', group_id, agent_id)
            _require(db, 'group_tasks', group_id, task_id)
            if job_id:
                job = db.execute('''SELECT j.status, a.result FROM group_jobs gj
                    JOIN jobs j ON j.id=gj.job_id
                    LEFT JOIN assignments a ON a.job_id=j.id AND a.status='completed'
                    WHERE gj.job_id=? AND gj.group_id=? AND gj.task_id=? AND gj.agent_id=?
                    ORDER BY a.rowid DESC LIMIT 1''',
                    (job_id, group_id, task_id, agent_id)).fetchone()
                if job is None or job['status'] != 'done' or not job['result']:
                    _conflict('Job is not completed for this agent and task')
                output = json.loads(job['result']).get('output', {})
                if output.get('text') != text:
                    _conflict('Job-sourced message differs from completed output')
            old = db.execute('SELECT * FROM group_messages WHERE group_id=? AND request_key=?',
                             (group_id, request_key)).fetchone()
            if old:
                if (old['agent_id'], old['task_id'], old['kind'], old['text'], old['job_id']) != (
                        agent_id, task_id, kind, text, job_id):
                    _conflict('Message key reused with different contents')
                return old['id']
            if db.execute('SELECT count(*) FROM group_messages WHERE group_id=?', (group_id,)).fetchone()[0] >= group['max_messages']:
                _conflict('Message limit reached')
            message_id = identifier()
            db.execute('''INSERT INTO group_messages
                (id, group_id, request_key, agent_id, task_id, job_id, kind, text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (message_id, group_id, request_key, agent_id, task_id, job_id, kind, text))
            from .claim_graph import attach_evidence
            focus = db.execute('SELECT claim_id FROM claim_tasks WHERE group_id=? AND task_id=?',
                               (group_id, task_id)).fetchone()
            if focus:
                attach_evidence(db, group_id, focus['claim_id'], 'message', message_id)
            return message_id

    def review_group_message(self, group_id, message_id, reviewer_id, review_key, status, note=''):
        _key(review_key)
        if status not in ('accepted', 'rejected', 'redirected'):
            raise ValueError('Invalid review status')
        if not isinstance(note, str) or len(note.encode()) > MAX_STATE_BYTES:
            raise ValueError('Review note too large')
        with self.transaction() as db:
            _require(db, 'group_agents', group_id, reviewer_id)
            message = _require(db, 'group_messages', group_id, message_id)
            if message['review_status'] != 'pending':
                if (message['reviewer_id'], message['review_key'], message['review_status'], message['review_note']) != (
                        reviewer_id, review_key, status, note):
                    _conflict('Message already reviewed')
                return
            db.execute('''UPDATE group_messages SET reviewer_id=?, review_key=?, review_status=?, review_note=?
                WHERE id=?''', (reviewer_id, review_key, status, note, message_id))

    def group(self, group_id, *, verifier_identity=None):
        """Snapshot; status is historical, current_status needs an observed identity."""
        with self.connect() as db:
            db.execute('BEGIN')
            group = db.execute('SELECT * FROM agent_groups WHERE id=?', (group_id,)).fetchone()
            if group is None:
                return None
            result = dict(group)
            result['imports'] = json.loads(result['imports'])
            result['graph'] = dict(db.execute('SELECT * FROM group_graphs WHERE group_id=?',
                                              (group_id,)).fetchone())
            group_run = db.execute('SELECT run_id, environment FROM group_runs WHERE group_id=?',
                                   (group_id,)).fetchone()
            result['run'] = dict(group_run) if group_run else None
            for name, table in (('agents', 'group_agents'), ('tasks', 'group_tasks'),
                                ('messages', 'group_messages'), ('jobs', 'group_jobs'),
                                ('artifacts', 'group_artifacts')):
                result[name] = [dict(row) for row in db.execute(
                    f'SELECT * FROM {table} WHERE group_id=? ORDER BY rowid', (group_id,))]
            for artifact in result['artifacts']:
                artifact['imports'] = json.loads(artifact['imports'])
                artifact['current_status'] = (
                    'verified' if verifier_identity is not None and
                    artifact['verifier_identity'] == verifier_identity else 'needs_recheck'
                ) if artifact['status'] == 'verified' else artifact['status']
            result['routing'] = [dict(row) for row in db.execute(
                'SELECT * FROM group_route_decisions WHERE group_id=? ORDER BY rowid', (group_id,))]
            result['graph_responses'] = [dict(row) for row in db.execute(
                'SELECT * FROM graph_response_receipts WHERE group_id=? ORDER BY rowid', (group_id,))]
            for receipt in result['graph_responses']:
                receipt['result'] = json.loads(receipt['result'])
            for decision in result['routing']:
                decision['explanation'] = json.loads(decision['explanation'])
            calls = [dict(row) for row in db.execute('''SELECT gj.request_key,gj.task_id,gj.agent_id,
                j.model,j.kind,j.task_type,j.status AS job_status,a.id AS assignment_id,
                a.worker_id,a.status,a.result FROM group_jobs gj JOIN jobs j ON j.id=gj.job_id
                LEFT JOIN assignments a ON a.job_id=j.id WHERE gj.group_id=?
                ORDER BY j.rowid,a.rowid''', (group_id,))]
            totals = {'requests': len(result['jobs']), 'leases': 0, 'retries': 0,
                      'failures': 0, 'input_tokens': {'known': 0, 'unknown': 0},
                      'output_tokens': {'known': 0, 'unknown': 0},
                      'provider_duration_ns': {'known': 0, 'unknown': 0},
                      'lean_checks': 0, 'lean_elapsed_ms': {'known': 0, 'unknown': 0}}
            seen = set()
            for call in calls:
                call['result'] = json.loads(call['result']) if call['result'] else None
                if call['assignment_id'] is None:
                    continue
                totals['leases'] += 1
                totals['retries'] += call['task_id'] in seen
                seen.add(call['task_id'])
                payload = call['result'] or {}
                if call['status'] in ('rejected', 'expired') or payload.get('status') == 'failed':
                    totals['failures'] += 1
                for name, value in (('input_tokens', payload.get('usage', {}).get('input_tokens')),
                                    ('output_tokens', payload.get('usage', {}).get('output_tokens')),
                                    ('provider_duration_ns', payload.get('generation', {}).get('total_duration_ns'))):
                    bucket = totals[name]
                    if value is None:
                        bucket['unknown'] += 1
                    else:
                        bucket['known'] += value
            result['calls'] = calls
            for row in db.execute('''SELECT v.elapsed_ms, u.attempt_id AS unknown_time FROM verifications v
                LEFT JOIN group_unknown_lean_time u ON u.attempt_id=v.attempt_id
                JOIN attempts t ON t.id=v.attempt_id JOIN assignments a ON a.id=t.assignment_id
                JOIN group_jobs gj ON gj.job_id=a.job_id WHERE gj.group_id=?''', (group_id,)):
                totals['lean_checks'] += 1
                if row['unknown_time']:
                    totals['lean_elapsed_ms']['unknown'] += 1
                else:
                    totals['lean_elapsed_ms']['known'] += row['elapsed_ms']
            for row in db.execute('SELECT elapsed_ms FROM group_lean_checks WHERE group_id=?', (group_id,)):
                totals['lean_checks'] += 1
                if row['elapsed_ms'] is None:
                    totals['lean_elapsed_ms']['unknown'] += 1
                else:
                    totals['lean_elapsed_ms']['known'] += row['elapsed_ms']
            result['cost'] = totals
            return result


def _require_group(db, group_id):
    row = db.execute('SELECT * FROM agent_groups WHERE id=?', (group_id,)).fetchone()
    if row is None:
        _conflict('Unknown group')
    return row
