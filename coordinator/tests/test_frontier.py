import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from solvenet.composed import declaration_name
from solvenet.context_packet import build_packet
from solvenet.frontier import FrontierLimit
from solvenet.proof_context import VerificationBusy
from solvenet.server import Coordinator
from solvenet.store import Conflict, Store
from solvenet.verifier import LeanVerifier, VerificationResult, VerificationStatus


class FrontierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.db'
        self.now = [100.0]
        self.store = Store(self.path, lease_seconds=5, clock=lambda: self.now[0])
        self.verifier = LeanVerifier(Path(__file__).resolve().parents[2] / 'lean',
                                    command=(str(Path.home() / '.elan/bin/lake'), 'env', 'lean'))
        self.coordinator = Coordinator(self.store, self.verifier)
        self.models = {r: 'scripted' for r in ('planner', 'investigator', 'critic', 'synthesizer')}

    def start(self, key='graph', **kwargs):
        return self.store.start_group_loop(key, ': True ∧ (True ∧ True)', ['Init'],
            'lean-test', self.models, mode='graph', max_work=64, **kwargs)

    def next(self, group):
        for _ in range(8):
            self.coordinator.tick()
            lease = self.store.claim('worker', ['scripted'], supports_model_respond=True)
            if lease:
                decision = next(d for d in self.store.frontier_trace(group)['decisions'] if d['job_id'] == lease['job']['id'])
                return lease, decision
        self.fail('No next frontier assignment: ' + str(self.store.group_loop(group)))

    def complete(self, lease, value):
        payload = dict(lease_token=lease['lease_token'], status='completed',
                       output=dict(text=json.dumps(value) if isinstance(value, dict) else value))
        if lease['job']['kind'] == 'model.respond':
            payload['output']['type'] = lease['job']['task_type']
        self.store.result(lease['assignment_id'], payload)
        self.store.result(lease['assignment_id'], payload)

    def plan(self, group, reason=''):
        lease, decision = self.next(group)
        self.assertEqual(decision['action'], 'plan')
        root = self.store.group(group)['graph']['root_id']
        self.complete(lease, dict(graph_schema='solvenet.graph.v1', claims=[
            dict(key=k, statement=s, imports=['Init'], environment='lean-test') for k, s in
            [('a', ': True'), ('b', ': True ∧ True'), ('c', ': True ∨ False')]],
            relationships=[dict(key='edge-' + k, **{'from': root, 'to': '$' + k}, kind='suggests_using',
                                reason=reason if k == 'a' else '')
                           for k in ('a', 'b', 'c')],
            priorities=[dict(key='p-' + k, claim='$' + k, priority=p) for k, p in [('a', 3), ('b', 2), ('c', 1)]]))
        receipt = self.store.ingest_group_graph_response(group, lease['job']['id'])
        return receipt['claims'], receipt['relationships']

    def artifact(self, claim, statement, proof, prerequisites=None):
        item = dict(key='proof', claim=claim, statement=statement, imports=['Init'],
                    environment='lean-test', proof=proof)
        if prerequisites is not None:
            item['prerequisite_proof_ids'] = prerequisites
        return dict(graph_schema='solvenet.graph.v1', artifacts=[item])

    def scenario(self, outcome):
        group = self.start(outcome)
        claims, edges = self.plan(group)
        lease, decision = self.next(group)
        self.assertEqual((decision['claim_id'], decision['action']), (claims['a'], 'investigate'))
        if outcome == 'challenged':
            response = dict(graph_schema='solvenet.graph.v1', reviews=[dict(
                key='challenge', relationship=edges['edge-a'], status='challenged', reason='Try the other branch')])
        else:
            response = self.artifact(claims['a'], ': True', 'exact True.intro' if outcome == 'verified' else 'exact False.elim (by assumption)')
        self.complete(lease, response)
        next_lease, chosen = self.next(group)
        packet = self.store.job_context_packet(next_lease['job']['id'])
        self.assertLessEqual(packet['budget']['packet_bytes'], 6144)
        self.assertLessEqual(packet['budget']['input_byte_upper_bound'], 8192)
        self.assertEqual(packet['messages'], next_lease['job']['messages'])
        self.assertEqual(chosen['graph_revision'], packet['graph_revision'])
        self.assertTrue(chosen['deferred'])
        return group, claims, chosen, next_lease

    def test_paired_three_branch_checked_rejected_and_challenged(self):
        observed = []
        # Use independent databases so only the evidence differs, not old work.
        for outcome in ('verified', 'rejected', 'challenged'):
            self.path = Path(self.temp.name) / (outcome + '.db')
            self.store = Store(self.path, lease_seconds=5, clock=lambda: self.now[0])
            self.coordinator = Coordinator(self.store, self.verifier)
            group, claims, choice, lease = self.scenario(outcome)
            observed.append(choice['action'])
            if outcome == 'verified':
                self.assertEqual(choice['claim_id'], self.store.group(group)['graph']['root_id'])
                self.assertIn('newly checked', choice['reason'])
                self.assertEqual(len(self.store.job_context_packet(lease['job']['id'])['manifest']['declarations']), 1)
            else:
                self.assertEqual(choice['claim_id'], claims['a'])
                self.assertIn('negative formal' if outcome == 'rejected' else 'reviewed challenge', choice['reason'])
        self.assertEqual(observed, ['synthesize', 'critique', 'critique'])

    def test_rejected_extra_artifact_cannot_reset_exhausted_target_context(self):
        group, claims, _, target = self.scenario('verified')
        manifest = self.store.job_context_packet(target['job']['id'])['manifest']
        self.complete(target, 'exact True.intro')
        retry, retry_choice = self.next(group)
        self.assertEqual(retry_choice['action'], 'synthesize')
        self.assertEqual(self.store.job_context_packet(retry['job']['id'])['manifest'], manifest)
        self.complete(retry, 'exact True.intro')
        self.coordinator.tick()  # verify the second equivalent target rejection
        original = self.store.group(group)['artifacts'][0]
        extra = self.store.propose_group_artifact(group, 'extra-rejected', original['agent_id'],
            original['task_id'], ': True', ['Init'], 'lean-test', 'exact False.intro')
        self.coordinator.tick()  # check the extra proof of the same immutable claim
        artifacts = self.store.group(group)['artifacts']
        self.assertEqual(artifacts[0]['status'], 'verified')
        self.assertEqual(artifacts[-1]['id'], extra)
        self.assertEqual(artifacts[-1]['status'], 'rejected')
        with self.store.connect() as db:
            claim = db.execute('SELECT claim_id FROM claim_artifacts WHERE artifact_id=?', (extra,)).fetchone()[0]
        self.assertEqual(claim, claims['a'])
        # The actual G4 selection stays identical despite the new rejected evidence.
        messages = [{'role': 'user', 'content': target['job']['messages'][0]['content'].split('\nGRAPH_CONTEXT_JSON')[0]}]
        with self.store.transaction() as db:
            built = build_packet(self.store, db, group, manifest['claim_id'], messages, max_output_tokens=2048)
        self.assertEqual(built['manifest']['declarations'], manifest['declarations'])
        _, choice = self.next(group)
        self.assertNotEqual(choice['action'], 'synthesize')
        self.assertTrue(any(d['action'] == 'synthesize' and d['reason'] == 'equivalent_strategy_exhausted'
                            for d in choice['deferred']))
        targets = [d for d in self.store.frontier_trace(group)['decisions'] if d['action'] == 'synthesize']
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0]['strategy'], targets[1]['strategy'].split('|retry:')[0])

    def test_paired_checked_lemma_challenge_selects_critique_without_invalidating(self):
        actions = []
        for challenged in (False, True):
            self.path = Path(self.temp.name) / ('checked-challenged' if challenged else 'checked')
            self.store = Store(self.path, lease_seconds=5, clock=lambda: self.now[0])
            self.coordinator = Coordinator(self.store, self.verifier)
            group = self.start()
            claims, edges = self.plan(group)
            lease, _ = self.next(group)
            self.complete(lease, self.artifact(claims['a'], ': True', 'exact True.intro'))
            self.coordinator.tick()  # observe Lean evidence before selecting the next task
            checked = self.store.group(group)['artifacts'][0]
            self.assertEqual(checked['status'], 'verified')
            if challenged:
                critic = next(a['id'] for a in self.store.group(group)['agents'] if a['role'] == 'critic')
                self.store.review_group_relationship(group, 'checked-challenge', edges['edge-a'], critic,
                    'challenged', 'The checked lemma may be unsuitable for this plan')
            _, choice = self.next(group)
            actions.append(choice['action'])
            if challenged:
                self.assertEqual(choice['claim_id'], claims['a'])
                self.assertIn('reviewed challenge', choice['reason'])
                task = next(t for t in self.store.group(group)['tasks'] if t['id'] == choice['task_id'])
                self.assertEqual(task['owner_id'], critic)
                self.assertNotEqual(task['owner_id'], checked['agent_id'])
            after = self.store.group(group, verifier_identity=checked['verifier_identity'])['artifacts'][0]
            self.assertEqual(after['status'], 'verified')
            self.assertEqual(after['current_status'], 'verified')
            self.assertEqual(after['verifier_identity'], checked['verifier_identity'])
        self.assertEqual(actions, ['synthesize', 'critique'])

    def test_critique_freezes_exact_trigger_without_unrelated_incoming_broadcast(self):
        group = self.start()
        claims, edges = self.plan(group)
        investigator, _ = self.next(group)
        critic = next(a['id'] for a in self.store.group(group)['agents'] if a['role'] == 'critic')
        unrelated = self.store.propose_group_relationship(group, 'unrelated', claims['c'], claims['a'], 'alternative_to')
        unrelated_review = self.store.review_group_relationship(group, 'unrelated-review', unrelated, critic,
            'promising', 'Not the scheduling trigger')
        self.complete(investigator, dict(graph_schema='solvenet.graph.v1', reviews=[dict(
            key='challenge', relationship=edges['edge-a'], status='challenged', reason='Exact reconsideration evidence')]))
        lease, choice = self.next(group)
        frozen = self.store.job_context_packet(lease['job']['id'])
        packet = json.loads(frozen['packet'])
        self.assertEqual(choice['action'], 'critique')
        review = next(r for r in packet['untrusted']['reviews'] if r['reason'] == 'Exact reconsideration evidence')
        self.assertEqual(review['status'], 'challenged')
        self.assertEqual(review['relationship_id'], edges['edge-a'])
        self.assertTrue(any(r['id'] == edges['edge-a'] and r['to_id'] == claims['a']
                            for r in packet['untrusted']['relationships']))
        self.assertEqual(frozen['request']['relationship_ids'], [edges['edge-a']])
        self.assertEqual(frozen['request']['review_ids'], [review['id']])
        self.assertNotIn(unrelated, frozen['source_ids'])
        self.assertNotIn(unrelated_review, frozen['source_ids'])
        self.store = Store(self.path, lease_seconds=5, clock=lambda: self.now[0])
        self.coordinator = Coordinator(self.store, self.verifier)
        self.assertEqual(self.store.job_context_packet(lease['job']['id']), frozen)
        self.complete(lease, dict(graph_schema='solvenet.graph.v1', reviews=[dict(key='resolve',
            relationship=review['relationship_id'], status='abandoned', reason='Response from packet evidence')]))
        _, choice = self.next(group)
        self.assertNotEqual((choice['action'], choice['claim_id']), ('critique', claims['a']))

    def test_long_multibyte_reasons_reach_investigator_and_critic_with_exact_provenance(self):
        from solvenet.context_packet import CATEGORY_BYTES, encoded_bytes

        def reason(prefix, size):
            remaining = size - len(prefix.encode())
            return prefix + 'λ\n"' * (remaining // 4) + 'x' * (remaining % 4)

        for size in (1200, 2048):
            with self.subTest(reason_bytes=size):
                group = self.start('long-reasons-' + str(size))
                relation_reason = reason('Use reassociation before composition. ', size)
                review_reason = reason('Reconsider whether this lemma advances the target. ', size)
                self.assertEqual(len(relation_reason.encode()), size)
                self.assertEqual(len(review_reason.encode()), size)
                claims, edges = self.plan(group, relation_reason)
                investigator, choice = self.next(group)
                self.assertEqual((choice['action'], choice['claim_id']), ('investigate', claims['a']))

                def assert_received(lease, review_id=None):
                    frozen = self.store.job_context_packet(lease['job']['id'])
                    packet = json.loads(frozen['packet'])
                    selections = [('relationships', edges['edge-a'], 'claim_relationships', relation_reason)]
                    if review_id:
                        selections.append(('reviews', review_id, 'claim_relationship_reviews', review_reason))
                    for category, selected_id, table, original_reason in selections:
                        received = next(r for r in packet['untrusted'][category] if r['id'] == selected_id)
                        with self.store.connect() as db:
                            original = dict(db.execute('SELECT * FROM ' + table + ' WHERE id=?', (selected_id,)).fetchone())
                        self.assertEqual(original['reason'], original_reason)
                        for key in original.keys() - {'reason'}:
                            self.assertEqual(received[key], original[key], key)
                        self.assertTrue(received['reason'].startswith(original_reason.split('λ')[0]))
                        self.assertIn('λ', received['reason'])
                        self.assertTrue(received['reason'].endswith(' [truncated]'))
                        self.assertLessEqual(len(encoded_bytes(received['reason'])), 416)
                        self.assertLessEqual(len(encoded_bytes(packet['untrusted'][category])), CATEGORY_BYTES[category])
                        self.assertIn(selected_id, frozen['source_ids'])
                    self.assertEqual(json.loads(lease['job']['messages'][-1]['content'].split('\n')[-1]), packet)
                    self.assertLessEqual(frozen['budget']['packet_bytes'], 6144)
                    self.assertLessEqual(frozen['budget']['admission_upper_bound'], 8192)

                assert_received(investigator)
                self.complete(investigator, dict(graph_schema='solvenet.graph.v1', reviews=[dict(
                    key='challenge', relationship=edges['edge-a'], status='challenged', reason=review_reason)]))
                critic, choice = self.next(group)
                self.assertEqual((choice['action'], choice['claim_id']), ('critique', claims['a']))
                review_id = self.store.job_context_packet(critic['job']['id'])['request']['review_ids'][0]
                assert_received(critic, review_id)
                self.complete(critic, dict(graph_schema='solvenet.graph.v1', reviews=[]))
                self.store.stop_frontier(group, 'test_done')

    def test_empty_plan_rejected_target_replans_once_with_durable_diagnostics(self):
        group = self.start(graph_limits={'retries': 0})
        initial, _ = self.next(group)
        self.complete(initial, dict(graph_schema='solvenet.graph.v1', claims=[]))
        target, choice = self.next(group)
        self.assertEqual(choice['action'], 'synthesize')
        self.complete(target, 'exact True.intro')
        self.coordinator.tick()
        attempt = self.store.run(self.store.group(group)['run']['run_id'])['attempts'][-1]
        self.assertEqual(attempt['verification_status'], 'rejected')
        self.store = Store(self.path, lease_seconds=5, clock=lambda: self.now[0])
        self.coordinator = Coordinator(self.store, self.verifier)
        replan, choice = self.next(group)
        self.assertEqual(choice['action'], 'plan')
        frozen = self.store.job_context_packet(replan['job']['id'])
        verdict = json.loads(frozen['packet'])['untrusted']['target_verdicts'][0]
        self.assertEqual(verdict['attempt_id'], attempt['id'])
        self.assertEqual(verdict['job_id'], target['job']['id'])
        self.assertEqual(verdict['status'], 'rejected')
        self.assertIn('type mismatch', verdict['diagnostics'])
        self.assertIn(verdict['diagnostics'], attempt['diagnostics'])
        self.assertEqual(frozen['request']['target_attempt_ids'], [attempt['id']])
        self.assertLessEqual(frozen['budget']['packet_bytes'], 6144)
        # An empty replan adds no evidence. The same verdict cannot buy a third plan.
        self.complete(replan, dict(graph_schema='solvenet.graph.v1', claims=[]))
        self.store = Store(self.path, lease_seconds=5, clock=lambda: self.now[0])
        self.coordinator = Coordinator(self.store, self.verifier)
        for _ in range(3):
            self.coordinator.tick()
        self.assertEqual(self.store.group_loop(group)['reason'], 'no_useful_frontier')
        self.assertEqual(len([d for d in self.store.frontier_trace(group)['decisions'] if d['action'] == 'plan']), 2)
        self.assertEqual(self.store.job_context_packet(replan['job']['id']), frozen)

    def test_target_replanning_capacity_can_publish_new_decomposition_and_respects_cap(self):
        for cap in (1, 4):
            group = self.start('target-cap-' + str(cap), graph_limits={'planning_calls': cap, 'retries': 0})
            initial, _ = self.next(group)
            self.complete(initial, dict(graph_schema='solvenet.graph.v1', claims=[]))
            target, _ = self.next(group)
            self.complete(target, 'exact True.intro')
            self.coordinator.tick()
            if cap == 1:
                self.coordinator.tick()
                self.assertEqual(self.store.group_loop(group)['phase'], 'stopped')
                self.assertEqual(self.store.frontier_trace(group)['model']['reserved_planning_critique_assignments'], 1)
                continue
            replan, choice = self.next(group)
            self.assertEqual(choice['action'], 'plan')
            root = json.loads(self.store.job_context_packet(replan['job']['id'])['packet'])['focus']['id']
            self.complete(replan, dict(graph_schema='solvenet.graph.v1', claims=[dict(key='new',
                statement=': True', imports=['Init'], environment='lean-test')], relationships=[dict(
                key='new-edge', **{'from': root, 'to': '$new'}, kind='suggests_using')]))
            investigator, choice = self.next(group)
            self.assertEqual(choice['action'], 'investigate')
            self.assertNotEqual(choice['claim_id'], root)
            self.assertEqual(self.store.frontier_trace(group)['model']['reserved_planning_critique_assignments'], 4)
            # Finish the active group so the next loop iteration has no competing job.
            self.complete(investigator, 'No further evidence')
            self.store.stop_frontier(group, 'test_done')

    def test_successful_target_finalizes_linked_task_and_decision_across_restart(self):
        group, _, winning, target = self.scenario('verified')
        task = next(t for t in self.store.group(group)['tasks'] if t['id'] == winning['task_id'])
        self.assertNotEqual(task['request_key'], 'synthesize')
        self.assertEqual(task['status'], 'open')
        self.assertEqual(winning['processed'], 0)
        self.complete(target, 'exact ⟨True.intro, True.intro, True.intro⟩')
        self.coordinator.tick()

        def assert_finalized():
            snapshot = self.store.group(group)
            task = next(t for t in snapshot['tasks'] if t['id'] == winning['task_id'])
            decision = next(d for d in snapshot['frontier']['decisions'] if d['job_id'] == target['job']['id'])
            self.assertEqual(task['status'], 'done')
            self.assertEqual(decision['processed'], 1)
            self.assertEqual(self.store.group_loop(group)['reason'], 'verified_target')
            return snapshot['frontier']

        before = assert_finalized()
        self.store = Store(self.path, lease_seconds=5, clock=lambda: self.now[0])
        self.coordinator = Coordinator(self.store, self.verifier)
        self.assertFalse(self.coordinator.tick())
        self.assertEqual(assert_finalized(), before)
        self.complete(target, 'exact ⟨True.intro, True.intro, True.intro⟩')
        self.assertEqual(assert_finalized(), before)

    def test_any_claim_composition_actual_use_target_lineage_and_restart(self):
        group, claims, _, target = self.scenario('verified')
        proof_a = self.store.group(group)['artifacts'][0]['id']
        agent_ids = [a['id'] for a in self.store.group(group)['agents']]
        # Two direct attempts are finite; their rejection opens the remaining frontier.
        self.complete(target, 'exact True.intro')
        retry, retry_choice = self.next(group)
        self.assertEqual(retry_choice['action'], 'synthesize')
        self.assertIn('independent retry', retry_choice['reason'])
        self.assertIsNotNone(self.store.group(group)['tasks'][-1]['parent_id'])
        self.complete(retry, 'exact True.intro')
        # B can receive A as selected context independently of the root's suggestions.
        self.store.propose_group_relationship(group, 'b-uses-a', claims['b'], claims['a'], 'suggests_using')
        investigate, choice = self.next(group)
        self.assertEqual(choice['claim_id'], claims['b'])
        packet = self.store.job_context_packet(investigate['job']['id'])
        self.assertEqual(packet['manifest']['selected_proof_ids'], [proof_a])
        self.complete(investigate, self.artifact(claims['b'], ': True ∧ True',
            f'exact ⟨{declaration_name(proof_a)}, {declaration_name(proof_a)}⟩', [proof_a]))
        target, choice = self.next(group)
        proof_b = self.store.group(group)['artifacts'][1]['id']
        self.assertEqual(choice['action'], 'synthesize')
        self.assertEqual(self.store.composed_evidence(proof_b)[-1]['usage']['direct'], [declaration_name(proof_a)])
        before = self.store.job_context_packet(target['job']['id'])
        self.store = Store(self.path, lease_seconds=5, clock=lambda: self.now[0])
        self.coordinator = Coordinator(self.store, self.verifier)
        self.assertEqual([a['id'] for a in self.store.group(group)['agents']], agent_ids)
        self.assertEqual(self.store.job_context_packet(target['job']['id']), before)
        self.complete(target, f'exact ⟨True.intro, {declaration_name(proof_b)}⟩')
        self.coordinator.tick()
        self.assertEqual(self.store.group_loop(group)['reason'], 'verified_target')
        attempt = self.store.run(self.store.group(group)['run']['run_id'])['attempts'][-1]
        evidence = self.store.composed_evidence(attempt['id'])[-1]
        self.assertEqual(evidence['usage']['direct'], [declaration_name(proof_b)])
        self.assertEqual(set(evidence['usage']['transitive']), {declaration_name(proof_a), declaration_name(proof_b)})
        bundle = self.store.export_proof_bundle(attempt['id'])
        result, usage = self.verifier.verify_composed(bundle)
        self.assertTrue(result.verified, result.diagnostics)
        self.assertEqual(usage['subprocesses'], 2)
        cost = self.store.frontier_trace(group)['lean']
        self.assertEqual(cost['operations'], 5)
        self.assertEqual(cost['subprocesses'], 10)
        self.assertEqual(cost['unknown_elapsed'], 0)

    def test_reciprocal_abandoned_does_not_block_direct_target(self):
        group = self.start()
        claims, edges = self.plan(group)
        critic = next(a['id'] for a in self.store.group(group)['agents'] if a['role'] == 'critic')
        root = self.store.group(group)['graph']['root_id']
        self.store.propose_group_relationship(group, 'reciprocal', claims['a'], root, 'suggests_using')
        for name, edge in edges.items():
            self.store.review_group_relationship(group, name, edge, critic, 'abandoned', 'Dead end')
        lease, chosen = self.next(group)
        self.assertEqual(chosen['action'], 'synthesize')
        self.assertIn('advisory', chosen['reason'])
        self.complete(lease, 'exact ⟨True.intro, True.intro, True.intro⟩')
        self.coordinator.tick()
        self.assertEqual(self.store.group_loop(group)['reason'], 'verified_target')

    def test_restart_duplicate_expiry_freezes_context_and_reservation(self):
        group = self.start()
        lease, decision = self.next(group)
        packet = self.store.job_context_packet(lease['job']['id'])
        reserved = self.store.group(group)['remaining_work']
        self.now[0] += 6
        self.store = Store(self.path, lease_seconds=5, clock=lambda: self.now[0])
        self.store.expire()
        self.coordinator = Coordinator(self.store, self.verifier)
        retry = self.store.claim('worker2', ['scripted'], supports_model_respond=True)
        self.assertEqual(retry['job']['id'], lease['job']['id'])
        self.assertEqual(self.store.job_context_packet(retry['job']['id']), packet)
        self.assertEqual(self.store.group(group)['remaining_work'], reserved)
        self.assertEqual(len(self.store.frontier_trace(group)['decisions']), 1)
        self.complete(retry, '{bad JSON}')
        target, choice = self.next(group)
        self.assertEqual(choice['action'], 'synthesize')
        self.assertEqual(len(self.store.group(group)['jobs']), 2)

    def test_operation_cap_rechecks_and_deadline_target_draining(self):
        group = self.start(graph_limits={'verification_operations': 1})
        claims, _ = self.plan(group)
        lease, _ = self.next(group)
        self.complete(lease, self.artifact(claims['a'], ': True', 'exact True.intro'))
        self.coordinator.tick()  # the auxiliary uses the last allowed operation
        reserved = self.store.frontier_trace(group)['model']
        self.coordinator.tick()  # preflight stops before reserving target model work
        self.assertEqual(self.store.group_loop(group)['reason'], 'verification_budget')
        self.assertEqual(self.store.frontier_trace(group)['lean']['operations'], 1)
        self.assertEqual(self.store.frontier_trace(group)['model'], reserved)
        self.assertEqual(len(self.store.group(group)['jobs']), 2)
        self.assertFalse(any(d['action'] == 'synthesize' for d in self.store.frontier_trace(group)['decisions']))

    def while_verification_blocked(self, during):
        """Hold one real check after its durable reservation; run a competing tick."""
        entered, release = threading.Event(), threading.Event()
        original = self.verifier.verify_composed
        failures = []

        def blocked(bundle):
            entered.set()
            if not release.wait(20):
                raise RuntimeError('Test did not release verifier')
            return original(bundle)

        def tick():
            try:
                self.coordinator.tick()
            except BaseException as error:
                failures.append(error)

        with patch.object(self.verifier, 'verify_composed', side_effect=blocked) as verify:
            thread = threading.Thread(target=tick)
            thread.start()
            try:
                self.assertTrue(entered.wait(10), 'Verifier did not acquire ownership')
                # A distinct Store/Coordinator also exercises durable cross-connection ownership.
                other_store = Store(self.path, lease_seconds=5, clock=lambda: self.now[0])
                during(Coordinator(other_store, self.verifier))
            finally:
                release.set()
                thread.join(30)
            self.assertFalse(thread.is_alive(), 'Verifier thread failed to drain')
            self.assertEqual(failures, [])
            self.assertEqual(verify.call_count, 1)

    def test_overlapping_target_ticks_last_slot_skip_busy_and_keep_winner(self):
        group = self.start(graph_limits={'verification_operations': 1})
        plan, _ = self.next(group)
        self.complete(plan, 'No decomposition')
        target, choice = self.next(group)
        self.complete(target, 'exact ⟨True.intro, True.intro, True.intro⟩')
        attempt = self.store.pending()

        def overlapping(other):
            self.assertFalse(other.tick())
            self.assertEqual(other.store.group_loop(group)['phase'], 'frontier')
            self.assertIsNotNone(other.store.pending())
            self.assertEqual(other.store.composed_evidence(attempt['id'])[0]['status'], 'checking')
            self.assertEqual(other.store.frontier_trace(group)['lean']['operations'], 1)
            with other.store.connect() as db:
                self.assertIsNone(db.execute('SELECT * FROM verifications WHERE attempt_id=?', (attempt['id'],)).fetchone())

        self.while_verification_blocked(overlapping)
        self.assertEqual(self.store.group_loop(group)['reason'], 'verified_target')
        evidence = self.store.composed_evidence(attempt['id'])
        self.assertEqual([(e['status'], e['committed']) for e in evidence], [('verified', 1)])
        self.assertEqual(self.store.frontier_trace(group)['lean']['operations'], 1)
        self.assertEqual(len(self.store.group(group)['jobs']), 2)
        winning = next(d for d in self.store.frontier_trace(group)['decisions'] if d['job_id'] == choice['job_id'])
        self.assertEqual(winning['processed'], 1)

    def test_overlapping_auxiliary_ticks_last_slot_no_unverifiable_target_job(self):
        group = self.start(graph_limits={'verification_operations': 1})
        claims, _ = self.plan(group)
        finding, _ = self.next(group)
        self.complete(finding, self.artifact(claims['a'], ': True', 'exact True.intro'))

        def overlapping(other):
            self.assertFalse(other.tick())
            self.assertEqual(other.store.pending_group_artifact()['status'], 'pending')
            self.assertEqual(other.store.group_loop(group)['phase'], 'frontier')
            self.assertEqual(other.store.frontier_trace(group)['lean']['operations'], 1)

        self.while_verification_blocked(overlapping)
        self.assertEqual(self.store.group(group)['artifacts'][0]['status'], 'verified')
        model_cost = self.store.frontier_trace(group)['model']
        self.coordinator.tick()
        self.assertEqual(self.store.group_loop(group)['reason'], 'verification_budget')
        self.assertEqual(self.store.frontier_trace(group)['model'], model_cost)
        self.assertFalse(any(d['action'] == 'synthesize' for d in self.store.frontier_trace(group)['decisions']))

    def test_overlapping_deadline_tick_preserves_reserved_inflight_target(self):
        group = self.start(deadline=101, graph_limits={'verification_operations': 1})
        plan, _ = self.next(group)
        self.complete(plan, 'No decomposition')
        target, _ = self.next(group)
        self.complete(target, 'exact ⟨True.intro, True.intro, True.intro⟩')

        def deadline(other):
            self.now[0] = 102
            self.assertTrue(other.tick())  # stop at deadline, but skip the owned check
            self.assertEqual(other.store.group_loop(group)['reason'], 'deadline')
            self.assertIsNotNone(other.store.pending())
            self.assertEqual(other.store.frontier_trace(group)['lean']['operations'], 1)

        self.while_verification_blocked(deadline)
        self.assertEqual(self.store.group_loop(group)['reason'], 'verified_target')
        self.assertEqual(self.store.frontier_trace(group)['lean']['operations'], 1)
        self.assertEqual(len(self.store.group(group)['jobs']), 2)

    def test_elapsed_preflight_stops_before_new_proof_model_work(self):
        group = self.start(graph_limits={'lean_elapsed_ms': 1})
        claims, _ = self.plan(group)
        finding, _ = self.next(group)
        self.complete(finding, self.artifact(claims['a'], ': True', 'exact True.intro'))
        self.coordinator.tick()
        self.assertEqual(self.store.group(group)['artifacts'][0]['status'], 'verified')
        model_cost = self.store.frontier_trace(group)['model']
        self.assertGreaterEqual(self.store.frontier_trace(group)['lean']['elapsed_ms'], 1)
        self.coordinator.tick()
        self.assertEqual(self.store.group_loop(group)['reason'], 'lean_time_budget')
        self.assertEqual(self.store.frontier_trace(group)['model'], model_cost)
        self.assertFalse(any(d['action'] == 'synthesize' for d in self.store.frontier_trace(group)['decisions']))

    def test_deadline_stops_new_auxiliary_checks_and_target_model_work(self):
        group = self.start(deadline=101)
        claims, _ = self.plan(group)
        finding, _ = self.next(group)
        self.complete(finding, self.artifact(claims['a'], ': True', 'exact True.intro'))
        model_cost = self.store.frontier_trace(group)['model']
        self.now[0] = 102
        with patch.object(self.verifier, 'verify_composed') as verify:
            self.coordinator.tick()
            verify.assert_not_called()
        self.assertEqual(self.store.group_loop(group)['reason'], 'deadline')
        self.assertEqual(self.store.frontier_trace(group)['model'], model_cost)
        self.assertEqual(self.store.frontier_trace(group)['lean']['operations'], 0)
        self.assertFalse(any(d['action'] == 'synthesize' for d in self.store.frontier_trace(group)['decisions']))

    def test_restart_recovers_abandoned_target_with_capacity_and_rejects_old_token(self):
        group = self.start(graph_limits={'verification_operations': 2})
        plan, _ = self.next(group)
        self.complete(plan, 'No decomposition')
        target, _ = self.next(group)
        self.complete(target, 'exact ⟨True.intro, True.intro, True.intro⟩')
        attempt = self.store.pending()
        bundle = attempt['bundle']
        first = self.store.begin_composed_check(attempt['id'], 'attempt', bundle, deadline_ms=1000)
        self.store = Store(self.path, lease_seconds=5, clock=lambda: self.now[0])
        self.store.bind_group_artifact_verifier(self.verifier.artifact_identity())
        with self.assertRaises(VerificationBusy):
            self.store.begin_composed_check(attempt['id'], 'attempt', bundle)
        self.now[0] += 7
        second = self.store.begin_composed_check(attempt['id'], 'attempt', bundle)
        self.assertNotEqual(second, first)
        outcome, usage = self.verifier.verify_composed(bundle)
        self.assertTrue(outcome.verified, outcome.diagnostics)
        self.store.verified(attempt['id'], outcome, bundle=bundle, usage=usage, check_id=first)
        self.assertIsNotNone(self.store.pending())
        self.assertEqual(self.store.group_loop(group)['phase'], 'frontier')
        self.store.verified(attempt['id'], outcome, bundle=bundle, usage=usage, check_id=second)
        self.assertEqual(self.store.group_loop(group)['reason'], 'verified_target')
        cost = self.store.frontier_trace(group)['lean']
        self.assertEqual((cost['operations'], cost['unknown_elapsed']), (2, 1))
        self.assertGreaterEqual(cost['elapsed_ms'], 1000)
        evidence = self.store.composed_evidence(attempt['id'])
        self.assertEqual([(e['status'], e['committed']) for e in evidence], [('abandoned', 0), ('verified', 1)])

    def test_restart_abandoned_last_slot_stops_without_fake_rejection(self):
        group = self.start(graph_limits={'verification_operations': 1})
        plan, _ = self.next(group)
        self.complete(plan, 'No decomposition')
        target, _ = self.next(group)
        self.complete(target, 'exact ⟨True.intro, True.intro, True.intro⟩')
        attempt = self.store.pending()
        self.store.begin_composed_check(attempt['id'], 'attempt', attempt['bundle'], deadline_ms=1000)
        self.now[0] += 7
        self.store = Store(self.path, lease_seconds=5, clock=lambda: self.now[0])
        self.coordinator = Coordinator(self.store, self.verifier)
        with patch.object(self.verifier, 'verify_composed') as verify:
            self.coordinator.tick()
            verify.assert_not_called()
        self.assertEqual(self.store.group_loop(group)['reason'], 'verification_budget')
        self.assertIsNone(self.store.pending())
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM verifications WHERE attempt_id=?', (attempt['id'],)).fetchone())
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?', (target['job']['id'],)).fetchone()[0], 'cancelled')
        self.assertEqual(self.store.composed_evidence(attempt['id'])[0]['status'], 'abandoned')
        self.assertEqual(self.store.frontier_trace(group)['lean']['unknown_elapsed'], 1)

    def test_unknown_time_restart_reservation_and_cumulative_cap(self):
        group = self.start(graph_limits={'lean_elapsed_ms': 30000})
        claims, _ = self.plan(group)
        lease, _ = self.next(group)
        self.complete(lease, self.artifact(claims['a'], ': True', 'exact True.intro'))
        artifact = self.store.pending_group_artifact()
        binding = self.store.bind_group_artifact_verifier(self.verifier.artifact_identity())
        bundle = self.store.ensure_artifact_context(artifact['id'], binding)
        self.store.begin_composed_check(artifact['id'], 'artifact', bundle, deadline_ms=30000)
        # Simulate process death before a receipt: reservation survives and costs one deadline.
        self.store = Store(self.path, clock=lambda: self.now[0])
        self.store.bind_group_artifact_verifier(self.verifier.artifact_identity())
        with self.assertRaises(VerificationBusy):
            self.store.begin_composed_check(artifact['id'], 'artifact', bundle)
        self.now[0] += 36  # bounded restart recovery after the verifier deadline + commit grace
        with self.assertRaisesRegex(FrontierLimit, 'lean_time_budget'):
            self.store.begin_composed_check(artifact['id'], 'artifact', bundle)
        cost = self.store.frontier_trace(group)['lean']
        self.assertEqual((cost['operations'], cost['elapsed_ms'], cost['unknown_elapsed']), (1, 30000, 1))
        self.assertEqual(self.store.composed_evidence(artifact['id'])[-1]['status'], 'abandoned')

    def test_deadline_drains_one_dispatched_target_and_no_auxiliary(self):
        group = self.start(deadline=101)
        plan, _ = self.next(group)
        self.complete(plan, 'No useful decomposition')
        target, _ = self.next(group)
        self.now[0] = 102
        self.complete(target, 'exact ⟨True.intro, True.intro, True.intro⟩')
        self.coordinator.tick()
        self.assertEqual(self.store.group_loop(group)['reason'], 'verified_target')
        self.assertEqual(self.store.frontier_trace(group)['lean']['operations'], 1)

    def test_unavailable_rejected_plan_and_small_planning_cap(self):
        group = self.start(graph_limits={'planning_calls': 1}, model_capabilities={'scripted': {'tasks': ['proof']}})
        self.coordinator.tick()
        self.assertEqual(self.store.group_loop(group)['reason'], 'capacity_or_model_budget')
        self.assertEqual(len(self.store.group(group)['jobs']), 0)
        self.assertTrue(self.store.frontier_trace(group)['decisions'][0]['deferred'])
        with self.assertRaises(ValueError):
            self.start('bad', graph_limits={'verification_operations': 25})
        with self.assertRaises(Conflict):
            self.start(graph_limits={'planning_calls': 2})

    def test_failed_verifier_cost_is_unknown_and_recheck_is_reserved(self):
        group = self.start()
        claims, _ = self.plan(group)
        lease, _ = self.next(group)
        self.complete(lease, self.artifact(claims['a'], ': True', 'exact True.intro'))
        with patch.object(self.verifier, 'verify_composed', side_effect=RuntimeError('injected')):
            self.coordinator.tick()
        cost = self.store.frontier_trace(group)['lean']
        self.assertEqual(cost['operations'], 1)
        self.assertEqual(cost['unknown_elapsed'], 1)
        self.assertGreater(cost['elapsed_ms'], 0)

    def test_model_failure_redirects_with_same_agents_and_bounded_calls(self):
        self.models['investigator'] = ['first', 'scripted']
        group = self.start()
        claims, _ = self.plan(group)
        # Advertise equal fit/presence; configuration order deterministically picks first.
        self.store.claim('first-worker', ['first'], supports_model_respond=True)
        self.store.claim('worker', ['scripted'], supports_model_respond=True)
        self.coordinator.tick()
        lease = self.store.claim('first-worker', ['first'], supports_model_respond=True)
        self.assertIsNotNone(lease)
        owner = next(j['agent_id'] for j in self.store.group(group)['jobs'] if j['job_id'] == lease['job']['id'])
        self.store.result(lease['assignment_id'], dict(lease_token=lease['lease_token'], status='failed',
            error='unreachable', failure_class='permanent'))
        self.store.claim('first-worker', ['first'], supports_model_respond=True,
                         provider_health={'status': 'unavailable', 'reason': 'offline'})
        retry, choice = self.next(group)
        self.assertEqual(retry['job']['model'], 'scripted')
        self.assertEqual(choice['claim_id'], claims['a'])
        self.assertIn('independent retry', choice['reason'])
        self.assertEqual(len(self.store.group(group)['agents']), 5)
        self.assertNotEqual(owner, next(j['agent_id'] for j in self.store.group(group)['jobs'] if j['job_id'] == retry['job']['id']))
        self.assertLessEqual(self.store.frontier_trace(group)['model']['reserved_assignments'], 64)

    def test_rebind_recheck_and_superseded_result_consume_slots(self):
        group = self.start(graph_limits={'verification_operations': 2})
        claims, _ = self.plan(group)
        lease, _ = self.next(group)
        self.complete(lease, self.artifact(claims['a'], ': True', 'exact True.intro'))
        artifact = self.store.pending_group_artifact()
        binding = self.store.bind_group_artifact_verifier(self.verifier.artifact_identity())
        bundle = self.store.ensure_artifact_context(artifact['id'], binding)
        check = self.store.begin_composed_check(artifact['id'], 'artifact', bundle)
        self.store.bind_group_artifact_verifier('test:changed')
        outcome = VerificationResult(VerificationStatus.VERIFIED, '', 7)
        self.store.checked_group_artifact(artifact['id'], 'verified', binding=binding, bundle=bundle,
            result=outcome, usage={'status': 'usage_unknown', 'subprocesses': 2}, check_id=check)
        self.assertEqual(self.store.pending_group_artifact()['status'], 'pending')
        self.assertFalse(self.store.composed_evidence(artifact['id'])[-1]['committed'])
        binding = self.store.bind_group_artifact_verifier(self.verifier.artifact_identity())
        self.coordinator.tick()
        self.assertEqual(self.store.frontier_trace(group)['lean']['operations'], 2)
        bundle = self.store.ensure_artifact_context(artifact['id'], binding)
        with self.assertRaises(VerificationBusy):
            self.store.begin_composed_check(artifact['id'], 'artifact', bundle)
        binding = self.store.bind_group_artifact_verifier('test:another-checker')
        bundle = self.store.ensure_artifact_context(artifact['id'], binding)
        with self.assertRaisesRegex(FrontierLimit, 'verification_budget'):
            self.store.begin_composed_check(artifact['id'], 'artifact', bundle)

    def test_help_requests_are_attributed_atomic_and_planning_cap_counts_retries(self):
        group = self.start(graph_limits={'planning_calls': 1})
        claims, edges = self.plan(group)
        self.assertEqual(self.store.frontier_trace(group)['model']['reserved_planning_critique_assignments'], 1)
        lease, _ = self.next(group)
        self.complete(lease, dict(graph_schema='solvenet.graph.v1', help_requests=[dict(
            key='help', claim=claims['b'], action='critique', priority=3, reason='Need an independent opinion')],
            reviews=[dict(key='challenge', relationship=edges['edge-a'], status='challenged')]))
        next_lease, choice = self.next(group)
        self.assertNotEqual(choice['action'], 'critique')
        self.assertIn('planning_critique_cap', {d['reason'] for d in choice['deferred']})
        self.assertLessEqual(self.store.frontier_trace(group)['model']['reserved_planning_critique_assignments'], 1)
        with self.store.connect() as db:
            proposal = db.execute('SELECT * FROM graph_action_proposals WHERE action="critique"').fetchone()
        self.assertEqual(proposal['job_id'], lease['job']['id'])

    def test_smaller_packet_closure_source_limits_and_no_useful_frontier(self):
        group = self.start(graph_limits={'included_lemmas': 1, 'source_bytes': 1024, 'packet_bytes': 2048, 'retries': 0})
        plan, _ = self.next(group)
        self.complete(plan, 'No decomposition')
        target, choice = self.next(group)
        packet = self.store.job_context_packet(target['job']['id'])
        self.assertLessEqual(packet['budget']['packet_bytes'], 2048)
        self.assertEqual(packet['manifest']['source_byte_limit'], 1024)
        self.complete(target, 'exact ⟨True.intro, True.intro, True.intro⟩')
        self.coordinator.tick()  # generated Lean source exceeds the smaller cap before subprocesses
        self.assertEqual(self.store.frontier_trace(group)['lean']['operations'], 1)
        self.assertEqual(self.store.frontier_trace(group)['lean']['subprocesses'], 0)
        self.coordinator.tick()
        replan, choice = self.next(group)
        self.assertEqual(choice['action'], 'plan')
        self.assertLessEqual(self.store.job_context_packet(replan['job']['id'])['budget']['packet_bytes'], 2048)
        self.complete(replan, dict(graph_schema='solvenet.graph.v1', claims=[]))
        self.coordinator.tick()
        self.assertIn(self.store.group_loop(group)['reason'], ('no_useful_frontier', 'capacity_or_model_budget'))
