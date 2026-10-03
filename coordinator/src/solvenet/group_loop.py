"""Bounded coordinator-side collaboration loop.

Each advance is one SQLite transaction. A durable task and its job are inserted
together before a worker can claim it. Worker retries reuse the same job; only
completed jobs advance the group. Text from models is untrusted strategy context.
"""

import json
import logging
import math

from .group_state import _conflict, _key, _require_group, _text
from .group_artifacts import artifact_from_finding, insert_artifact
from .group_routing import validate_routing, choose

LOG = logging.getLogger(__name__)


MIGRATION_15 = """
CREATE TABLE group_loops (
 group_id TEXT PRIMARY KEY REFERENCES agent_groups(id),
 models TEXT NOT NULL, phase TEXT NOT NULL, reason TEXT,
 deadline REAL, created_at REAL NOT NULL);
PRAGMA user_version = 15;
"""

MAX_GROUP_DURATION_SECONDS = 3600

MIGRATION_17 = """
ALTER TABLE group_loops ADD COLUMN capabilities TEXT NOT NULL DEFAULT '{}';
CREATE TABLE group_route_decisions (
 group_id TEXT NOT NULL REFERENCES agent_groups(id), request_key TEXT NOT NULL,
 job_id TEXT REFERENCES jobs(id), explanation TEXT NOT NULL,
 PRIMARY KEY(group_id, request_key));
CREATE TABLE group_lean_checks (
 id INTEGER PRIMARY KEY, group_id TEXT NOT NULL REFERENCES agent_groups(id),
 artifact_id TEXT NOT NULL, status TEXT NOT NULL, elapsed_ms INTEGER,
 FOREIGN KEY(artifact_id) REFERENCES group_artifacts(id));
PRAGMA user_version = 17;
"""

MIGRATION_18 = """
ALTER TABLE worker_presence ADD COLUMN supports_model_respond INTEGER NOT NULL DEFAULT 0;
CREATE TABLE group_unknown_lean_time (
 attempt_id TEXT PRIMARY KEY REFERENCES verifications(attempt_id));
PRAGMA user_version = 18;
"""


def _parse(text, field, length):
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or field not in value:
        return None
    if set(value) != {field}:
        from .graph_response import SCHEMA
        if value.get('graph_schema') != SCHEMA:
            return None
    items = value[field]
    if not isinstance(items, list) or len(items) != length:
        return None
    return items


def _excerpt(text, max_bytes=1500):
    raw = text.encode('utf-8')
    return text if len(raw) <= max_bytes else raw[:max_bytes].decode('utf-8', 'ignore') + ' [truncated]'


def _json_excerpt(text, max_bytes):
    """Bound the encoded representation too (newlines and quotes expand in JSON)."""
    excerpt = _excerpt(text, max_bytes)
    while len(json.dumps(excerpt, ensure_ascii=False).encode('utf-8')) > max_bytes + 16:
        excerpt = _excerpt(text, max(1, len(excerpt.encode('utf-8')) // 2))
    return excerpt


def _unverified_context(findings, max_bytes=1100):
    """Keep untrusted multiline model text inside JSON strings, never prompt headings."""
    return 'UNVERIFIED_FINDINGS_JSON: ' + json.dumps([
        {'source': source, 'status': 'unverified', 'text': _json_excerpt(text, max_bytes)}
        for source, text in findings], ensure_ascii=False)


class GroupLoop:
    def start_group_loop(self, request_key, statement, imports, environment, models, *,
                         max_work=12, deadline=None, model_capabilities=None, mode='fixed', graph_limits=None):
        """Create a local group. models maps planner, investigator, critic, synthesizer.

        A single model may serve several roles, but agents remain distinct.
        Twelve work units reserve up to two leases for each of six model jobs.
        An absent deadline defaults to one hour from creation. A group waiting
        for an unavailable or proof-only worker then terminates as `deadline`.
        """
        _key(request_key)
        from .frontier import validate_limits
        if mode not in ('fixed', 'graph') or (mode == 'fixed' and graph_limits is not None):
            raise ValueError('mode must be fixed or graph; graph_limits require graph mode')
        limits = validate_limits(graph_limits) if mode == 'graph' else {}
        normalized = validate_routing(models, model_capabilities)
        capabilities = model_capabilities or {}
        if type(max_work) is not int or not 12 <= max_work <= 256:
            raise ValueError('max_work must be between 12 and 256')
        if deadline is not None and (type(deadline) not in (float, int) or
                                     not math.isfinite(deadline)):
            raise ValueError('deadline must be finite')
        now = self.clock()
        serialized = self._validate_group(request_key, statement, imports, environment,
                                          max_work, 32, 128)
        with self.transaction() as db:
            if deadline is not None and not now < deadline <= now + MAX_GROUP_DURATION_SECONDS:
                # Allow an exact idempotent retry after an existing loop's deadline.
                previous = db.execute('''SELECT gl.deadline FROM group_loops gl
                    JOIN agent_groups g ON g.id=gl.group_id WHERE g.request_key=?''',
                    (request_key,)).fetchone()
                if previous is None or previous['deadline'] != deadline:
                    raise ValueError('deadline must be in the next hour')
            group_id = self._create_group(db, request_key, statement, serialized, environment,
                                          max_work, 32, 128)
            existing = db.execute('SELECT * FROM group_loops WHERE group_id=?', (group_id,)).fetchone()
            if existing:
                requested_deadline = (existing['created_at'] + MAX_GROUP_DURATION_SECONDS
                                      if deadline is None else deadline)
                if (existing['models'] != json.dumps(models, sort_keys=True) or
                        existing['capabilities'] != json.dumps(capabilities, sort_keys=True) or
                         existing['deadline'] != requested_deadline or existing['mode'] != mode or
                         existing['limits'] != json.dumps(limits, sort_keys=True)):
                    _conflict('Loop key reused with different configuration')
                return group_id
            if deadline is None:
                deadline = now + MAX_GROUP_DURATION_SECONDS
            for key, role in (('planner', 'planner'), ('investigator-1', 'investigator'),
                              ('investigator-2', 'investigator'), ('critic', 'critic'),
                              ('synthesizer', 'synthesizer')):
                from .store import identifier
                agent = db.execute('SELECT role FROM group_agents WHERE group_id=? AND request_key=?',
                                   (group_id, key)).fetchone()
                if agent:
                    if agent['role'] != role:
                        _conflict('Loop agent role differs from existing group agent')
                else:
                    db.execute('INSERT INTO group_agents(id,group_id,request_key,role) VALUES (?,?,?,?)',
                               (identifier(), group_id, key, role))
            db.execute('''INSERT INTO group_loops
                (group_id,models,phase,reason,deadline,created_at,capabilities,mode,limits)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                       (group_id, json.dumps(models, sort_keys=True), 'frontier' if mode == 'graph' else 'plan', None, deadline, now,
                         json.dumps(capabilities, sort_keys=True), mode, json.dumps(limits, sort_keys=True)))
        return group_id

    def group_loop(self, group_id):
        with self.connect() as db:
            row = db.execute('SELECT phase,reason,deadline,models,capabilities,mode,limits FROM group_loops WHERE group_id=?',
                              (group_id,)).fetchone()
            return dict(row) | {'models': json.loads(row['models']),
                                'capabilities': json.loads(row['capabilities']),
                                'limits': json.loads(row['limits'])} if row else None

    def advance_groups(self):
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT group_id FROM group_loops WHERE phase!='stopped'")]
        changed = False
        errors = getattr(self, '_group_transition_errors', {})
        self._group_transition_errors = errors
        for group_id in list(errors):
            if group_id not in ids:
                del errors[group_id]
        overflow_count = 0
        overflow_sample = None
        for group_id in ids:
            try:
                changed = self.advance_group(group_id) or changed
            except (ValueError, TypeError, KeyError, IndexError, RuntimeError) as error:
                # Each transition's transaction has rolled back. Isolate local
                # state/response errors; database, OS and resource errors escape.
                detail = (type(error).__name__, _excerpt(str(error), 512))
                if group_id not in errors and len(errors) >= 128:
                    overflow_count += 1
                    if overflow_sample is None:
                        overflow_sample = (group_id, *detail)
                    continue
                if errors.get(group_id) != detail:
                    LOG.error('Group %s transition failed (%s): %s', group_id, *detail)
                errors[group_id] = detail
            else:
                errors.pop(group_id, None)
        if overflow_count and not getattr(self, '_group_transition_overflow', False):
            LOG.error('Group transition error cache full; %s additional failures '
                      '(sample group %s, %s: %s); further overflow logs suppressed until recovery',
                      overflow_count, *overflow_sample)
        self._group_transition_overflow = bool(overflow_count)
        return changed

    def advance_group(self, group_id):
        """Perform one durable transition; return whether anything changed."""
        with self.connect() as db:
            row = db.execute('SELECT mode FROM group_loops WHERE group_id=?', (group_id,)).fetchone()
        if row and row['mode'] == 'graph':
            return self.advance_frontier(group_id)
        from .store import identifier
        with self.transaction() as db:
            loop = db.execute('SELECT * FROM group_loops WHERE group_id=?', (group_id,)).fetchone()
            if loop is None or loop['phase'] == 'stopped':
                return False
            group = _require_group(db, group_id)
            models = json.loads(loop['models'])
            models = {role: [value] if isinstance(value, str) else value for role, value in models.items()}
            capabilities = json.loads(loop['capabilities'])
            agents = {r['request_key']: r['id'] for r in db.execute(
                'SELECT * FROM group_agents WHERE group_id=?', (group_id,))}

            def stop(reason):
                db.execute("UPDATE group_loops SET phase='stopped',reason=? WHERE group_id=?",
                           (reason, group_id))
                db.execute("""UPDATE jobs SET status='cancelled' WHERE status='queued' AND id IN
                    (SELECT job_id FROM group_jobs WHERE group_id=?)""", (group_id,))
                db.execute("""UPDATE group_tasks SET status=CASE
                    WHEN request_key='synthesize' AND ?='verified_target' THEN 'done'
                    WHEN ?='deadline' THEN 'cancelled' ELSE 'blocked' END
                    WHERE group_id=? AND status='open'""", (reason, reason, group_id))
                self._refresh(db)
                return True

            run = db.execute('SELECT r.* FROM group_runs gr JOIN runs r ON r.id=gr.run_id '
                             'WHERE gr.group_id=?', (group_id,)).fetchone()
            if run and run['status'] == 'solved':
                return stop('verified_target')
            if run and run['status'] == 'error':
                return stop('verifier_error')
            if loop['deadline'] is not None and self.clock() >= loop['deadline']:
                return stop('deadline')

            def job(key):
                return db.execute('SELECT j.* FROM group_jobs gj JOIN jobs j ON j.id=gj.job_id '
                                  'WHERE gj.group_id=? AND gj.request_key=?', (group_id, key)).fetchone()

            def output(key):
                current = job(key)
                if current is None:
                    return None
                if current['status'] == 'failed':
                    return False
                if current['status'] != 'done':
                    return None
                result = db.execute('SELECT result FROM assignments WHERE job_id=? AND status=\'completed\'',
                                    (current['id'],)).fetchone()
                return json.loads(result[0])['output']['text'] if result else None

            def dispatch(key, creator, owner, description, kind, task_type, context, *, parent=None,
                         avoid=None, proof_context=None):
                """Reserve one call and persist its decision, task and job atomically."""
                _text(description, 'description', 8192)
                _text(context, 'context', 8192)
                if job(key):
                    return False
                from .context_packet import build_packet, prompt_cost, freeze_packet
                root = db.execute('SELECT root_id FROM group_graphs WHERE group_id=?', (group_id,)).fetchone()[0]
                output_tokens = 2048 if kind == 'model.generate' else 512
                base_messages = [{'role': 'user', 'content': context}]
                role = 'investigator' if owner.startswith('investigator') else owner
                packet_context_limit = min(8192, max(capabilities.get(model, {}).get('context_tokens', 8192)
                                                     for model in models[role]))
                try:
                    built = (build_packet(self, db, group_id, root, base_messages,
                        max_output_tokens=output_tokens,
                        context_limit=packet_context_limit,
                        proof_ids=proof_context['selected_proof_ids'] if proof_context else None)
                        if root is not None else None)
                except ValueError:
                    return stop('context_limit')
                actual_messages = built['messages'] if built else base_messages
                model, cost, explanation = choose(
                    db, group_id, models, capabilities, role, task_type or 'proof',
                    prompt_cost(group['statement'], json.loads(group['imports']), actual_messages, 0),
                    group['remaining_work'], avoid=avoid, now=self.clock(), lease_seconds=self.lease_seconds,
                    context_token_bound=prompt_cost(group['statement'], json.loads(group['imports']),
                                                    actual_messages, output_tokens))
                if model is None:
                    db.execute('INSERT OR IGNORE INTO group_route_decisions VALUES (?,?,NULL,?)',
                               (group_id, key, explanation))
                    candidates = json.loads(explanation)['candidates']
                    return stop('budget' if candidates and all('budget_exceeded' in c['reasons']
                                                               for c in candidates) else 'model_unavailable_or_unfit')
                count = db.execute('SELECT count(*) FROM group_tasks WHERE group_id=?', (group_id,)).fetchone()[0]
                if count >= group['max_tasks']:
                    return stop('task_limit')
                if parent:
                    p = db.execute('SELECT depth FROM group_tasks WHERE id=? AND group_id=?',
                                   (parent, group_id)).fetchone()
                    if not p or p['depth'] >= 4:
                        return stop('task_depth')
                task_id = identifier()
                db.execute('''INSERT INTO group_tasks
                    (id,group_id,request_key,parent_id,creator_id,owner_id,description,budget,remaining,depth)
                    VALUES (?,?,?,?,?,?,?,?,0,?)''',
                    (task_id, group_id, key, parent, agents[creator], agents[owner], description,
                     cost, p['depth'] + 1 if parent else 0))
                from .claim_graph import attach_task
                root = db.execute('SELECT root_id FROM group_graphs WHERE group_id=?', (group_id,)).fetchone()[0]
                if root is not None:
                    action = 'synthesize' if kind == 'model.generate' else (
                        'critique' if task_type == 'critique' else 'investigate')
                    attach_task(db, group_id, task_id, root, action)
                db.execute('UPDATE agent_groups SET remaining_work=remaining_work-? WHERE id=?', (cost, group_id))
                nonlocal run
                if not run:
                    problem_id, run_id = identifier(), identifier()
                    db.execute('INSERT INTO problems VALUES (?,?,?)',
                               (problem_id, group['statement'], group['imports']))
                    db.execute("INSERT INTO runs(id,problem_id,status,max_repairs,created_at) VALUES (?,?, 'running',0,?)",
                               (run_id, problem_id, self.clock()))
                    db.execute('INSERT INTO group_runs VALUES (?,?,?)', (group_id, run_id, group['environment']))
                else:
                    run_id = run['id']
                    if run['status'] == 'exhausted':
                        db.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
                    elif run['status'] != 'running':
                        return stop('run_terminal')
                job_id = identifier()
                db.execute('''INSERT INTO jobs
                    (id,run_id,status,model,max_output_tokens,max_assignments,
                     generation_timeout_seconds,kind,task_type,messages)
                    VALUES (?,?,'queued',?, ?,2,120,?,?,?)''',
                     (job_id, run_id, model,
                     2048 if kind == 'model.generate' else 512, kind, task_type,
                      json.dumps(actual_messages)))
                db.execute('INSERT INTO group_jobs VALUES (?,?,?,?,?,?,?)',
                            (job_id, group_id, key, task_id, agents[owner], group['environment'], cost))
                if built is not None:
                    freeze_packet(db, job_id, group_id, task_id, built, dict(messages=base_messages))
                elif proof_context is not None:
                    from .proof_context import freeze_context
                    freeze_context(db, job_id, 'job', proof_context)
                db.execute('INSERT INTO group_route_decisions VALUES (?,?,?,?)',
                           (group_id, key, job_id, explanation))
                return True

            def task(key):
                return db.execute('SELECT * FROM group_tasks WHERE group_id=? AND request_key=?',
                                   (group_id, key)).fetchone()

            def finding_key(index):
                key = f'investigate-{index}'
                return key + '-escalate' if job(key + '-escalate') else key

            def message(key, agent, kind, text):
                current = job(key)
                if db.execute('SELECT 1 FROM group_messages WHERE group_id=? AND request_key=?',
                              (group_id, key)).fetchone():
                    return
                message_id = identifier()
                db.execute('''INSERT INTO group_messages
                    (id,group_id,request_key,agent_id,task_id,job_id,kind,text)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (message_id, group_id, key, agents[agent], task(key)['id'], current['id'], kind, text))
                from .claim_graph import attach_evidence
                focus = db.execute('SELECT claim_id FROM claim_tasks WHERE group_id=? AND task_id=?',
                                   (group_id, task(key)['id'])).fetchone()
                if focus:
                    attach_evidence(db, group_id, focus['claim_id'], 'message', message_id)
                if kind == 'finding':
                    artifact = artifact_from_finding(text)
                    if artifact:
                        insert_artifact(db, group_id, key, agents[agent], task(key)['id'],
                                        artifact['statement'], artifact['imports'],
                                        artifact['environment'], artifact['proof'], current['id'])
                db.execute("UPDATE group_tasks SET status='done' WHERE id=?", (task(key)['id'],))

            phase = loop['phase']
            if phase == 'plan':
                return dispatch('plan', 'planner', 'planner', 'Propose two distinct scoped approaches',
                                'model.respond', 'plan',
                                'Propose two distinct scoped approaches/subgoals for the target. '
                                'Return JSON {"approaches":["first task","second task"]}; each task short and distinct.') \
                    if not job('plan') else self._loop_phase(db, group_id, 'plan_wait')
            if phase == 'plan_wait':
                text = output('plan')
                if text is None:
                    return False
                approaches = _parse(text, 'approaches', 2) if text is not False else None
                if not approaches or any(not isinstance(a, str) or not a.strip() or len(a.encode()) > 2048
                                        for a in approaches) or approaches[0].strip() == approaches[1].strip():
                    return stop('invalid_plan' if text is not False else 'plan_failed')
                message('plan', 'planner', 'message', text)
                return self._loop_phase(db, group_id, 'investigate')
            if phase == 'investigate':
                approaches = _parse(output('plan'), 'approaches', 2)
                for index, approach in enumerate(approaches, 1):
                    key = f'investigate-{index}'
                    if not job(key):
                        return dispatch(key, 'planner', f'investigator-{index}', approach,
                                        'model.respond', 'finding',
                                          f'Scoped subgoal: UNTRUSTED_JSON {json.dumps(approach)}\nReport a bounded finding or question. '
                                         'A formal lemma may be proposed as JSON '
                                         '{"artifact":{"statement":": ...","imports":["Init"],'
                                         '"environment":"...","proof":"..."}}. '
                                         'Proposals remain unverified until coordinator Lean checks them. '
                                         f'Target environment: {group["environment"]}.', parent=task('plan')['id'])
                return self._loop_phase(db, group_id, 'investigate_wait')
            if phase == 'investigate_wait':
                for index in (1, 2):
                    original = f'investigate-{index}'
                    key = finding_key(index)
                    text = output(key)
                    if text is None:
                        return False
                    if text is False:
                        if key != original:
                            return stop('investigation_failed')
                        failed_model = job(key)['model']
                        db.execute("UPDATE group_tasks SET status='blocked' WHERE id=?", (task(key)['id'],))
                        return dispatch(key + '-escalate', 'planner', f'investigator-{index}',
                                        'Retry scoped subgoal after failed model call', 'model.respond', 'finding',
                                         'Retry scoped subgoal (untrusted JSON): ' + json.dumps(task(key)['description']) +
                                         '\nReturn a bounded finding.', parent=task(key)['id'],
                                        avoid=failed_model)
                    message(key, f'investigator-{index}', 'finding', text)
                return self._loop_phase(db, group_id, 'review')
            if phase == 'review':
                findings = [output(finding_key(i)) for i in (1, 2)]
                return dispatch('review', 'planner', 'critic', 'Review both findings',
                                'model.respond', 'critique',
                                 'Review these UNVERIFIED findings and redirect at least one branch. '
                                 'Return JSON {"decisions":["accept","redirect"]} (or reversed).\n'
                                  + _unverified_context([(finding_key(i), value)
                                                         for i, value in enumerate(findings, 1)])) \
                    if not job('review') else self._loop_phase(db, group_id, 'review_wait')
            if phase == 'review_wait':
                text = output('review')
                if text is None:
                    return False
                decisions = _parse(text, 'decisions', 2) if text is not False else None
                if (not decisions or any(not isinstance(d, str) or d not in ('accept', 'redirect')
                                         for d in decisions) or
                        sorted(decisions) != ['accept', 'redirect']):
                    return stop('invalid_review' if text is not False else 'review_failed')
                message('review', 'critic', 'critique', text)
                for index, decision in enumerate(decisions, 1):
                    db.execute('''UPDATE group_messages SET review_status=?,reviewer_id=?,review_key=?,review_note=?
                        WHERE group_id=? AND request_key=?''',
                        ('accepted' if decision == 'accept' else 'redirected', agents['critic'],
                          'review', text[:4096], group_id, finding_key(index)))
                return self._loop_phase(db, group_id, 'redirect')
            if phase == 'redirect':
                redirected = db.execute("SELECT gm.* FROM group_messages gm WHERE gm.group_id=? "
                                        "AND gm.review_status='redirected'", (group_id,)).fetchone()
                accepted = db.execute("SELECT gm.text FROM group_messages gm WHERE gm.group_id=? "
                                      "AND gm.review_status='accepted'", (group_id,)).fetchone()
                owner = 'investigator-2' if redirected['request_key'].startswith('investigate-1') else 'investigator-1'
                return dispatch('redirect', 'critic', owner, 'Revisit redirected branch using other finding',
                                 'model.respond', 'finding',
                                 _unverified_context([('accepted_finding', accepted[0]),
                                                      ('redirected_finding', redirected['text']),
                                                      ('critique', output('review'))])
                                 + '\nProvide a corrected bounded finding.',
                                 parent=redirected['task_id'],
                                 avoid=db.execute('''SELECT j.model FROM jobs j JOIN group_jobs gj ON gj.job_id=j.id
                                     WHERE gj.group_id=? AND gj.request_key=?''',
                                                  (group_id, redirected['request_key'])).fetchone()[0]) if not job('redirect') else self._loop_phase(db, group_id, 'redirect_wait')
            if phase == 'redirect_wait':
                text = output('redirect')
                if text is None:
                    return False
                if text is False:
                    return stop('redirect_failed')
                owner_id = db.execute('SELECT owner_id FROM group_tasks WHERE id=?',
                                      (task('redirect')['id'],)).fetchone()[0]
                owner = next(name for name, aid in agents.items() if aid == owner_id)
                message('redirect', owner, 'finding', text)
                return self._loop_phase(db, group_id, 'synthesize')
            if phase == 'synthesize':
                binding = db.execute('SELECT identity,revision FROM artifact_verifier_binding WHERE id=1').fetchone()
                root = db.execute('SELECT root_id FROM group_graphs WHERE group_id=?', (group_id,)).fetchone()[0]
                if root is not None and (
                        binding['identity'] is None or self.artifact_verifier_binding != (
                            binding['identity'], binding['revision'])):
                    return False  # even empty graph target contexts need a fresh binding
                if db.execute("SELECT 1 FROM group_artifacts WHERE group_id=? AND status='pending'",
                              (group_id,)).fetchone():
                    return False
                verified = db.execute('''SELECT id,statement,imports,environment FROM group_artifacts
                    WHERE group_id=? AND status='verified' AND verifier_identity=?
                    AND imports=? AND environment=?
                    ORDER BY rowid LIMIT 3''',
                    (group_id, binding['identity'], group['imports'], group['environment'])).fetchall()
                from .proof_context import selected_bundle
                from .store import Conflict
                proof_context = None
                # A bounded selection must not strand the fixed loop if the union
                # of individually checked closures is too large or incompatible.
                while verified and root is not None:
                    try:
                        proof_context = selected_bundle(db, group_id, root, '', [a['id'] for a in verified])
                        break
                    except (ValueError, Conflict):
                        verified = verified[:-1]
                if proof_context is None and root is not None:
                    proof_context = selected_bundle(db, group_id, root, '', [])
                context = ('Use these UNVERIFIED findings as hints only. Produce a proof body for the '
                           'original target, not an auxiliary claim. Worker labels in findings are not '
                           'verification evidence. ')
                if root is not None:
                    context += 'Consult the frozen graph context packet.'
                else:
                    # Pre-graph groups have no claim attachments or packet. Retain
                    # their original bounded handoff when resuming after migration.
                    context += _unverified_context([(finding_key(i), output(finding_key(i))) for i in (1, 2)] +
                                                   [('redirect', output('redirect'))], max_bytes=600)
                    context += '\nLEAN_VERIFIED_AUXILIARY_CLAIMS_JSON: ' + json.dumps([
                        {'id': a['id'], 'statement_summary': _json_excerpt(a['statement'], 400),
                         'status': 'verified', 'usable_as_declaration': False} for a in verified
                        if self.artifact_verifier_binding == (binding['identity'], binding['revision'])], ensure_ascii=False)
                return dispatch('synthesize', 'planner', 'synthesizer', 'Prove entire original target',
                                  'model.generate', None,
                                  context,
                                  proof_context=proof_context) \
                    if not job('synthesize') else self._loop_phase(db, group_id, 'synthesis_wait')
            if phase == 'synthesis_wait':
                current = job('synthesize')
                if current['status'] in ('queued', 'assigned', 'verifying'):
                    return False
                # Only the real verifier can set the run to solved. A finished
                # auxiliary proof or rejected candidate is never sufficient.
                return stop('no_verified_target')
            raise RuntimeError(f'Unknown group loop phase: {phase}')

    @staticmethod
    def _loop_phase(db, group_id, phase):
        db.execute('UPDATE group_loops SET phase=? WHERE group_id=?', (phase, group_id))
        return True
