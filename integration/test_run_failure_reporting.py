"""Coordinator result/lease lifecycle → homelab outcome regression."""

import tempfile
import unittest
from pathlib import Path

from solvenet.store import Store
from solvenet.verifier import VerificationResult, VerificationStatus
from solvenet_homelab.runs import detail, outcome


class RunFailureReportingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = 1000
        self.store = Store(Path(directory.name) / 'coordinator.db',
                           clock=lambda: self.now, lease_seconds=3)
        self.run_id = self.store.submit(': True', ['Init'], attempts=1)['run_id']

    def claim(self):
        return self.store.claim('worker', ['scripted'])

    def fail(self, lease, failure_class='transient', error='service unavailable'):
        payload = {'lease_token': lease['lease_token'], 'status': 'failed'}
        if failure_class is not None:
            payload['failure_class'] = failure_class
        if error is not None:
            payload['error'] = error
        self.store.result(lease['assignment_id'], payload)

    def test_permanent_provider_failure_is_a_completed_lease(self):
        self.fail(self.claim(), 'permanent', 'OpenAI credential rejected <provider>')
        run = self.store.run(self.run_id)
        self.assertEqual(run['status'], 'exhausted')
        self.assertEqual(run['assignments'][0]['status'], 'completed')
        self.assertEqual(run['assignments'][0]['failure_class'], 'permanent')
        self.assertEqual(run['attempts'], [])
        self.assertIsNone(self.claim())
        html = detail(run)
        self.assertIn('Outcome: zero candidates; provider/worker failure', html)
        self.assertIn('1 failed leased assignment(s); no proof reached Lean', html)
        self.assertIn('OpenAI credential rejected &lt;provider&gt;', html)
        self.assertNotIn('provider unreachable', html)
        self.assertNotIn('candidate(s) rejected by Lean', html)

    def test_exhausted_retry_counts_reported_failures_not_expired_leases(self):
        self.claim()
        self.now += 4
        self.fail(self.claim(), error='dial tcp: connection refused <offline>')
        self.fail(self.claim(), error='dial tcp: connection refused <offline>')
        run = self.store.run(self.run_id)
        self.assertEqual(run['status'], 'exhausted')
        self.assertEqual(len(run['assignments']), 3)
        expired = next(lease for lease in run['assignments'] if lease['status'] == 'expired')
        self.assertIsNone(expired['failure_class'])
        html = detail(run)
        self.assertIn('Outcome: zero candidates; provider unreachable', html)
        self.assertIn('2 failed leased assignment(s); no proof reached Lean', html)
        self.assertIn('Most common reported failure: dial tcp: connection refused &lt;offline&gt;', html)
        self.assertNotIn('3 failed leased assignment(s)', html)

    def test_transient_failure_then_success_reports_verified_proof(self):
        self.fail(self.claim())
        run = self.store.run(self.run_id)
        self.assertEqual(run['status'], 'running')
        self.assertIn('Outcome: Awaiting candidates', outcome(run))
        lease = self.claim()
        self.store.result(lease['assignment_id'], {
            'lease_token': lease['lease_token'], 'status': 'completed',
            'output': {'text': 'exact True.intro'}})
        pending = self.store.pending()
        self.assertIn('Outcome: Verification pending', outcome(self.store.run(self.run_id)))
        self.store.verified(pending['id'], VerificationResult(VerificationStatus.VERIFIED, '', 0))
        run = self.store.run(self.run_id)
        self.assertEqual(run['status'], 'solved')
        self.assertEqual(len(run['assignments']), 2)
        self.assertEqual(sum(lease['failure_class'] is not None for lease in run['assignments']), 1)
        html = detail(run)
        self.assertIn('Outcome: Initial proof verified', html)
        self.assertIn('Failure: service unavailable', html)
        self.assertNotIn('zero candidates', html)

    def test_expired_leases_without_results_have_unknown_cause(self):
        for _ in range(3):
            self.assertIsNotNone(self.claim())
            self.now += 4
            self.store.expire()
        run = self.store.run(self.run_id)
        self.assertEqual(run['status'], 'exhausted')
        self.assertTrue(all(lease['status'] == 'expired' for lease in run['assignments']))
        html = detail(run)
        self.assertIn('Outcome: zero candidates</h2>', html)
        self.assertNotIn('provider/worker failure', html)
        self.assertNotIn('provider unreachable', html)
        self.assertNotIn('failed leased assignment(s)', html)

    def test_failed_results_without_error_still_count_as_failures(self):
        # Older workers omit failure_class; the coordinator defaults it to transient.
        for _ in range(3):
            self.fail(self.claim(), failure_class=None, error=None)
        run = self.store.run(self.run_id)
        self.assertEqual(run['status'], 'exhausted')
        html = detail(run)
        self.assertIn('Outcome: zero candidates; provider/worker failure', html)
        self.assertIn('3 failed leased assignment(s); no proof reached Lean', html)
        self.assertIn('Reported failure: Unknown provider failure', html)
        self.assertNotIn('provider unreachable', html)


if __name__ == '__main__':
    unittest.main()
