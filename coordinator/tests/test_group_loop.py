import json
import tempfile
import unittest
from pathlib import Path

from solvenet.server import Coordinator
from solvenet.store import Store
from solvenet.verifier import LeanVerifier, VerificationStatus


class GroupLoopTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'state.db'
        self.now = [100.0]
        self.store = Store(self.path, lease_seconds=5, clock=lambda: self.now[0])
        self.models = {role: 'scripted' for role in
                       ('planner', 'investigator', 'critic', 'synthesizer')}
        self.group = self.store.start_group_loop('target', ': True ∧ True', ['Init'],
                                                 'lean-test', self.models)

    def drive(self, key, text, *, expires=False):
        for _ in range(12):
            self.store.advance_group(self.group)
            lease = self.store.claim('worker', ['scripted'], supports_model_respond=True)
            if lease:
                break
        else:
            self.fail(f'No claim for {key}')
        current = next(j for j in self.store.group(self.group)['jobs'] if j['job_id'] == lease['job']['id'])
        self.assertEqual(current['request_key'], key)
        if expires:
            self.now[0] += 6
            self.store = Store(self.path, lease_seconds=5, clock=lambda: self.now[0])
            self.store.expire()
            with self.assertRaises(Exception):
                self.store.result(lease['assignment_id'], {
                    'lease_token': lease['lease_token'], 'status': 'completed',
                    'output': {'type': lease['job']['task_type'], 'text': text}})
            lease = self.store.claim('other-worker', ['scripted'], supports_model_respond=True)
            self.assertEqual(current['job_id'], lease['job']['id'])
        if key == 'synthesize':
            self.assertEqual(lease['job']['kind'], 'model.generate')
            self.assertIn('UNVERIFIED', lease['job']['messages'][0]['content'])
            output = {'text': text}
        else:
            self.assertEqual(lease['job']['kind'], 'model.respond')
            output = {'type': lease['job']['task_type'], 'text': text}
        self.store.result(lease['assignment_id'], {'lease_token': lease['lease_token'],
                                                    'status': 'completed', 'output': output})
        self.store.advance_group(self.group)
        return lease

    def collaboration(self, proof, *, expire=False):
        self.drive('plan', json.dumps({'approaches': ['Study first conjunct', 'Study second conjunct']}))
        self.drive('investigate-1', 'First conjunct has proof True.intro', expires=expire)
        self.drive('investigate-2', 'Second conjunct has proof True.intro')
        self.drive('review', json.dumps({'decisions': ['redirect', 'accept']}))
        redirected = self.drive('redirect', 'Combine the two constructors')
        self.assertIn('Second conjunct', redirected['job']['messages'][0]['content'])
        self.drive('synthesize', proof)

    def test_restart_expiry_and_real_lean_synthesis(self):
        self.collaboration('constructor <;> trivial', expire=True)
        group = self.store.group(self.group)
        self.assertEqual(len(group['jobs']), 6)
        self.assertEqual(group['remaining_work'], 0)
        self.assertEqual(len(group['agents']), 5)
        self.assertEqual([m['review_status'] for m in group['messages'][1:3]],
                         ['redirected', 'accepted'])
        self.assertEqual(group['messages'][4]['verification_status'], 'unverified')
        run_id = group['run']['run_id']
        self.store = Store(self.path, lease_seconds=5, clock=lambda: self.now[0])
        self.assertFalse(self.store.advance_group(self.group))  # awaiting verifier
        verifier = LeanVerifier(Path(__file__).resolve().parents[2] / 'lean',
                                command=(str(Path.home() / '.elan/bin/lake'), 'env', 'lean'))
        coordinator = Coordinator(self.store, verifier)
        self.assertTrue(coordinator.tick())
        self.assertFalse(self.store.advance_group(self.group))
        self.assertEqual(self.store.group_loop(self.group)['reason'], 'verified_target')
        self.assertEqual(self.store.run_status(run_id)['status'], 'solved')
        self.assertEqual(len(self.store.run(run_id)['attempts']), 1)
        self.assertEqual(len(self.store.run(run_id)['assignments']), 7)  # expired lease + six calls

    def test_unverified_auxiliary_and_no_solution(self):
        self.collaboration('exact True.intro')  # proves True, but not True ∧ True
        run_id = self.store.group(self.group)['run']['run_id']
        attempt = self.store.pending()
        verifier = LeanVerifier(Path(__file__).resolve().parents[2] / 'lean',
                                command=(str(Path.home() / '.elan/bin/lake'), 'env', 'lean'))
        result = verifier.verify(attempt['statement'], attempt['candidate'],
                                 imports=json.loads(attempt['imports']))
        self.assertEqual(result.status, VerificationStatus.REJECTED)
        self.store.verified(attempt['id'], result)
        self.assertEqual(self.store.run_status(run_id)['status'], 'exhausted')
        self.assertTrue(self.store.advance_group(self.group))
        self.assertEqual(self.store.group_loop(self.group)['reason'], 'no_verified_target')
        self.assertEqual(self.store.group(self.group)['remaining_work'], 0)
        self.assertFalse(self.store.advance_group(self.group))

    def test_deadline_stops_pending_group(self):
        self.store.advance_group(self.group)
        # Deadline is checked even if a worker has an active lease.
        with self.store.transaction() as db:
            db.execute('UPDATE group_loops SET deadline=? WHERE group_id=?', (101, self.group))
        self.now[0] = 102
        self.assertTrue(self.store.advance_group(self.group))
        self.assertEqual(self.store.group_loop(self.group)['reason'], 'deadline')
        self.assertIsNone(self.store.claim('worker', ['scripted'], supports_model_respond=True))

    def test_deadline_cannot_be_starved_by_unrelated_verification(self):
        self.store.advance_group(self.group)  # queued planner job
        ordinary = self.store.submit(': True', ['Init'], attempts=1)
        lease = self.store.claim('worker', ['scripted'])
        self.assertEqual(lease['job']['run_id'], ordinary['run_id'])
        self.store.result(lease['assignment_id'], {'lease_token': lease['lease_token'],
                          'status': 'completed', 'output': {'text': 'trivial'}})
        self.now[0] += 3600
        # Even before scheduler reconciliation, a claim cannot lease expired group work.
        self.assertIsNone(self.store.claim('late', ['scripted'], supports_model_respond=True))
        verifier = LeanVerifier(Path(__file__).resolve().parents[2] / 'lean',
                                command=(str(Path.home() / '.elan/bin/lake'), 'env', 'lean'))
        self.assertTrue(Coordinator(self.store, verifier).tick())
        self.assertEqual(self.store.group_loop(self.group)['reason'], 'deadline')
        self.assertEqual(self.store.run_status(ordinary['run_id'])['status'], 'solved')

    def test_no_compatible_worker_terminates_at_deadline(self):
        self.assertEqual(self.store.group_loop(self.group)['deadline'], self.now[0] + 3600)
        coordinator = Coordinator(self.store, None)  # no Lean candidate can exist without a worker
        self.assertTrue(coordinator.tick())
        for _ in range(2):
            self.assertIsNone(self.store.claim('proof-only', ['scripted']))
        self.now[0] += 3600
        self.assertTrue(coordinator.tick())
        self.assertEqual(self.store.group_loop(self.group)['reason'], 'deadline')
        self.assertEqual(self.store.group(self.group)['remaining_work'], 10)
        self.assertIsNone(self.store.claim('compatible-late', ['scripted'], supports_model_respond=True))
        self.assertEqual(self.store.start_group_loop('target', ': True ∧ True', ['Init'],
                                                      'lean-test', self.models), self.group)

    def test_deadline_during_lean_check_reconciles_verified_target(self):
        self.collaboration('constructor <;> trivial')
        run_id = self.store.group(self.group)['run']['run_id']
        pending = self.store.pending()
        self.assertIsNotNone(pending)
        self.now[0] += 3600
        self.assertTrue(self.store.advance_group(self.group))
        self.assertEqual(self.store.group_loop(self.group)['reason'], 'deadline')
        self.assertEqual(self.store.group(self.group)['tasks'][-1]['status'], 'cancelled')
        verifier = LeanVerifier(Path(__file__).resolve().parents[2] / 'lean',
                                command=(str(Path.home() / '.elan/bin/lake'), 'env', 'lean'))
        result = verifier.verify(pending['statement'], pending['candidate'],
                                 imports=json.loads(pending['imports']))
        self.assertTrue(result.verified)
        reopened = Store(self.path, clock=lambda: self.now[0])
        reopened.verified(pending['id'], result)
        self.assertEqual(reopened.run_status(run_id)['status'], 'solved')
        self.assertEqual(reopened.group_loop(self.group)['reason'], 'verified_target')
        self.assertEqual(reopened.group(self.group)['tasks'][-1]['status'], 'done')
        reopened.verified(pending['id'], result)
        self.assertEqual(reopened.group_loop(self.group)['reason'], 'verified_target')

    def test_deadline_validation(self):
        for deadline in (float('nan'), float('inf'), float('-inf'), 100, 3701, True):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                self.store.start_group_loop('invalid-' + str(deadline), ': True', ['Init'],
                                            'lean-test', self.models, deadline=deadline)


if __name__ == '__main__':
    unittest.main()
