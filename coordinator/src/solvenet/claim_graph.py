"""Bounded coordinator-only graph writes; suggestions confer no proof authority."""

import json

from . import protocol_limits as limits
from .group_state import _conflict, _key, _require, _require_group, _text
from .claim_graph_read import ClaimGraphRead

MAX_CLAIMS = 64
MAX_PUBLICATIONS = 256
MAX_RELATIONSHIPS = 256
MAX_REVIEWS = 256
MAX_EVIDENCE_LINKS = 512
MAX_REASON_BYTES = 2048


def _capacity(db, table, group_id, maximum):
    if db.execute(f'SELECT count(*) FROM {table} WHERE group_id=?',
                  (group_id,)).fetchone()[0] >= maximum:
        _conflict(f'{table} limit reached')


def _reason(reason):
    if not isinstance(reason, str) or len(reason.encode()) > MAX_REASON_BYTES:
        raise ValueError('Graph reason too large')


def _context(statement, imports, environment):
    _text(statement, 'claim statement', limits.MAX_STATEMENT_BYTES)
    _text(environment, 'claim environment', 1024)
    if (not isinstance(imports, list) or not 1 <= len(imports) <= limits.MAX_IMPORTS or
            any(not isinstance(i, str) or not i.strip() or
                len(i.encode()) > limits.MAX_IMPORT_BYTES for i in imports)):
        raise ValueError('Invalid claim imports')
    return json.dumps(imports)


def _source(db, group_id, agent_id, task_id, job_id):
    """Resolve actual completed assignment, never accept a model's identity."""
    if (agent_id is None) != (task_id is None):
        _conflict('Graph source needs both agent and task')
    if agent_id is not None:
        _require(db, 'group_agents', group_id, agent_id)
        task = _require(db, 'group_tasks', group_id, task_id)
        if task['owner_id'] != agent_id:
            _conflict('Graph source agent is not task owner')
    if job_id is None:
        return 'coordinator', agent_id, task_id, None, None
    row = db.execute('''SELECT a.id FROM group_jobs gj JOIN jobs j ON j.id=gj.job_id
        JOIN assignments a ON a.job_id=j.id AND a.status='completed'
        WHERE gj.group_id=? AND gj.task_id=? AND gj.agent_id=? AND gj.job_id=?
        AND j.status='done' AND a.result IS NOT NULL ORDER BY a.rowid DESC LIMIT 1''',
        (group_id, task_id, agent_id, job_id)).fetchone()
    if row is None:
        _conflict('Graph source is not a completed job for this agent and task')
    return 'job', agent_id, task_id, job_id, row['id']


def insert_claim(db, group_id, request_key, statement, imports, environment, *,
                 agent_id=None, task_id=None, job_id=None, reason=''):
    from .store import identifier
    _key(request_key)
    _reason(reason)
    serialized = _context(statement, imports, environment)
    _require_group(db, group_id)
    source = _source(db, group_id, agent_id, task_id, job_id)
    old = db.execute('''SELECT p.*,c.statement,c.imports,c.environment FROM claim_publications p
        JOIN group_claims c ON c.id=p.claim_id WHERE p.group_id=? AND p.request_key=?''',
        (group_id, request_key)).fetchone()
    if old:
        if tuple(old[k] for k in ('statement', 'imports', 'environment', 'source',
                                 'agent_id', 'task_id', 'job_id', 'assignment_id', 'reason')) != (
                statement, serialized, environment, *source, reason):
            _conflict('Claim publication key reused with different contents')
        return old['claim_id'], old['id']
    _capacity(db, 'claim_publications', group_id, MAX_PUBLICATIONS)
    claim = db.execute('''SELECT id FROM group_claims
        WHERE group_id=? AND statement=? AND imports=? AND environment=?''',
        (group_id, statement, serialized, environment)).fetchone()
    if claim:
        claim_id = claim['id']
    else:
        _capacity(db, 'group_claims', group_id, MAX_CLAIMS)
        claim_id = identifier()
        db.execute('INSERT INTO group_claims VALUES (?,?,?,?,?)',
                   (claim_id, group_id, statement, serialized, environment))
    publication_id = identifier()
    db.execute('INSERT INTO claim_publications VALUES (?,?,?,?,?,?,?,?,?,?)',
               (publication_id, group_id, request_key, claim_id, *source, reason))
    return claim_id, publication_id


def attach_task(db, group_id, task_id, claim_id, action):
    if action not in ('investigate', 'critique', 'prove', 'synthesize'):
        raise ValueError('Invalid claim action')
    _require(db, 'group_tasks', group_id, task_id)
    _require(db, 'group_claims', group_id, claim_id)
    old = db.execute('SELECT * FROM claim_tasks WHERE group_id=? AND task_id=?',
                     (group_id, task_id)).fetchone()
    if old:
        if (old['claim_id'], old['action']) != (claim_id, action):
            _conflict('Task already focused on a claim/action')
        return
    db.execute('INSERT INTO claim_tasks VALUES (?,?,?,?)', (group_id, task_id, claim_id, action))


def attach_evidence(db, group_id, claim_id, kind, evidence_id):
    if kind not in ('message', 'artifact'):
        raise ValueError('Invalid claim evidence kind')
    claim = _require(db, 'group_claims', group_id, claim_id)
    evidence = _require(db, 'group_' + kind + 's', group_id, evidence_id)
    if kind == 'artifact' and (evidence['statement'], evidence['imports'], evidence['environment']) != (
            claim['statement'], claim['imports'], claim['environment']):
        _conflict('Artifact does not prove the exact claim context')
    table = 'claim_' + kind + 's'
    old = db.execute(f'SELECT claim_id FROM {table} WHERE group_id=? AND {kind}_id=? AND claim_id=?',
                     (group_id, evidence_id, claim_id)).fetchone()
    if old:
        return
    if kind == 'artifact' and db.execute(
            'SELECT 1 FROM claim_artifacts WHERE group_id=? AND artifact_id=?',
            (group_id, evidence_id)).fetchone():
        _conflict('Artifact already linked')
    _capacity(db, table, group_id, MAX_EVIDENCE_LINKS)
    db.execute(f'INSERT INTO {table} VALUES (?,?,?)', (group_id, evidence_id, claim_id))


class ClaimGraph(ClaimGraphRead):
    def propose_group_claim(self, group_id, request_key, statement, imports, environment, **source):
        """Return (immutable claim ID, publication ID). Each new key retains its source."""
        with self.transaction() as db:
            return insert_claim(db, group_id, request_key, statement, imports, environment, **source)

    def attach_group_claim_task(self, group_id, task_id, claim_id, action='investigate'):
        with self.transaction() as db:
            attach_task(db, group_id, task_id, claim_id, action)

    def attach_group_claim_evidence(self, group_id, claim_id, kind, evidence_id):
        with self.transaction() as db:
            attach_evidence(db, group_id, claim_id, kind, evidence_id)

    def propose_group_relationship(self, group_id, request_key, from_id, to_id, kind,
                                   *, agent_id=None, task_id=None, job_id=None, reason=''):
        from .store import identifier
        _key(request_key)
        _reason(reason)
        if kind not in ('suggests_using', 'alternative_to', 'supersedes'):
            raise ValueError('Invalid planning relationship')
        with self.transaction() as db:
            first = _require(db, 'group_claims', group_id, from_id)
            second = _require(db, 'group_claims', group_id, to_id)
            if from_id == to_id or (first['imports'], first['environment']) != (
                    second['imports'], second['environment']):
                _conflict('Planning links need distinct compatible claims')
            source = _source(db, group_id, agent_id, task_id, job_id)
            values = (from_id, to_id, kind, *source, reason)
            old = db.execute('SELECT * FROM claim_relationships WHERE group_id=? AND request_key=?',
                             (group_id, request_key)).fetchone()
            if old:
                if tuple(old[k] for k in ('from_id', 'to_id', 'kind', 'source', 'agent_id',
                                         'task_id', 'job_id', 'assignment_id', 'reason')) != values:
                    _conflict('Relationship key reused')
                return old['id']
            _capacity(db, 'claim_relationships', group_id, MAX_RELATIONSHIPS)
            relationship_id = identifier()
            db.execute('INSERT INTO claim_relationships VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (relationship_id, group_id, request_key, *values))
            return relationship_id

    def review_group_relationship(self, group_id, request_key, relationship_id, reviewer_id,
                                  status, reason=''):
        from .store import identifier
        _key(request_key)
        _reason(reason)
        if status not in ('promising', 'challenged', 'abandoned'):
            raise ValueError('Invalid relationship review status')
        with self.transaction() as db:
            _require(db, 'claim_relationships', group_id, relationship_id)
            _require(db, 'group_agents', group_id, reviewer_id)
            old = db.execute('SELECT * FROM claim_relationship_reviews WHERE group_id=? AND request_key=?',
                             (group_id, request_key)).fetchone()
            if old:
                if tuple(old[k] for k in ('relationship_id', 'reviewer_id', 'status', 'reason')) != (
                        relationship_id, reviewer_id, status, reason):
                    _conflict('Relationship review key reused')
                return old['id']
            _capacity(db, 'claim_relationship_reviews', group_id, MAX_REVIEWS)
            review_id = identifier()
            db.execute('INSERT INTO claim_relationship_reviews VALUES (?,?,?,?,?,?,?)',
                       (review_id, group_id, request_key, relationship_id, reviewer_id, status, reason))
            return review_id
