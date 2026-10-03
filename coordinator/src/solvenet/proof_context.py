"""Coordinator-owned selection of checked proofs, frozen before execution."""

import json

from .composed import SCHEMA, MAX_DECLARATIONS, declaration_name, encode, validate_bundle
from .group_state import _require, _conflict

MIGRATION_21 = """
CREATE TABLE proof_contexts (
 owner_id TEXT PRIMARY KEY, owner_kind TEXT NOT NULL CHECK(owner_kind IN ('artifact','job')),
 group_id TEXT NOT NULL REFERENCES agent_groups(id), bundle TEXT NOT NULL);
CREATE TABLE composed_checks (
 id INTEGER PRIMARY KEY, owner_id TEXT NOT NULL, owner_kind TEXT NOT NULL,
 bundle TEXT NOT NULL, status TEXT NOT NULL, diagnostics TEXT NOT NULL,
 elapsed_ms INTEGER, usage TEXT NOT NULL, committed INTEGER NOT NULL);
CREATE INDEX composed_checks_owner ON composed_checks(owner_id,id);
CREATE TRIGGER immutable_proof_context BEFORE UPDATE ON proof_contexts
 BEGIN SELECT RAISE(ABORT,'Immutable proof context'); END;
CREATE TRIGGER immutable_check_inputs BEFORE UPDATE OF owner_id,owner_kind,bundle ON composed_checks
 BEGIN SELECT RAISE(ABORT,'Immutable verification inputs'); END;
PRAGMA user_version = 21;
"""

MIGRATION_24 = """
CREATE TABLE verification_ownership (
 owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, check_id INTEGER NOT NULL UNIQUE
 REFERENCES composed_checks(id), expires REAL NOT NULL,
 PRIMARY KEY(owner_kind,owner_id));
-- Pre-upgrade executions have no live ownership token; retain their spent slots.
UPDATE frontier_lean_reservations SET finished=1 WHERE check_id IN
 (SELECT id FROM composed_checks WHERE status='checking');
UPDATE composed_checks SET status='abandoned' WHERE status='checking';
PRAGMA user_version = 24;
"""


class VerificationBusy(Exception):
    """Another tick owns this input, or its persisted result is already terminal."""


def owns_check(db, owner_id, owner_kind, check_id):
    # Callers that never started a check retain the historical explicit result API.
    return check_id is None or db.execute('''SELECT 1 FROM verification_ownership
        WHERE owner_id=? AND owner_kind=? AND check_id=?''', (owner_id, owner_kind, check_id)).fetchone() is not None


def selected_bundle(db, group_id, claim_id, proof, proof_ids):
    """Resolve an ordered acyclic closure, retaining exact prior replay bodies."""
    if (not isinstance(proof_ids, list) or len(proof_ids) > MAX_DECLARATIONS or
            len(set(proof_ids)) != len(proof_ids)):
        raise ValueError('Invalid prerequisite proof IDs')
    claim = _require(db, 'group_claims', group_id, claim_id)
    binding = db.execute('''SELECT b.*,gl.mode,gl.limits FROM artifact_verifier_binding b
        LEFT JOIN group_loops gl ON gl.group_id=? WHERE b.id=1''', (group_id,)).fetchone()
    if not binding['identity']:
        _conflict('A current verifier binding is required for composed context')
    declarations, selected, active = [], {}, set()

    def visit(proof_id):
        if proof_id in active:
            _conflict('Cyclic proof context')
        if proof_id in selected:
            return
        if len(selected) + len(active) >= MAX_DECLARATIONS:
            _conflict('Composed declaration limit exceeded')
        active.add(proof_id)
        artifact = _require(db, 'group_artifacts', group_id, proof_id)
        if (artifact['status'] != 'verified' or artifact['verifier_identity'] != binding['identity'] or
                artifact['imports'] != claim['imports'] or artifact['environment'] != claim['environment']):
            _conflict('Prerequisite is not currently eligible in the exact claim context')
        evidence = db.execute('''SELECT claim_id FROM claim_artifacts
            WHERE group_id=? AND artifact_id=?''', (group_id, proof_id)).fetchone()
        if evidence is None:
            _conflict('Prerequisite has no immutable claim')
        prior = db.execute('SELECT bundle FROM proof_contexts WHERE owner_id=?', (proof_id,)).fetchone()
        dependencies = []
        if prior:
            prior = json.loads(prior['bundle'])
            if (prior['statement'], prior['proof'], prior['imports'], prior['environment']) != (
                    artifact['statement'], artifact['proof'], json.loads(artifact['imports']), artifact['environment']):
                _conflict('Previously checked proof was edited')
            dependencies = [item['proof_id'] for item in prior['declarations']]
            for item in prior['declarations']:
                visit(item['proof_id'])
                if selected[item['proof_id']] != item:
                    _conflict('Previously checked prerequisite was edited')
        item = dict(claim_id=evidence['claim_id'], proof_id=proof_id,
                    name=declaration_name(proof_id), statement=artifact['statement'],
                    proof=artifact['proof'], prerequisite_ids=dependencies)
        selected[proof_id] = item
        declarations.append(item)
        active.remove(proof_id)

    for proof_id in proof_ids:
        visit(proof_id)
    bundle = dict(schema=SCHEMA, group_id=group_id, claim_id=claim_id,
                  statement=claim['statement'], imports=json.loads(claim['imports']),
                  environment=claim['environment'], proof=proof, declarations=declarations,
                  selected_proof_ids=proof_ids,
                  verifier_identity=binding['identity'], verifier_revision=binding['revision'])
    if binding['mode'] == 'graph':
        limits = json.loads(binding['limits'])
        if len(declarations) > limits['included_lemmas']:
            raise ValueError('Configured composed declaration limit exceeded')
        bundle['source_byte_limit'] = limits['source_bytes']
    validate_bundle(bundle)
    return bundle


def freeze_context(db, owner_id, owner_kind, bundle):
    old = db.execute('SELECT bundle FROM proof_contexts WHERE owner_id=?', (owner_id,)).fetchone()
    if old and old['bundle'] != encode(bundle):
        _conflict('Proof context is immutable')
    db.execute('INSERT OR IGNORE INTO proof_contexts VALUES (?,?,?,?)',
               (owner_id, owner_kind, bundle['group_id'], encode(bundle)))


def binding_matches(db, bundle):
    row = db.execute('SELECT identity,revision FROM artifact_verifier_binding WHERE id=1').fetchone()
    return (row['identity'], row['revision']) == (bundle['verifier_identity'], bundle['verifier_revision'])


def check_inputs_match(db, owner_id, owner_kind, bundle):
    """Commit against the exact frozen source, independently of graph revisions."""
    if owner_kind == 'attempt':
        row = db.execute('''SELECT pc.bundle,t.candidate,p.statement,p.imports FROM attempts t
            JOIN assignments a ON a.id=t.assignment_id JOIN jobs j ON j.id=a.job_id
            JOIN runs r ON r.id=j.run_id JOIN problems p ON p.id=r.problem_id
            JOIN proof_contexts pc ON pc.owner_id=j.id AND pc.owner_kind='job'
            WHERE t.id=?''', (owner_id,)).fetchone()
        if not row or (bundle['statement'], bundle['imports']) != (row['statement'], json.loads(row['imports'])):
            return False
        expected = json.loads(row['bundle']) | {'proof': row['candidate']}
    else:
        row = db.execute('''SELECT pc.bundle,a.statement,a.imports,a.environment,a.proof
            FROM proof_contexts pc JOIN group_artifacts a ON a.id=pc.owner_id
            WHERE pc.owner_id=? AND pc.owner_kind='artifact' ''', (owner_id,)).fetchone()
        if not row or (bundle['statement'], bundle['imports'], bundle['environment'], bundle['proof']) != (
                row['statement'], json.loads(row['imports']), row['environment'], row['proof']):
            return False
        expected = json.loads(row['bundle'])
    # A recheck may authorize a new verifier revision, never new mathematical inputs.
    for key in ('verifier_identity', 'verifier_revision'):
        expected.pop(key, None)
    actual = {k: v for k, v in bundle.items() if k not in ('verifier_identity', 'verifier_revision')}
    return encode(expected) == encode(actual)


def record_check(db, owner_id, owner_kind, bundle, result, usage, committed, check_id=None):
    if usage is None:
        usage = {'status': 'usage_unknown'}
    from .frontier import finish_check
    finish_check(db, check_id, result, usage)
    if check_id is not None:
        db.execute('''UPDATE composed_checks SET status=?,diagnostics=?,elapsed_ms=?,usage=?,committed=?
            WHERE id=? AND owner_id=? AND owner_kind=? AND bundle=? AND status='checking' ''',
            (str(result.status), result.diagnostics, result.elapsed_ms, json.dumps(usage), int(committed),
             check_id, owner_id, owner_kind, encode(bundle)))
        db.execute('DELETE FROM verification_ownership WHERE owner_id=? AND owner_kind=? AND check_id=?',
                   (owner_id, owner_kind, check_id))
        return
    db.execute('''INSERT INTO composed_checks
        (owner_id,owner_kind,bundle,status,diagnostics,elapsed_ms,usage,committed)
        VALUES (?,?,?,?,?,?,?,?)''', (owner_id, owner_kind, encode(bundle), str(result.status),
        result.diagnostics, result.elapsed_ms, json.dumps(usage), int(committed)))


class ProofContexts:
    def begin_composed_check(self, owner_id, owner_kind, bundle, *, deadline_ms=30000):
        from .frontier import reserve_check, FrontierLimit
        refusal = None
        with self.transaction() as db:
            if owner_kind == 'attempt':
                terminal = db.execute('''SELECT 1 FROM attempts t JOIN assignments a ON a.id=t.assignment_id
                    JOIN jobs j ON j.id=a.job_id LEFT JOIN verifications v ON v.attempt_id=t.id
                    WHERE t.id=? AND (v.attempt_id IS NOT NULL OR j.status='cancelled')''', (owner_id,)).fetchone()
            else:
                terminal = db.execute("SELECT 1 FROM group_artifacts WHERE id=? AND status!='pending'", (owner_id,)).fetchone()
            if terminal:
                raise VerificationBusy('Verification already completed')
            ownership = db.execute('SELECT * FROM verification_ownership WHERE owner_id=? AND owner_kind=?',
                                   (owner_id, owner_kind)).fetchone()
            if ownership:
                if ownership['expires'] > self.clock():
                    raise VerificationBusy('Verification in flight')
                # Recovery is bounded by the verifier deadline plus a short commit grace.
                # An abandoned operation keeps its unknown time charge and spent slot.
                db.execute("UPDATE composed_checks SET status='abandoned' WHERE id=? AND status='checking'", (ownership['check_id'],))
                db.execute('UPDATE frontier_lean_reservations SET finished=1 WHERE check_id=?', (ownership['check_id'],))
                db.execute('DELETE FROM verification_ownership WHERE check_id=?', (ownership['check_id'],))
            row = db.execute('''INSERT INTO composed_checks
                (owner_id,owner_kind,bundle,status,diagnostics,elapsed_ms,usage,committed)
                VALUES (?,?,?,'checking','',NULL,?,0)''',
                (owner_id, owner_kind, encode(bundle), json.dumps({'status': 'usage_unknown'})))
            check_id = row.lastrowid
            try:
                reserve_check(db, self, check_id, owner_kind, bundle, deadline_ms)
            except FrontierLimit as error:
                refusal = error
                db.execute('DELETE FROM composed_checks WHERE id=?', (check_id,))
            else:
                db.execute('INSERT INTO verification_ownership VALUES (?,?,?,?)',
                           (owner_kind, owner_id, check_id, self.clock() + deadline_ms / 1000 + 5))
        # Commit abandoned-check accounting even if recovery cannot afford a new slot.
        if refusal:
            raise refusal
        return check_id

    def proof_context(self, owner_id):
        with self.connect() as db:
            row = db.execute('SELECT bundle FROM proof_contexts WHERE owner_id=?', (owner_id,)).fetchone()
            return json.loads(row['bundle']) if row else None

    def export_proof_bundle(self, owner_id):
        """Prefer the last committed verification, including the target candidate."""
        with self.connect() as db:
            row = db.execute('''SELECT bundle FROM composed_checks WHERE owner_id=? AND committed=1
                ORDER BY id DESC LIMIT 1''', (owner_id,)).fetchone()
            if row:
                return json.loads(row['bundle'])
        return self.proof_context(owner_id)

    def composed_evidence(self, owner_id):
        with self.connect() as db:
            return [dict(row) | {'bundle': json.loads(row['bundle']), 'usage': json.loads(row['usage'])}
                    for row in db.execute('SELECT * FROM composed_checks WHERE owner_id=? ORDER BY id',
                                          (owner_id,))]

    def checked_proof_bundle(self, owner_id):
        """Public export: actual proof owners with a committed check, never job context.

        A fresh pending proof has no public bundle. Rechecks may be pending while
        the last committed (possibly rejected) receipt remains inspectable.
        """
        with self.connect() as db:
            row = db.execute('''SELECT c.bundle FROM composed_checks c
                WHERE c.owner_id=? AND c.committed=1 AND (
                    (c.owner_kind='artifact' AND EXISTS (
                        SELECT 1 FROM group_artifacts a WHERE a.id=c.owner_id)) OR
                    (c.owner_kind='attempt' AND EXISTS (
                        SELECT 1 FROM attempts t JOIN assignments a ON a.id=t.assignment_id
                        JOIN jobs j ON j.id=a.job_id JOIN group_jobs gj ON gj.job_id=j.id
                        WHERE t.id=c.owner_id AND j.kind='model.generate')))
                ORDER BY c.id DESC LIMIT 1''', (owner_id,)).fetchone()
            return json.loads(row['bundle']) if row else None

    def select_target_proof_context(self, job_id, proof_ids):
        """Private explicit selection; queued target-only jobs, before dispatch."""
        with self.transaction() as db:
            prior = db.execute('SELECT bundle FROM proof_contexts WHERE owner_id=?', (job_id,)).fetchone()
            if prior:
                if json.loads(prior['bundle'])['selected_proof_ids'] != proof_ids:
                    _conflict('Proof context is immutable')
                return
            row = db.execute('''SELECT gj.group_id,gg.root_id,j.kind,j.status FROM group_jobs gj
                JOIN jobs j ON j.id=gj.job_id JOIN group_graphs gg ON gg.group_id=gj.group_id
                WHERE gj.job_id=?''', (job_id,)).fetchone()
            if not row or row['kind'] != 'model.generate' or row['status'] != 'queued':
                _conflict('Context selection requires a queued group target job')
            bundle = selected_bundle(db, row['group_id'], row['root_id'], '', proof_ids)
            freeze_context(db, job_id, 'job', bundle)

    def ensure_artifact_context(self, artifact_id, binding):
        with self.transaction() as db:
            artifact = db.execute('SELECT * FROM group_artifacts WHERE id=?', (artifact_id,)).fetchone()
            prior = db.execute('SELECT bundle FROM proof_contexts WHERE owner_id=?', (artifact_id,)).fetchone()
            if prior:
                bundle = json.loads(prior['bundle'])
                if (bundle['statement'], bundle['proof'], bundle['imports'], bundle['environment']) != (
                        artifact['statement'], artifact['proof'], json.loads(artifact['imports']), artifact['environment']):
                    _conflict('Previously frozen proof was edited')
            else:
                claim = db.execute('''SELECT claim_id FROM claim_artifacts
                    WHERE group_id=? AND artifact_id=?''',
                    (artifact['group_id'], artifact_id)).fetchone()
                if not claim:
                    return None  # historical groups retain the standalone path
                bundle = selected_bundle(db, artifact['group_id'], claim[0], artifact['proof'], [])
                freeze_context(db, artifact_id, 'artifact', bundle)
            # A new verification attempt can recheck the same immutable source
            # under a changed verifier. Its own replay bundle records that binding.
            return bundle | {'verifier_identity': binding[0], 'verifier_revision': binding[1]}
