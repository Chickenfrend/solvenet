"""G3 acceptance checks against the pinned Lean 4.19 toolchain."""

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solvenet.composed import declaration_name, validate_bundle
from solvenet.graph_response import SCHEMA as GRAPH_SCHEMA
from solvenet.proof_context import selected_bundle
from solvenet.server import Coordinator
from solvenet.sandbox import ContainerVerifier
from solvenet.store import Conflict, Store
from solvenet.verifier import LeanVerifier, VerificationStatus

ROOT = Path(__file__).resolve().parents[2]


class ComposedTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'state.db'
        self.store = Store(self.path)
        self.verifier = LeanVerifier(ROOT / 'lean')
        self.binding = self.store.bind_group_artifact_verifier(self.verifier.artifact_identity())
        self.coordinator = Coordinator(self.store, self.verifier)
        self.group = self.store.create_group('g', ': (True ∧ True) ∧ True', ['Init'], 'pinned')
        self.agent = self.store.add_agent(self.group, 'a', 'investigator')
        self.task = self.store.add_group_task(self.group, 't', self.agent, self.agent, 'Prove lemma', 8)

    def propose(self, key, statement, proof, prerequisites=None, **options):
        return self.store.propose_group_artifact(self.group, key, self.agent, self.task,
            statement, options.get('imports', ['Init']), options.get('environment', 'pinned'), proof,
            prerequisite_proof_ids=prerequisites)

    def checked(self, key, statement, proof, prerequisites=None):
        artifact = self.propose(key, statement, proof, prerequisites)
        self.assertTrue(self.coordinator.tick())
        status = next(a for a in self.store.group(self.group)['artifacts'] if a['id'] == artifact)
        self.assertEqual(status['status'], 'verified', status['diagnostics'])
        return artifact

    def chain(self):
        a = self.checked('A', ': True', 'exact True.intro')
        b = self.checked('B', ': True ∧ True',
                         f'exact ⟨{declaration_name(a)}, {declaration_name(a)}⟩', [a])
        return a, b

    def target_bundle(self, prerequisites, proof):
        with self.store.connect() as db:
            root = db.execute('SELECT root_id FROM group_graphs WHERE group_id=?', (self.group,)).fetchone()[0]
            return selected_bundle(db, self.group, root, proof, prerequisites)

    def test_chain_actual_use_alternative_proofs_and_restart_replay(self):
        a, b = self.chain()
        standalone = self.checked('B2', ': True ∧ True', 'exact ⟨True.intro, True.intro⟩', [a])
        used = self.store.composed_evidence(b)[0]['usage']
        self.assertEqual(used['direct'], [declaration_name(a)])
        self.assertEqual(self.store.composed_evidence(standalone)[0]['usage']['transitive'], [])
        bundle = self.target_bundle([b, standalone], f'exact ⟨{declaration_name(b)}, True.intro⟩')
        result, usage = self.verifier.verify_composed(bundle)
        self.assertTrue(result.verified, result.diagnostics)
        self.assertEqual(usage['direct'], [declaration_name(b)])
        self.assertEqual(usage['transitive'], [declaration_name(a), declaration_name(b)])
        self.assertEqual(usage['type'], [])
        self.assertFalse(self.verifier.verify(bundle['statement'], bundle['proof']).verified)
        replay = Store(self.path).export_proof_bundle(b)
        self.assertEqual(replay, self.store.export_proof_bundle(b))
        # Replay is self-contained: no mutable graph reads, even if artifacts disappear.
        with patch.object(self.store, 'connect', side_effect=AssertionError('Graph consultation')):
            result, replay_usage = self.verifier.verify_composed(json.loads(json.dumps(replay)))
        self.assertTrue(result.verified, result.diagnostics)
        self.assertEqual(replay_usage['direct'], [declaration_name(a)])
        self.assertIsNone(self.store.group(self.group)['run'])

    def test_invalid_unused_lemmas_expected_axioms_and_renames_rejected(self):
        a, b = self.chain()
        base = self.target_bundle([b], 'exact ⟨⟨True.intro, True.intro⟩, True.intro⟩')
        cases = []
        for bad_proof in ('sorry', 'exact SolveNetExpected_0', 'exact False.elim (by sorry)',
                          'exact True.intro\naxiom arbitrary : False',
                          'exact True.intro\ntheorem SolveNetCandidate : False := by sorry'):
            bundle = copy.deepcopy(base)
            bundle['declarations'][0]['proof'] = bad_proof
            cases.append(bundle)
        false = copy.deepcopy(base)
        false['declarations'][0]['statement'] = ': False'
        cases.append(false)
        target_ax = copy.deepcopy(base)
        target_ax['proof'] = 'exact SolveNetExpected_2'
        cases.append(target_ax)
        renamed = copy.deepcopy(base)
        renamed['declarations'][0]['name'] += '_renamed'
        cases.append(renamed)
        collision = copy.deepcopy(base)
        collision['declarations'][1]['name'] = collision['declarations'][0]['name']
        cases.append(collision)
        cycle = copy.deepcopy(base)
        cycle['declarations'][0]['prerequisite_ids'] = [b]
        cases.append(cycle)
        for bundle in cases:
            with self.subTest(bundle=bundle):
                result, _ = self.verifier.verify_composed(bundle)
                self.assertFalse(result.verified, result.diagnostics)
        permitted = LeanVerifier(ROOT / 'lean', allowed_axioms=frozenset(
            {'sorryAx', 'SolveNetExpected_0', 'SolveNetExpected_2'}))
        for bundle in (cases[0], cases[1], target_ax):
            bundle = bundle | {'verifier_identity': permitted.artifact_identity()}
            result, _ = permitted.verify_composed(bundle)
            self.assertFalse(result.verified, result.diagnostics)

    def test_selection_rejects_unverified_edited_import_environment_and_limits(self):
        a, b = self.chain()
        pending = self.propose('pending', ': False', 'trivial')
        with self.assertRaises(Conflict):
            self.target_bundle([pending], 'trivial')
        with self.assertRaises((ValueError, Conflict)):
            self.target_bundle([a] * 9, 'trivial')
        for column, value in (('proof', 'sorry'), ('imports', '["Lean"]'), ('environment', 'other')):
            with self.store.transaction() as db:
                old = db.execute(f'SELECT {column} FROM group_artifacts WHERE id=?', (a,)).fetchone()[0]
                db.execute(f'UPDATE group_artifacts SET {column}=? WHERE id=?', (value, a))
            with self.subTest(column=column), self.assertRaises(Conflict):
                self.target_bundle([b], 'trivial')
            with self.store.transaction() as db:
                db.execute(f'UPDATE group_artifacts SET {column}=? WHERE id=?', (old, a))
        stale = self.target_bundle([b], 'trivial') | {'verifier_identity': 'local:changed'}
        self.assertFalse(self.verifier.verify_composed(stale)[0].verified)
        oversized = self.target_bundle([b], 'trivial') | {'proof': '--' + 'x' * 32768}
        with self.assertRaises(ValueError):
            validate_bundle(oversized)

    def test_binding_aba_discards_check_but_unrelated_graph_update_does_not(self):
        a = self.checked('A', ': True', 'trivial')
        b = self.propose('B', ': True ∧ True', f'exact ⟨{declaration_name(a)}, True.intro⟩', [a])
        original = self.verifier.verify_composed

        def aba(bundle):
            result = original(bundle)
            other = Store(self.path)
            other.bind_group_artifact_verifier('local:other')
            other.bind_group_artifact_verifier(bundle['verifier_identity'])
            return result

        with patch.object(self.verifier, 'verify_composed', side_effect=aba):
            self.coordinator.tick()
        self.assertEqual(self.store.composed_evidence(b)[0]['committed'], 0)
        self.assertEqual(next(x for x in self.store.group(self.group)['artifacts'] if x['id'] == b)['status'], 'pending')
        self.coordinator.tick()  # A's fresh standalone check

        def graph_update(bundle):
            self.store.propose_group_claim(self.group, 'unrelated', ': True → True', ['Init'], 'pinned',
                                           reason='Unrelated immutable input')
            return original(bundle)

        with patch.object(self.verifier, 'verify_composed', side_effect=graph_update):
            self.coordinator.tick()
        self.assertEqual(self.store.composed_evidence(b)[-1]['committed'], 1)
        self.assertTrue(self.store.export_proof_bundle(b)['verifier_revision'] > self.binding[1])

    def test_composed_timeout_and_bounded_diagnostics(self):
        a, b = self.chain()
        bundle = self.target_bundle([b], 'trivial')
        timeout = LeanVerifier(ROOT / 'lean', timeout_seconds=0.01)
        self.assertEqual(timeout.verify_composed(bundle)[0].status, VerificationStatus.TIMEOUT)
        flood = bundle | {'proof': 'fail "' + 'x' * 5000 + '"'}
        bounded = LeanVerifier(ROOT / 'lean', max_diagnostics_bytes=512)
        result, _ = bounded.verify_composed(flood)
        self.assertFalse(result.verified)
        self.assertLess(len(result.diagnostics.encode()), 600)

    def test_type_dependencies_are_reported_and_traversed(self):
        a = self.checked('A', ': True', 'trivial')
        b = self.checked('typed-B', f': {declaration_name(a)} = {declaration_name(a)}', 'rfl', [a])
        c = self.checked('typed-C', f': {declaration_name(b)} = {declaration_name(b)}', 'rfl', [b])
        usage = self.store.composed_evidence(c)[0]['usage']
        # Eq's elaborated implicit type contains A, as well as the explicit B terms.
        self.assertEqual(usage['type'], [declaration_name(a), declaration_name(b)])
        self.assertEqual(usage['transitive'], [declaration_name(a), declaration_name(b)])

    def test_docker_composed_request_pins_image_and_retains_bounded_usage(self):
        a, b = self.chain()
        image = 'sha256:' + 'a' * 64
        bundle = self.target_bundle([b], 'trivial') | {'verifier_identity': 'docker:' + image}
        expected_usage = dict(status='known', direct=[], type=[], transitive=[])

        def execute(command, timeout):
            self.assertEqual(command[-1], image)
            mount = command[command.index('--mount') + 1]
            directory = Path(mount.removeprefix('type=bind,src=').removesuffix(',dst=/work'))
            request = json.loads((directory / 'request.json').read_text())
            self.assertEqual(request['bundle'], bundle)
            (directory / 'result.json').write_text(json.dumps(dict(status='verified', diagnostics='', elapsed_ms=1)))
            (directory / 'usage.json').write_text(json.dumps(expected_usage))
            return subprocess.CompletedProcess(command, 0, stderr=b'')

        with patch('solvenet.sandbox._run_docker', side_effect=execute), \
             patch.object(ContainerVerifier, '_remove'):
            result, usage = ContainerVerifier(image='mutable:tag').verify_composed(bundle)
        self.assertTrue(result.verified, result.diagnostics)
        self.assertEqual(usage, expected_usage)


class TargetContextTests(unittest.TestCase):
    def test_finding_chain_fixed_loop_target_attempt_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            store = Store(path)
            verifier = LeanVerifier(ROOT / 'lean')
            coordinator = Coordinator(store, verifier)
            group = store.start_group_loop('chain', ': (True ∧ True) ∧ True', ['Init'], 'pinned',
                {r: 'scripted' for r in ('planner', 'investigator', 'critic', 'synthesizer')})

            def drive(key, output):
                for _ in range(5):
                    store.advance_group(group)
                lease = store.claim('worker', ['scripted'], supports_model_respond=True)
                self.assertIsNotNone(lease, key)
                job = lease['job']
                store.result(lease['assignment_id'], dict(lease_token=lease['lease_token'],
                    status='completed', output={'text': output, **(
                        {'type': job['task_type']} if job['kind'] == 'model.respond' else {})}))
                return job

            drive('plan', json.dumps({'approaches': ['A', 'B']}))
            drive('A', json.dumps({'artifact': dict(statement=': True', imports=['Init'],
                                                    environment='pinned', proof='trivial')}))
            store.advance_group(group)
            coordinator.tick()
            a = store.group(group)['artifacts'][0]['id']
            # B is a model.respond finding, with exact completed-job provenance.
            drive('B', json.dumps(dict(graph_schema=GRAPH_SCHEMA,
                claims=[dict(key='b', statement=': True ∧ True', imports=['Init'], environment='pinned')],
                artifacts=[dict(key='proof', claim='$b', statement=': True ∧ True', imports=['Init'],
                    environment='pinned', proof=f'exact ⟨{declaration_name(a)}, True.intro⟩',
                    prerequisite_proof_ids=[a])])))
            store.advance_group(group)
            coordinator.tick()
            b = store.group(group)['artifacts'][1]['id']
            run_id = store.group(group)['run']['run_id']
            self.assertNotEqual(store.run_status(run_id)['status'], 'solved')
            drive('review', json.dumps({'decisions': ['accept', 'redirect']}))
            drive('redirect', 'Use B')
            job = drive('target', f'exact ⟨{declaration_name(b)}, True.intro⟩')
            context = store.proof_context(job['id'])
            self.assertEqual(context['proof'], '')
            self.assertEqual([x['proof_id'] for x in context['declarations']], [a, b])
            self.assertIn(declaration_name(b), job['messages'][0]['content'])
            attempt = store.pending()
            self.assertEqual(attempt['bundle']['claim_id'], context['claim_id'])
            changed = attempt['bundle'] | {'proof': 'trivial'}
            outcome, changed_usage = verifier.verify_composed(changed)
            self.assertTrue(outcome.verified)
            store.verified(attempt['id'], outcome, bundle=changed, usage=changed_usage)
            self.assertNotEqual(store.run_status(run_id)['status'], 'solved')
            self.assertEqual(store.composed_evidence(attempt['id'])[-1]['committed'], 0)
            # Restart before Lean; tick must use persisted declarations, not live graph selection.
            reopened = Store(path)
            self.assertTrue(Coordinator(reopened, verifier).tick())
            self.assertEqual(reopened.run_status(run_id)['status'], 'solved')
            evidence = reopened.composed_evidence(attempt['id'])[-1]
            self.assertEqual(evidence['usage']['direct'], [declaration_name(b)])
            self.assertEqual(evidence['usage']['transitive'], [declaration_name(a), declaration_name(b)])
            replay = reopened.export_proof_bundle(attempt['id'])
            self.assertEqual(replay['proof'], attempt['candidate'])
            self.assertTrue(verifier.verify_composed(replay)[0].verified)
            self.assertEqual(reopened.run(run_id)['attempts'][0]['verification_status'], 'verified')
