import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from solvenet.store import Conflict, Store


class ClaimGraphTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'state.db'
        self.store = Store(self.path)
        self.group = self.store.create_group('group', ': True ∧ True', ['Init'], 'lean-test')
        self.root = self.store.group(self.group)['graph']['root_id']
        self.one = self.store.add_agent(self.group, 'one', 'investigator')
        self.two = self.store.add_agent(self.group, 'two', 'investigator')

    def propose(self, key, statement=': True', **options):
        return self.store.propose_group_claim(self.group, key, statement, ['Init'], 'lean-test', **options)

    def task(self, key, agent, claim=None):
        return self.store.add_group_task(self.group, key, agent, agent, 'Investigate', 2,
                                          claim_id=claim)

    def revision(self):
        return self.store.group(self.group)['graph']['revision']

    def test_restart_exact_dedup_every_publication_and_two_investigators(self):
        a, publication = self.propose('first')
        b, _ = self.propose('second', ': 1 = 1')
        t1, t2 = self.task('task-one', self.one, a), self.task('task-two', self.two, a)
        sources = []
        for i, agent, task in ((1, self.one, t1), (2, self.two, t2)):
            job = self.store.enqueue_group_job(self.group, task, agent, f'job-{i}',
                'lean-test', 'scripted', 'finding', [{'role': 'user', 'content': 'Investigate'}])
            lease = self.store.claim(f'worker-{i}', ['scripted'], supports_model_respond=True)
            self.store.result(lease['assignment_id'], {'lease_token': lease['lease_token'],
                'status': 'completed', 'output': {'type': 'finding', 'text': f'finding {i}'}})
            claim, pub = self.propose(f'publication-{i}', agent_id=agent, task_id=task,
                                      job_id=job, reason=f'finding {i}')
            self.assertEqual(claim, a)
            sources.append((pub, agent, task, job, lease['assignment_id']))
        self.store.propose_group_relationship(self.group, 'root-a', self.root, a, 'suggests_using')
        self.store.propose_group_relationship(self.group, 'root-b', self.root, b, 'suggests_using')
        with self.assertRaises(Conflict):
            self.propose('wrong-completed-source', agent_id=self.one, task_id=t1, job_id=sources[1][3])
        revision = self.revision()
        self.store = Store(self.path)
        self.assertEqual(self.propose('first'), (a, publication))
        for i, (pub, agent, task, job, assignment) in enumerate(sources, 1):
            self.assertEqual(self.propose(f'publication-{i}', agent_id=agent, task_id=task,
                job_id=job, reason=f'finding {i}'), (a, pub))
        self.assertEqual(self.revision(), revision)
        graph = self.store.group_claim_neighborhood(self.group)
        self.assertEqual(len(graph['claims']), 3)
        self.assertEqual({t['task_id'] for t in graph['tasks']}, {t1, t2})
        actual = [(p['id'], p['agent_id'], p['task_id'], p['job_id'], p['assignment_id'])
                  for p in graph['publications'] if p['source'] == 'job']
        self.assertEqual(actual, sources)
        self.assertEqual(graph['artifacts'], [])  # Publishing grants no Lean status.
        self.assertEqual(graph, self.store.group_claim_neighborhood(self.group))
        different, _ = self.store.propose_group_claim(self.group, 'context', ': True', ['Init', 'Lean'], 'lean-test')
        changed, _ = self.store.propose_group_claim(self.group, 'environment', ': True', ['Init'], 'other')
        spaced, _ = self.propose('whitespace', ':  True')
        self.assertEqual(len({a, different, changed, spaced}), 4)

    def test_atomic_invalid_cross_group_sources_and_links(self):
        a, _ = self.propose('a')
        other = self.store.create_group('other', ': False', ['Init'], 'lean-test')
        foreign = self.store.group(other)['graph']['root_id']
        task = self.task('one', self.one, a)
        job = self.store.enqueue_group_job(self.group, task, self.one, 'unfinished',
            'lean-test', 'scripted', 'finding', [{'role': 'user', 'content': 'Find'}])
        before = self.store.group_claim_neighborhood(self.group, a)
        operations = [
            lambda: self.propose('bad-source', agent_id='unknown', task_id=task),
            lambda: self.propose('bad-job', agent_id=self.one, task_id=task, job_id=job),
            lambda: self.propose('wrong-owner', agent_id=self.two, task_id=task),
            lambda: self.propose('unknown-job', agent_id=self.one, task_id=task, job_id='unknown'),
            lambda: self.store.propose_group_relationship(self.group, 'foreign', a, foreign, 'suggests_using'),
            lambda: self.store.propose_group_relationship(self.group, 'self', a, a, 'suggests_using'),
            lambda: self.store.attach_group_claim_task(self.group, task, foreign),
            lambda: self.store.attach_group_claim_evidence(self.group, a, 'message', 'unknown'),
            lambda: self.store.add_group_task(self.group, 'bad-task', self.one, self.one,
                                               'Cross group', 2, claim_id=foreign),
        ]
        for operation in operations:
            with self.assertRaises(Conflict):
                operation()
            self.assertEqual(self.store.group_claim_neighborhood(self.group, a), before)
        self.assertEqual(len(self.store.group(self.group)['tasks']), 1)
        incompatible, _ = self.store.propose_group_claim(self.group, 'incompatible', ': True', ['Lean'], 'lean-test')
        with self.assertRaises(Conflict):
            self.store.propose_group_relationship(self.group, 'bad-env', a, incompatible, 'supersedes')

    def test_reciprocal_reviews_supersession_and_independent_proof_history(self):
        a, _ = self.propose('a')
        corrected, _ = self.propose('corrected', ': True ∧ (True ∧ True)')
        task = self.task('proof', self.one, a)
        artifact = self.store.propose_group_artifact(self.group, 'proof', self.one, task,
                                                     ': True', ['Init'], 'lean-test', 'trivial')
        binding = self.store.bind_group_artifact_verifier('pinned-a')
        self.store.checked_group_artifact(artifact, 'verified', binding=binding)
        forward = self.store.propose_group_relationship(self.group, 'forward', self.root, a, 'suggests_using')
        revision = self.revision()
        self.assertEqual(self.store.propose_group_relationship(self.group, 'forward', self.root, a,
                                                               'suggests_using'), forward)
        self.assertEqual(self.revision(), revision)
        with self.assertRaises(Conflict):
            self.store.propose_group_relationship(self.group, 'forward', self.root, a,
                                                   'suggests_using', reason='Changed reason')
        self.store.propose_group_relationship(self.group, 'reverse', a, self.root, 'suggests_using')
        self.store.propose_group_relationship(self.group, 'corrected', corrected, a, 'supersedes')
        self.store.propose_group_relationship(self.group, 'alternative', corrected, a, 'alternative_to')
        revision = self.revision()
        review = self.store.review_group_relationship(self.group, 'review', forward, self.two,
                                                       'challenged', 'Try the direct proof')
        self.assertGreater(self.revision(), revision)
        revision = self.revision()
        self.assertEqual(self.store.review_group_relationship(self.group, 'review', forward,
            self.two, 'challenged', 'Try the direct proof'), review)
        self.assertEqual(self.revision(), revision)
        graph = self.store.group_claim_neighborhood(self.group, a, verifier_identity='pinned-a')
        self.assertEqual(graph['artifacts'][0]['status'], 'verified')
        self.assertEqual(graph['artifacts'][0]['current_status'], 'verified')
        self.assertEqual(graph['reviews'][0]['status'], 'challenged')
        with self.assertRaises(Conflict):
            self.store.attach_group_claim_evidence(self.group, corrected, 'artifact', artifact)
        self.store.bind_group_artifact_verifier('pinned-b')
        self.assertGreater(self.revision(), revision)
        self.store = Store(self.path)
        graph = self.store.group_claim_neighborhood(self.group, a, verifier_identity='pinned-a')
        self.assertEqual(graph['artifacts'][0]['status'], 'pending')
        self.assertEqual(graph['artifacts'][0]['proof'], 'trivial')
        self.assertEqual(graph['outcomes'][0]['status'], 'verified')
        self.assertEqual(graph['outcomes'][0]['verifier_identity'], 'pinned-a')
        self.assertEqual(next(c['statement'] for c in graph['claims'] if c['id'] == a), ': True')
        with self.store.transaction() as db, self.assertRaises(sqlite3.IntegrityError):
            db.execute('UPDATE group_claims SET statement=? WHERE id=?', (': False', a))

    def test_evidence_reviews_task_status_and_idempotent_attachment_revision(self):
        a, _ = self.propose('a')
        task = self.task('task', self.one, a)
        message = self.store.add_group_message(self.group, 'finding', self.one, task, 'finding', 'Idea')
        revision = self.revision()
        self.store.attach_group_claim_evidence(self.group, a, 'message', message)
        self.store.attach_group_claim_task(self.group, task, a)
        self.assertEqual(self.revision(), revision)
        self.store.review_group_message(self.group, message, self.two, 'review', 'accepted', 'Useful')
        self.assertGreater(self.revision(), revision)
        graph = self.store.group_claim_neighborhood(self.group, a)
        self.assertEqual(graph['messages'][0]['review_status'], 'accepted')
        self.assertEqual(graph['messages'][0]['verification_status'], 'unverified')
        revision = self.revision()
        self.store.set_group_task_status(self.group, task, 'done')
        self.assertGreater(self.revision(), revision)

    def test_graph_caps_fail_without_partial_node_publication_or_task(self):
        a, pub = self.propose('a')
        before = self.store.group_claim_neighborhood(self.group, a)
        for constant, operation in (
            ('MAX_CLAIMS', lambda: self.propose('overflow', ': False')),
            ('MAX_PUBLICATIONS', lambda: self.propose('dedup-overflow')),
            ('MAX_RELATIONSHIPS', lambda: self.store.propose_group_relationship(
                self.group, 'edge', self.root, a, 'suggests_using')),
        ):
            with patch('solvenet.claim_graph.' + constant, 1), self.assertRaises(Conflict):
                if constant == 'MAX_RELATIONSHIPS':
                    self.store.propose_group_relationship(self.group, 'seed', a, self.root, 'suggests_using')
                    before = self.store.group_claim_neighborhood(self.group, a)
                operation()
            self.assertEqual(self.store.group_claim_neighborhood(self.group, a), before)
        with patch('solvenet.claim_graph.MAX_PUBLICATIONS', 1):
            self.assertEqual(self.propose('a'), (a, pub))
            task = self.task('task', self.one, a)
            with self.assertRaises(Conflict):
                self.store.propose_group_artifact(self.group, 'overflow-proof', self.one, task,
                                                   ': False', ['Init'], 'lean-test', 'trivial')
            self.assertEqual(self.store.group(self.group)['artifacts'], [])
        with self.assertRaises(ValueError):
            self.propose('oversized', reason='ü' * 2049)

    def test_bounded_indexed_reads_no_n_plus_one_and_encoded_size(self):
        for i in range(12):
            claim, _ = self.propose(f'claim-{i}', f': {i} = {i}')
            self.store.propose_group_relationship(self.group, f'edge-{i}', self.root, claim, 'suggests_using')
        original = self.store.connect
        statements = []

        @contextmanager
        def traced():
            with original() as db:
                db.set_trace_callback(statements.append)
                yield db

        with patch.object(self.store, 'connect', traced):
            small = self.store.group_claim_neighborhood(self.group, max_nodes=3, max_items=1)
            small_count = len(statements)
            statements.clear()
            large = self.store.group_claim_neighborhood(self.group, max_nodes=16, max_items=32)
        self.assertEqual(len(statements), small_count)
        self.assertLess(small_count, 25)
        self.assertEqual(len(small['claims']), 3)
        self.assertTrue(small['omitted']['traversal_truncated'])
        self.assertGreater(small['omitted']['publications'], 0)
        self.assertEqual(len(large['claims']), 13)
        bounded = self.store.group_claim_neighborhood(self.group, max_bytes=1000)
        self.assertLessEqual(len(json.dumps(bounded).encode()), 1000)
        with self.store.connect() as db:
            for column, index in (('from_id', 'relationships_from'), ('to_id', 'relationships_to')):
                plan = db.execute(f'EXPLAIN QUERY PLAN SELECT * FROM claim_relationships WHERE group_id=? AND {column}=?',
                                  (self.group, self.root)).fetchall()
                self.assertIn(index, str([tuple(r) for r in plan]))
        for options in ({'depth': 4}, {'max_nodes': 33}, {'max_items': 65}, {'max_bytes': 262145}):
            with self.assertRaises(ValueError):
                self.store.group_claim_neighborhood(self.group, **options)

    def test_work_reservation_job_transitions_restart_and_exact_retries(self):
        now = [100.0]
        self.store.clock = lambda: now[0]
        claim, _ = self.propose('focused')
        task = self.task('focused-task', self.one, claim)
        revision = self.revision()

        def enqueue():
            return self.store.enqueue_group_job(self.group, task, self.one, 'reserved-job',
                'lean-test', 'scripted', 'finding', [{'role': 'user', 'content': 'Find'}], cost=2)

        job = enqueue()
        self.assertGreater(self.revision(), revision)
        graph = self.store.group_claim_neighborhood(self.group, claim)
        self.assertEqual(graph['tasks'][0]['remaining'], 0)
        self.assertEqual(graph['tasks'][0]['budget'], 2)
        revision = self.revision()
        self.store = Store(self.path, clock=lambda: now[0])
        self.assertEqual(enqueue(), job)
        self.assertEqual(self.revision(), revision)
        lease = self.store.claim('worker', ['scripted'], supports_model_respond=True)
        self.assertGreater(self.revision(), revision)
        revision = self.revision()
        payload = {'lease_token': lease['lease_token'], 'status': 'failed',
                   'error': 'Temporary outage', 'failure_class': 'transient'}
        self.store.result(lease['assignment_id'], payload)
        self.assertGreater(self.revision(), revision)
        revision = self.revision()
        self.store.result(lease['assignment_id'], payload)
        self.assertEqual(self.revision(), revision)
        lease = self.store.claim('worker', ['scripted'], supports_model_respond=True)
        self.assertGreater(self.revision(), revision)
        revision = self.revision()
        payload = {'lease_token': lease['lease_token'], 'status': 'completed',
                   'output': {'type': 'finding', 'text': 'New evidence'}}
        self.store.result(lease['assignment_id'], payload)
        self.assertGreater(self.revision(), revision)
        revision = self.revision()
        self.store.result(lease['assignment_id'], payload)
        self.assertEqual(self.revision(), revision)
        self.assertEqual(Store(self.path).group(self.group)['graph']['revision'], revision)
        # v1 job transitions are unrelated to the focused group's availability.
        self.store.submit(': True', ['Init'], attempts=1)
        self.store.claim('independent', ['scripted'])
        self.assertEqual(self.revision(), revision)

    def test_reservation_rollback_and_group_work_availability(self):
        task = self.task('focused', self.one, self.root)
        revision = self.revision()
        original = self.store.transaction

        @contextmanager
        def failing():
            with original() as db:
                db.execute('''CREATE TEMP TRIGGER fail_reservation AFTER UPDATE OF remaining ON group_tasks
                    BEGIN SELECT RAISE(ABORT,'reservation failure'); END''')
                yield db

        with patch.object(self.store, 'transaction', failing), self.assertRaisesRegex(sqlite3.IntegrityError, 'reservation failure'):
            self.store.enqueue_group_job(self.group, task, self.one, 'fail', 'lean-test',
                'scripted', 'finding', [{'role': 'user', 'content': 'Find'}])
        self.assertEqual(self.revision(), revision)
        self.assertIsNone(self.store.group(self.group)['run'])
        self.assertEqual(self.store.group_claim_neighborhood(self.group)['tasks'][0]['remaining'], 2)
        # Reserving additional task work changes the group's availability even
        # if the newly created task has not itself acquired a focus yet.
        with self.store.transaction() as db:
            db.execute('UPDATE agent_groups SET remaining_work=remaining_work-1 WHERE id=?', (self.group,))
        self.assertGreater(self.revision(), revision)
        revision = self.revision()
        with self.store.transaction() as db:
            db.execute('UPDATE agent_groups SET remaining_work=remaining_work WHERE id=?', (self.group,))
            db.execute('UPDATE group_tasks SET remaining=remaining WHERE id=?', (task,))
        self.assertEqual(self.revision(), revision)

    def test_review_omissions_include_capped_reciprocal_relationship(self):
        claim, _ = self.propose('neighbor')
        forward = self.store.propose_group_relationship(self.group, 'forward', self.root, claim, 'suggests_using')
        reverse = self.store.propose_group_relationship(self.group, 'reverse', claim, self.root, 'suggests_using')
        review = self.store.review_group_relationship(self.group, 'review', reverse, self.two, 'challenged')
        graph = self.store.group_claim_neighborhood(self.group, max_items=1)
        self.assertEqual([r['id'] for r in graph['relationships']], [forward])
        self.assertEqual(graph['reviews'], [])
        self.assertEqual(graph['omitted']['relationships'], 1)
        self.assertEqual(graph['omitted']['reviews'], 1)
        complete = self.store.group_claim_neighborhood(self.group, max_items=2)
        self.assertEqual([r['id'] for r in complete['reviews']], [review])
        self.assertEqual(complete['omitted']['reviews'], 0)

    def test_distinct_proofs_retain_provenance_and_evidence_review_caps(self):
        a, _ = self.propose('a')
        for index, agent in enumerate((self.one, self.two)):
            task = self.task(f'task-{index}', agent, a)
            self.store.propose_group_artifact(self.group, f'proof-{index}', agent, task,
                                               ': True', ['Init'], 'lean-test', 'trivial')
        graph = self.store.group_claim_neighborhood(self.group, a)
        self.assertEqual(len(graph['artifacts']), 2)
        self.assertEqual({p['agent_id'] for p in graph['publications'] if p['agent_id']}, {self.one, self.two})
        self.assertEqual({p['claim_id'] for p in graph['publications']}, {a})
        relationship = self.store.propose_group_relationship(self.group, 'edge', self.root, a, 'suggests_using')
        review = self.store.review_group_relationship(self.group, 'review', relationship, self.one, 'promising')
        revision = self.revision()
        with patch('solvenet.claim_graph.MAX_REVIEWS', 1):
            self.assertEqual(self.store.review_group_relationship(self.group, 'review', relationship,
                              self.one, 'promising'), review)
            with self.assertRaises(Conflict):
                self.store.review_group_relationship(self.group, 'extra-review', relationship, self.two, 'challenged')
        self.assertEqual(self.revision(), revision)
        # Force attachment failure after a message has been inserted. Neither the
        # message nor its graph revision may escape the enclosing transaction.
        task = self.store.group(self.group)['tasks'][0]['id']
        with patch('solvenet.claim_graph.MAX_EVIDENCE_LINKS', 1):
            self.store.add_group_message(self.group, 'first', self.one, task, 'finding', 'First')
            revision = self.revision()
            with self.assertRaises(Conflict):
                self.store.add_group_message(self.group, 'second', self.one, task, 'finding', 'Second')
            self.assertEqual(len(self.store.group(self.group)['messages']), 1)
            self.assertEqual(self.revision(), revision)

    def test_actual_node_ceiling_and_outcome_ceiling_are_transactional(self):
        for index in range(63):
            self.propose(f'node-{index}', f': {index} = {index}')
        revision = self.revision()
        with self.assertRaises(Conflict):
            self.propose('overflow', ': False')
        self.assertEqual(self.revision(), revision)
        # Exact dedup remains possible when the node ceiling has been reached.
        self.assertEqual(self.propose('duplicate', ': 0 = 0')[0], self.propose('node-0', ': 0 = 0')[0])
        task = self.task('task', self.one, self.root)
        artifact = self.store.propose_group_artifact(self.group, 'artifact', self.one, task,
            ': True ∧ True', ['Init'], 'lean-test', 'constructor <;> trivial')
        binding = self.store.bind_group_artifact_verifier('pinned')
        # Populate a full history fixture; the database constraint protects
        # verification commits as well as calls through graph helper methods.
        with self.store.transaction() as db:
            db.executemany('INSERT INTO group_artifact_outcomes VALUES (?,?,?,?,?,?)',
                           [(self.group, artifact, 'rejected', 'historical', 'pinned', i)
                            for i in range(256)])
        revision = self.revision()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'history limit'):
            self.store.checked_group_artifact(artifact, 'verified', binding=binding)
        self.assertEqual(self.store.group(self.group)['artifacts'][0]['status'], 'pending')
        self.assertEqual(self.revision(), revision)

    def test_new_root_creation_is_atomic(self):
        original = self.store.transaction

        @contextmanager
        def failing():
            with original() as db:
                db.execute('''CREATE TEMP TRIGGER fail_root BEFORE INSERT ON claim_publications
                    BEGIN SELECT RAISE(ABORT,'root failure'); END''')
                yield db

        with patch.object(self.store, 'transaction', failing), self.assertRaisesRegex(sqlite3.IntegrityError, 'root failure'):
            self.store.create_group('failed', ': True', ['Init'], 'lean-test')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM agent_groups').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM group_graphs').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM group_claims').fetchone()[0], 1)

    def test_version_18_migration_no_fabricated_links_and_v1_compatibility(self):
        old_path = self.path.parent / 'old.db'
        # Build an actual v18 database using the shipped migration chain.
        with patch('solvenet.store.MIGRATION_19', 'PRAGMA user_version=19;'), \
                patch('solvenet.store.MIGRATION_20', 'PRAGMA user_version=20;'), \
                patch('solvenet.store.MIGRATION_21', 'PRAGMA user_version=21;'):
            Store(old_path)
        with closing(sqlite3.connect(old_path)) as db, db:
            db.execute('PRAGMA user_version=18')
            db.execute('INSERT INTO agent_groups VALUES (?,?,?,?,?,?,?,?,?,?)',
                       ('old', 'old', ': True', '["Init"]', 'lean-test', 32, 30, 32, 128, 0))
            db.execute('INSERT INTO group_agents(id,group_id,request_key,role) VALUES (?,?,?,?)',
                       ('agent', 'old', 'agent', 'investigator'))
            db.execute('''INSERT INTO group_tasks
                (id,group_id,request_key,creator_id,owner_id,description,budget,remaining,depth)
                VALUES ('task','old','task','agent','agent','Historical',2,2,0)''')
            db.execute('''INSERT INTO group_messages
                (id,group_id,request_key,agent_id,task_id,kind,text)
                VALUES ('message','old','message','agent','task','finding','Historical')''')
            db.execute("UPDATE artifact_verifier_binding SET identity='old-pinned',revision=17 WHERE id=1")
            for index, status in enumerate(('verified', 'rejected', 'timeout', 'pending')):
                db.execute('''INSERT INTO group_artifacts
                    (id,group_id,request_key,agent_id,task_id,statement,imports,environment,proof,
                     status,diagnostics,verifier_identity,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (f'artifact-{index}', 'old', f'artifact-{index}', 'agent', 'task', ': True',
                     '["Init"]', 'lean-test', 'trivial', status, f'Historical {status}',
                     'old-pinned' if status == 'verified' else None, 'coordinator'))
        migrated = Store(old_path)
        old = migrated.group('old')
        self.assertEqual(old['graph']['revision'], 0)
        self.assertIsNone(old['graph']['root_id'])
        self.assertEqual(migrated.group_claim_neighborhood('old')['claims'], [])
        self.assertEqual(len(old['tasks']), 1)
        self.assertEqual(len(old['messages']), 1)
        with migrated.connect() as db:
            outcomes = [dict(r) for r in db.execute('SELECT * FROM group_artifact_outcomes ORDER BY rowid')]
        self.assertEqual([r['status'] for r in outcomes], ['verified', 'rejected', 'timeout'])
        self.assertEqual(outcomes[0]['verifier_identity'], 'old-pinned')
        self.assertEqual(outcomes[0]['diagnostics'], 'Historical verified')
        self.assertTrue(all(r['verifier_revision'] is None for r in outcomes))
        migrated.bind_group_artifact_verifier('new-pinned')
        artifacts = {a['id']: a for a in migrated.group('old')['artifacts']}
        self.assertEqual(artifacts['artifact-0']['status'], 'pending')
        self.assertEqual(artifacts['artifact-0']['diagnostics'], '')
        self.assertIsNone(artifacts['artifact-0']['verifier_identity'])
        with Store(old_path).connect() as db:
            self.assertEqual([dict(r) for r in db.execute('SELECT * FROM group_artifact_outcomes ORDER BY rowid')], outcomes)
        self.assertEqual(migrated.create_group('old', ': True', ['Init'], 'lean-test'), 'old')
        migrated.add_group_task('old', 'new-task', 'agent', 'agent', 'New work', 1)
        self.assertEqual(migrated.group('old')['graph']['revision'], 0)
        # A normal independent proof run neither creates nor consults a graph.
        run = migrated.submit(': True', ['Init'])
        self.assertIsNotNone(migrated.run(run['run_id']))
        with migrated.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM group_claims').fetchone()[0], 0)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
