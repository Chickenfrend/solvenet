import json
import tempfile
import unittest
from pathlib import Path

from solvenet.context_packet import prompt_cost
from solvenet.graph_response import graph_batch
from solvenet.store import Store


class GraphInstructionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'state.db')
        self.group = self.store.create_group('g', ': True ∧ True', ['Init'], 'pinned', max_work=64)
        self.root = self.store.group(self.group)['graph']['root_id']
        self.agent = self.store.add_agent(self.group, 'a', 'investigator')
        self.store.bind_group_artifact_verifier('pinned-verifier')
        self.index = 0

    def dispatch(self, kind, claim=None):
        self.index += 1
        task = self.store.add_group_task(self.group, str(self.index), self.agent, self.agent,
            'Respond', 2, claim_id=claim or self.root)
        job = self.store.enqueue_group_job(self.group, task, self.agent, str(self.index),
            'pinned', 'scripted', kind, [dict(role='user', content='Respond')], graph_context=True)
        lease = self.store.claim('worker', ['scripted'], supports_model_respond=True)
        self.assertEqual(lease['job']['id'], job)
        frozen = self.store.job_context_packet(job)
        self.assertEqual(frozen['messages'], lease['job']['messages'])
        prompt = frozen['messages'][-1]['content']
        example = json.loads(prompt.split('solution):\n')[1].split('\n')[0])
        empty = json.loads(prompt.split('No useful decomposition is valid: ')[1].split('\n')[0])
        self.assertEqual(json.loads(empty['text']), dict(graph_schema='solvenet.graph.v1'))
        self.assertEqual(frozen['budget']['input_byte_upper_bound'],
            prompt_cost(': True ∧ True', ['Init'], frozen['messages'], 0))
        self.assertEqual(frozen['budget']['admission_upper_bound'],
            frozen['budget']['input_byte_upper_bound'] + 512)
        self.assertGreater(frozen['budget']['input_byte_upper_bound'],
            prompt_cost(': True ∧ True', ['Init'], [dict(role='user', content='Respond')], 0) + 1000)
        return job, lease, frozen, example, empty

    def complete(self, dispatched, envelope):
        job, lease, _, _, _ = dispatched
        # Use the exact shown text through the actual completion/ingestion path.
        self.store.result(lease['assignment_id'], dict(status='completed', lease_token=lease['lease_token'],
            output=dict(type=lease['job']['task_type'], **envelope)))
        return self.store.ingest_group_graph_response(self.group, job)

    def test_shown_plan_and_empty_are_accepted_without_seed_claims(self):
        d = self.dispatch('plan')
        receipt = self.complete(d, d[3])
        self.assertEqual(receipt['status'], 'accepted')
        self.assertEqual(receipt['claims']['c'], self.root)
        self.assertEqual(len(receipt['findings']), 1)
        d = self.dispatch('plan')
        self.assertEqual(self.complete(d, d[4])['status'], 'accepted')

    def test_shown_finding_context_is_exact_and_artifact_pending(self):
        d = self.dispatch('finding')
        self.assertEqual(self.complete(d, d[3])['status'], 'accepted')
        artifact = self.store.group(self.group)['artifacts'][0]
        self.assertEqual(artifact['status'], 'pending')
        self.assertEqual(artifact['statement'], ': True ∧ True')
        self.assertEqual(artifact['proof'], 'YOUR_LEAN_PROOF_BODY')

    def test_critique_without_received_edges_is_accepted(self):
        d = self.dispatch('critique')
        self.assertEqual(self.complete(d, d[3])['status'], 'accepted')

    def test_review_example_uses_received_edge_and_unreceived_edge_is_rejected(self):
        claim = self.store.propose_group_claim(self.group, 'aux', ': True', ['Init'], 'pinned')[0]
        edge = self.store.propose_group_relationship(self.group, 'edge', self.root, claim, 'suggests_using')
        d = self.dispatch('critique')
        batch = json.loads(d[3]['text'])
        self.assertEqual(batch['reviews'][0]['relationship'], edge)
        self.assertEqual(self.complete(d, d[3])['status'], 'accepted')
        d = self.dispatch('critique')
        # A valid group ID published after dispatch must not become received evidence.
        late = self.store.propose_group_relationship(self.group, 'late', self.root, claim, 'alternative_to')
        batch = json.loads(d[3]['text'])
        batch['reviews'][0]['relationship'] = late
        receipt = self.complete(d, dict(text=json.dumps(batch)))
        self.assertEqual(receipt['status'], 'rejected')
        self.assertIn('received context packet', receipt['error'])

    def test_invalid_array_nesting_and_local_ids_report_errors(self):
        d = self.dispatch('plan')
        batch = json.loads(d[3]['text'])
        batch['claims'] = dict(batch['claims'][0])
        self.assertIn('Invalid graph array', self.complete(d, dict(text=json.dumps(batch)))['error'])
        d = self.dispatch('plan')
        batch = json.loads(d[3]['text'])
        batch['findings'][0]['claim'] = '$missing'
        self.assertIn('Unknown job-local reference', self.complete(d, dict(text=json.dumps(batch)))['error'])
        self.assertIsNone(graph_batch(json.dumps(d[3])))  # Outer envelope is not itself a graph batch.

    def test_schema_instructions_participate_in_full_prompt_admission(self):
        task = self.store.add_group_task(self.group, 'small', self.agent, self.agent,
            'Respond', 2, claim_id=self.root)
        messages = [dict(role='user', content='Respond')]
        self.assertLess(prompt_cost(': True ∧ True', ['Init'], messages, 512), 2500)
        with self.assertRaisesRegex(ValueError, 'complete messages exceed context budget'):
            self.store.enqueue_group_job(self.group, task, self.agent, 'small', 'pinned',
                'scripted', 'plan', messages, graph_context=True, context_limit=2500)
        self.assertEqual(self.store.group(self.group)['jobs'], [])
