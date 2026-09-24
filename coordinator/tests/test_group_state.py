import tempfile
import unittest
from pathlib import Path

from solvenet.store import Conflict, Store


class GroupStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'coordinator.db'
        self.store = Store(self.path)

    def group_with_agents(self, statement='True', environment='lean-v1', **options):
        group = self.store.create_group('campaign', statement, ['Init'], environment, **options)
        planner = self.store.add_agent(group, 'planner', 'planner')
        one = self.store.add_agent(group, 'one', 'investigator')
        two = self.store.add_agent(group, 'two', 'investigator')
        return group, planner, one, two

    def enqueue(self, group, task, agent, key='call', environment='lean-v1', **options):
        return self.store.enqueue_group_job(
            group, task, agent, key, environment, 'scripted', 'finding',
            [{'role': 'user', 'content': 'Investigate the goal'}], **options)

    def test_restart_provenance_and_idempotence(self):
        group, planner, one, two = self.group_with_agents(max_work=4, max_tasks=3, max_messages=3)
        parent = self.store.add_group_task(group, 'root', planner, one, 'Investigate', 2)
        child = self.store.add_group_task(group, 'child', planner, two, 'Second approach', 2, parent_id=parent)
        self.assertEqual(self.store.add_group_task(group, 'child', planner, two, 'Second approach', 2,
                                                   parent_id=parent), child)
        self.assertEqual(self.store.update_agent_state(group, one, 'update', 'working', 0), 1)
        job = self.enqueue(group, parent, one)
        with self.assertRaises(Conflict):
            self.store.add_group_message(group, 'premature', one, parent, 'finding',
                                         'Try constructor', job_id=job)
        lease = self.store.claim('worker', ['scripted'], supports_model_respond=True)
        self.store.result(lease['assignment_id'], {
            'lease_token': lease['lease_token'], 'status': 'completed',
            'output': {'type': 'finding', 'text': 'Try constructor'}})
        with self.assertRaises(Conflict):
            self.store.add_group_message(group, 'false-source', one, parent, 'finding',
                                         'Different text', job_id=job)
        finding = self.store.add_group_message(group, 'finding', one, parent, 'finding',
                                               'Try constructor', job_id=job)
        self.store.review_group_message(group, finding, planner, 'review', 'accepted', 'Useful')
        reopened = Store(self.path)
        self.assertEqual(reopened.create_group('campaign', 'True', ['Init'], 'lean-v1',
                                               max_work=4, max_tasks=3, max_messages=3), group)
        self.assertEqual(self.enqueue(group, parent, one), job)
        self.assertEqual(reopened.add_group_message(group, 'finding', one, parent, 'finding',
                                                     'Try constructor', job_id=job), finding)
        reopened.review_group_message(group, finding, planner, 'review', 'accepted', 'Useful')
        self.assertEqual(reopened.update_agent_state(group, one, 'update', 'working', 0), 1)
        state = reopened.group(group)
        self.assertEqual(state['remaining_work'], 0)
        self.assertEqual([(t['id'], t['parent_id'], t['remaining']) for t in state['tasks']],
                         [(parent, None, 1), (child, parent, 2)])
        self.assertEqual(state['agents'][1]['state'], 'working')
        self.assertEqual(state['messages'][0]['verification_status'], 'unverified')
        self.assertEqual(state['messages'][0]['review_status'], 'accepted')
        self.assertEqual(state['jobs'][0]['agent_id'], one)
        self.assertEqual(state['jobs'][0]['environment'], 'lean-v1')
        self.assertEqual(state['run']['environment'], 'lean-v1')
        self.assertEqual(reopened.run(state['run']['run_id'])['problem']['statement'], 'True')
        self.assertEqual(reopened.run(state['run']['run_id'])['status'], 'exhausted')

    def test_atomic_rollback_and_claimable_binding(self):
        group, _, one, _ = self.group_with_agents(max_work=1)
        task = self.store.add_group_task(group, 'task', one, one, 'Investigate', 1)
        with self.assertRaises(Conflict):
            self.enqueue(group, task, one, cost=2)
        self.assertIsNone(self.store.group(group)['run'])
        self.assertEqual(self.store.group(group)['tasks'][0]['remaining'], 1)
        # Fail after inserting the run and job: the entire transaction rolls back.
        original = self.store.transaction

        def failing_transaction():
            context = original()
            class Wrapper:
                def __enter__(self):
                    db = context.__enter__()
                    db.execute('''CREATE TEMP TRIGGER fail_group_job BEFORE INSERT ON group_jobs
                        BEGIN SELECT RAISE(ABORT, 'injected failure'); END''')
                    return db

                def __exit__(self, *args):
                    return context.__exit__(*args)
            return Wrapper()

        self.store.transaction = failing_transaction
        try:
            with self.assertRaisesRegex(Exception, 'injected failure'):
                self.enqueue(group, task, one)
        finally:
            self.store.transaction = original
        self.assertIsNone(self.store.group(group)['run'])
        self.assertEqual(self.store.group(group)['tasks'][0]['remaining'], 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
        job = self.enqueue(group, task, one)
        self.assertEqual(self.store.claim('worker', ['scripted'], supports_model_respond=True)['job']['id'], job)
        with self.assertRaises(Conflict):
            self.store.enqueue_task(self.store.group(group)['run']['run_id'], 'scripted', 'finding',
                                    [{'role': 'user', 'content': 'bypass'}])

    def test_agent_task_and_environment_mismatch(self):
        group, planner, one, two = self.group_with_agents(max_work=2)
        task = self.store.add_group_task(group, 'task', planner, one, 'Investigate', 2)
        with self.assertRaises(Conflict):
            self.enqueue(group, task, two)
        with self.assertRaises(Conflict):
            self.enqueue(group, task, one, environment='lean-v2')
        self.assertIsNone(self.store.group(group)['run'])
        job = self.enqueue(group, task, one)
        with self.assertRaises(Conflict):
            self.store.add_group_message(group, 'bad', two, task, 'finding', 'Wrong agent', job_id=job)
        with self.assertRaises(Conflict):
            self.enqueue(group, task, one, environment='lean-v2')
        with self.assertRaises(Conflict):
            self.enqueue(group, task, one, cost=2)
        self.assertEqual(self.store.group(group)['tasks'][0]['remaining'], 1)
        with self.assertRaises(Conflict):
            self.store.create_group('campaign', 'True', ['Init'], 'lean-v2', max_work=2)
        other = self.store.create_group('other-env', 'True', ['Init'], 'lean-v2')
        foreign = self.store.add_agent(other, 'foreign', 'investigator')
        foreign_task = self.store.add_group_task(other, 'foreign-task', foreign, foreign, 'Investigate', 1)
        with self.assertRaises(Conflict):
            self.store.add_group_message(other, 'wrong-env-job', foreign, foreign_task,
                                         'finding', 'Wrong source', job_id=job)
        with self.assertRaises(Conflict):
            self.store.enqueue_group_job(other, foreign_task, foreign, 'call', 'lean-v1',
                                         'scripted', 'finding', [{'role': 'user', 'content': 'Wrong env'}])

    def test_cross_group_duplicates_limits_and_revisions(self):
        group, planner, one, _ = self.group_with_agents(max_work=2, max_tasks=1, max_messages=1)
        other = self.store.create_group('other', 'False', ['Init'], 'lean-v1')
        foreign = self.store.add_agent(other, 'foreign', 'investigator')
        with self.assertRaises(Conflict):
            self.store.add_group_task(group, 'bad', planner, foreign, 'cross group', 1)
        task = self.store.add_group_task(group, 'task', planner, one, 'Investigate', 2)
        with self.assertRaises(Conflict):
            self.store.add_group_task(group, 'extra', planner, one, 'Overflow', 1)
        with self.assertRaises(Conflict):
            self.store.add_group_task(group, 'task', planner, one, 'Different', 2)
        with self.assertRaises(Conflict):
            self.store.add_group_message(other, 'bad', foreign, task, 'finding', 'cross group')
        with self.assertRaises(Conflict):
            self.enqueue(other, task, foreign)
        message = self.store.add_group_message(group, 'msg', one, task, 'question', 'What next?')
        with self.assertRaises(Conflict):
            self.store.add_group_message(group, 'msg2', one, task, 'finding', 'More')
        with self.assertRaises(Conflict):
            self.store.add_group_message(group, 'msg', one, task, 'question', 'Changed')
        with self.assertRaises(Conflict):
            self.store.review_group_message(group, message, foreign, 'review', 'accepted')
        self.store.review_group_message(group, message, planner, 'review', 'redirected')
        with self.assertRaises(Conflict):
            self.store.review_group_message(group, message, planner, 'review2', 'accepted')
        with self.assertRaises(ValueError):
            self.store.update_agent_state(group, one, 'update', 'x' * 4097, 0)
        self.assertEqual(self.store.update_agent_state(group, one, 'first', 'note', 0), 1)
        with self.assertRaises(Conflict):
            self.store.update_agent_state(group, one, 'second', 'stale', 0)

    def test_depth_and_budget_after_restart(self):
        group, _, one, _ = self.group_with_agents(max_work=6, max_tasks=6)
        parent = None
        for depth in range(5):
            parent = self.store.add_group_task(group, str(depth), one, one, 'Step', 1, parent_id=parent)
        with self.assertRaises(Conflict):
            self.store.add_group_task(group, 'too-deep', one, one, 'Step', 1, parent_id=parent)
        reopened = Store(self.path)
        self.assertEqual(reopened.group(group)['remaining_work'], 1)
        job = self.enqueue(group, parent, one)
        with self.assertRaises(Conflict):
            self.store.enqueue_group_job(group, parent, one, 'second', 'lean-v1', 'scripted',
                                         'finding', [{'role': 'user', 'content': 'Another'}])
        self.assertEqual(reopened.group(group)['jobs'][0]['job_id'], job)

    def test_group_run_resumes_after_first_call_without_reusing_a_v1_run(self):
        group, _, one, _ = self.group_with_agents(max_work=2)
        task = self.store.add_group_task(group, 'task', one, one, 'Investigate', 2)
        first = self.enqueue(group, task, one)
        lease = self.store.claim('worker', ['scripted'], supports_model_respond=True)
        self.assertEqual(lease['job']['id'], first)
        self.store.result(lease['assignment_id'], {
            'lease_token': lease['lease_token'], 'status': 'completed',
            'output': {'type': 'finding', 'text': 'An observation'}})
        run_id = self.store.group(group)['run']['run_id']
        self.assertEqual(self.store.run(run_id)['status'], 'exhausted')
        reopened = Store(self.path)
        self.assertEqual(reopened.enqueue_group_job(group, task, one, 'call', 'lean-v1',
                         'scripted', 'finding', [{'role': 'user', 'content': 'Investigate the goal'}]), first)
        second = reopened.enqueue_group_job(group, task, one, 'next', 'lean-v1',
                                            'scripted', 'question', [{'role': 'user', 'content': 'Next?'}])
        self.assertNotEqual(first, second)
        self.assertEqual(reopened.run(run_id)['status'], 'running')
        self.assertEqual(reopened.group(group)['tasks'][0]['remaining'], 0)
        self.assertEqual(reopened.claim('worker', ['scripted'], supports_model_respond=True)['job']['id'], second)

    def test_budget_reserves_all_retry_assignments(self):
        group, _, one, _ = self.group_with_agents(max_work=2)
        task = self.store.add_group_task(group, 'task', one, one, 'Investigate', 2)
        job = self.enqueue(group, task, one, cost=2)
        first = self.store.claim('worker', ['scripted'], supports_model_respond=True)
        self.assertEqual(first['job']['id'], job)
        self.store.result(first['assignment_id'], {
            'lease_token': first['lease_token'], 'status': 'failed',
            'failure_class': 'transient', 'error': 'unavailable'})
        retry = self.store.claim('worker', ['scripted'], supports_model_respond=True)
        self.assertEqual(retry['job']['id'], job)
        self.store.result(retry['assignment_id'], {
            'lease_token': retry['lease_token'], 'status': 'failed',
            'failure_class': 'transient', 'error': 'unavailable'})
        self.assertIsNone(self.store.claim('worker', ['scripted'], supports_model_respond=True))
        self.assertEqual(self.store.group(group)['tasks'][0]['remaining'], 0)


if __name__ == '__main__':
    unittest.main()
