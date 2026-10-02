"""Coordinator-owned, local formal artifacts (distinct from informal findings)."""

import json

from . import protocol_limits as limits
from .group_state import _conflict, _key, _require, _require_group, _text
from .verifier import truncate_diagnostics


MIGRATION_16 = """
CREATE TABLE artifact_verifier_binding (
 id INTEGER PRIMARY KEY CHECK (id=1), identity TEXT, revision INTEGER NOT NULL);
INSERT INTO artifact_verifier_binding VALUES (1,NULL,0);
CREATE TABLE group_artifacts (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES agent_groups(id),
 request_key TEXT NOT NULL, agent_id TEXT NOT NULL, task_id TEXT NOT NULL, job_id TEXT,
 statement TEXT NOT NULL, imports TEXT NOT NULL, environment TEXT NOT NULL, proof TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending'
  CHECK (status IN ('pending','verified','rejected','incompatible','verifier_error','timeout')),
  diagnostics TEXT NOT NULL DEFAULT '', verifier_identity TEXT,
  source TEXT NOT NULL CHECK (source IN ('job','coordinator')),
 UNIQUE(group_id, request_key), UNIQUE(group_id, id),
 FOREIGN KEY(group_id, agent_id) REFERENCES group_agents(group_id, id),
 FOREIGN KEY(group_id, task_id) REFERENCES group_tasks(group_id, id),
 FOREIGN KEY(job_id, group_id, task_id, agent_id)
 REFERENCES group_jobs(job_id, group_id, task_id, agent_id));
PRAGMA user_version = 16;
"""

MAX_ARTIFACTS = 64
MAX_ARTIFACT_DIAGNOSTICS_BYTES = 2048
MAX_ARTIFACT_TEXT_BYTES = 8192


def validate_artifact(statement, imports, environment, proof):
    _text(statement, 'artifact statement', MAX_ARTIFACT_TEXT_BYTES)
    if (not isinstance(imports, list) or not 1 <= len(imports) <= limits.MAX_IMPORTS or
            any(not isinstance(item, str) or not item.strip() or
                    len(item.encode()) > limits.MAX_IMPORT_BYTES for item in imports)):
        raise ValueError('Invalid artifact imports')
    _text(environment, 'artifact environment', 1024)
    _text(proof, 'artifact proof', MAX_ARTIFACT_TEXT_BYTES)


def artifact_from_finding(text):
    """Only an explicitly structured proposal is formal; all other text stays informal."""
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get('artifact'), dict):
        return None
    artifact = value['artifact']
    if not {'statement', 'imports', 'environment', 'proof'} <= artifact.keys():
        return None
    try:
        validate_artifact(*(artifact[k] for k in ('statement', 'imports', 'environment', 'proof')))
    except ValueError:
        return None
    return artifact


def insert_artifact(db, group_id, request_key, agent_id, task_id,
                    statement, imports, environment, proof, job_id=None):
    """Insert with checked provenance inside the caller's existing transaction."""
    from .store import identifier
    _key(request_key)
    validate_artifact(statement, imports, environment, proof)
    _require_group(db, group_id)
    _require(db, 'group_agents', group_id, agent_id)
    task = _require(db, 'group_tasks', group_id, task_id)
    if task['owner_id'] != agent_id:
        _conflict('Artifact agent is not task owner')
    if job_id:
        source = db.execute('''SELECT j.status, j.kind, j.task_type, a.result FROM group_jobs gj
            JOIN jobs j ON j.id=gj.job_id
            LEFT JOIN assignments a ON a.job_id=j.id AND a.status='completed'
            WHERE gj.job_id=? AND gj.group_id=? AND gj.task_id=? AND gj.agent_id=?
            ORDER BY a.rowid DESC LIMIT 1''',
            (job_id, group_id, task_id, agent_id)).fetchone()
        if (not source or source['status'] != 'done' or not source['result'] or
                source['kind'] != 'model.respond' or source['task_type'] != 'finding'):
            _conflict('Artifact source is not a completed finding for this agent and task')
        output = json.loads(source['result']).get('output', {})
        proposal = artifact_from_finding(output.get('text'))
        if not proposal or any(proposal[k] != value for k, value in (
                ('statement', statement), ('imports', imports),
                ('environment', environment), ('proof', proof))):
            _conflict('Artifact differs from completed job output')
    serialized = json.dumps(imports)
    old = db.execute('SELECT * FROM group_artifacts WHERE group_id=? AND request_key=?',
                     (group_id, request_key)).fetchone()
    if old:
        if (old['agent_id'], old['task_id'], old['job_id'], old['statement'],
                old['imports'], old['environment'], old['proof']) != (
                agent_id, task_id, job_id, statement, serialized, environment, proof):
            _conflict('Artifact key reused with different contents')
        return old['id']
    if db.execute('SELECT count(*) FROM group_artifacts WHERE group_id=?',
                  (group_id,)).fetchone()[0] >= MAX_ARTIFACTS:
        _conflict('Artifact limit reached')
    artifact_id = identifier()
    db.execute('''INSERT INTO group_artifacts
        (id,group_id,request_key,agent_id,task_id,job_id,statement,imports,environment,proof,source)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (artifact_id, group_id, request_key, agent_id, task_id, job_id,
         statement, serialized, environment, proof, 'job' if job_id else 'coordinator'))
    # Historical groups remain untouched; new proposals in graph-enabled groups
    # publish their exact context rather than inheriting a possibly different focus.
    graph = db.execute('SELECT root_id FROM group_graphs WHERE group_id=?', (group_id,)).fetchone()
    if graph['root_id'] is not None:
        from .claim_graph import insert_claim, attach_evidence
        claim_id, _ = insert_claim(db, group_id, 'artifact:' + artifact_id,
                                  statement, imports, environment, agent_id=agent_id,
                                  task_id=task_id, job_id=job_id, reason='Formal artifact proposal')
        attach_evidence(db, group_id, claim_id, 'artifact', artifact_id)
    return artifact_id


class GroupArtifacts:
    def record_group_lean_check(self, artifact_id, status, elapsed_ms):
        with self.transaction() as db:
            db.execute('''INSERT INTO group_lean_checks(group_id,artifact_id,status,elapsed_ms)
                SELECT group_id,id,?,? FROM group_artifacts WHERE id=?''',
                (str(status), elapsed_ms, artifact_id))

    def propose_group_artifact(self, group_id, request_key, agent_id, task_id,
                               statement, imports, environment, proof, *, job_id=None):
        with self.transaction() as db:
            return insert_artifact(db, group_id, request_key, agent_id, task_id,
                                   statement, imports, environment, proof, job_id)

    def bind_group_artifact_verifier(self, identity):
        """Return (identity, revision); invalidate stale claims atomically."""
        with self.transaction() as db:
            row = db.execute('SELECT identity,revision FROM artifact_verifier_binding WHERE id=1').fetchone()
            revision = row['revision']
            if row['identity'] != identity:
                revision += 1
                db.execute('UPDATE artifact_verifier_binding SET identity=?,revision=? WHERE id=1',
                           (identity, revision))
                db.execute('''UPDATE group_artifacts SET status='pending',diagnostics='',verifier_identity=NULL
                    WHERE status='verified' AND (verifier_identity IS NULL OR verifier_identity != ?
                        OR ? IS NULL)''', (identity, identity))
            token = identity, revision
        self.artifact_verifier_binding = token
        return token

    def needs_artifact_identity(self):
        """Only pending checks or synthesis about to consume verified context need a scan."""
        with self.connect() as db:
            return db.execute('''SELECT 1 FROM group_artifacts a
                LEFT JOIN group_loops gl ON gl.group_id=a.group_id
                WHERE (a.status='pending' AND (gl.group_id IS NULL OR gl.phase!='stopped'))
                   OR (a.status='verified' AND gl.phase='synthesize') LIMIT 1''').fetchone() is not None

    def pending_group_artifact(self):
        with self.connect() as db:
            row = db.execute('''SELECT a.*, g.imports AS target_imports,
                g.environment AS target_environment FROM group_artifacts a
                JOIN agent_groups g ON g.id=a.group_id
                LEFT JOIN group_loops gl ON gl.group_id=a.group_id
                WHERE a.status='pending' AND (gl.group_id IS NULL OR gl.phase!='stopped')
                ORDER BY a.rowid LIMIT 1''').fetchone()
            return dict(row) if row else None

    def checked_group_artifact(self, artifact_id, status, diagnostics='', *, binding=None):
        if status not in ('verified', 'rejected', 'incompatible', 'verifier_error', 'timeout'):
            raise ValueError('Invalid artifact verification status')
        if binding is None or (status == 'verified' and not binding[0]):
            raise ValueError('Verifier binding required for artifact result')
        bounded = truncate_diagnostics(diagnostics, MAX_ARTIFACT_DIAGNOSTICS_BYTES)
        with self.transaction() as db:
            db.execute('''UPDATE group_artifacts SET status=?,diagnostics=?,verifier_identity=?
                WHERE id=? AND status='pending' AND EXISTS (
                    SELECT 1 FROM artifact_verifier_binding WHERE id=1 AND identity IS ? AND revision=?)''',
                (status, bounded, binding[0], artifact_id, binding[0], binding[1]))
