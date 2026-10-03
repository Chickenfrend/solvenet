"""G6: frozen graph packets across HTTP, independent Go processes and real Lean."""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

from solvenet.server import Coordinator, make_server
from solvenet.store import Store
from solvenet.verifier import LeanVerifier
from solvenet.composed import declaration_name
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_group_collaboration as fixed
from test_group_collaboration import api
from test_site_worker_lean import ROOT, serving

MODEL = 'ollama/graph-fixture:latest'
FIXTURE = json.loads((ROOT / 'integration/fixtures/graph-nat-reorder.json').read_text())
ENVIRONMENT = FIXTURE['environment']
TARGET = FIXTURE['statement']
A, B, C = (FIXTURE[k] for k in ('a', 'b', 'c'))


class GraphCollaborationTests(unittest.TestCase):
    setUpClass = classmethod(fixed.GroupCollaborationTests.setUpClass.__func__)
    tearDownClass = classmethod(fixed.GroupCollaborationTests.tearDownClass.__func__)

    def scenario(self, rejected=False):
        with tempfile.TemporaryDirectory(prefix='solvenet-g6-') as directory:
            path = Path(directory) / 'state.db'
            verifier = LeanVerifier(ROOT / 'lean')
            coordinator = Coordinator(Store(path), verifier)
            observed, errors, packets = [], [], {}
            proofs = {}
            test = self

            class Ollama(BaseHTTPRequestHandler):
                def reply(self, value):
                    body = json.dumps(value).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self):
                    self.reply({'models': [{'name': 'graph-fixture:latest'}]})

                def do_POST(self):
                    request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    if self.path == '/api/show':
                        self.reply({})
                        return
                    try:
                        prompt = request['messages'][-1]['content']
                        packet = json.loads(prompt.split('\n')[-1])
                        action = prompt.split('Frontier action: ')[1].split('.')[0]
                        focus = packet['focus']
                        lemmas = {x['statement']: x for x in packet['checked_lemmas']}
                        test.assertEqual(packet, json.loads(current['packet']))
                        test.assertEqual(request['messages'][-1], current['messages'][-1])
                        if 'b' in proofs:
                            target_proof = FIXTURE['proof_target'].format(b_name=declaration_name(proofs['b']))
                            test.assertTrue(all(target_proof not in m['content'] for m in request['messages']))
                        test.assertLessEqual(len(json.dumps(request['messages']).encode()) +
                            request['options']['num_predict'] + 512, request['options']['num_ctx'])
                        if action == 'plan':
                            value = dict(graph_schema='solvenet.graph.v1', claims=[
                                dict(key=k, statement=s, imports=['Init'], environment=ENVIRONMENT)
                                for k, s in [('a', A), ('b', B), ('c', C)]],
                                relationships=[dict(key='edge-' + k, **{'from': focus['id'], 'to': '$' + k},
                                    kind='suggests_using') for k in ('a', 'b', 'c')] +
                                    [dict(key='b-a', **{'from': '$b', 'to': '$a'}, kind='suggests_using')],
                                priorities=[dict(key='priority-' + k, claim='$' + k, priority=p)
                                    for k, p in [('a', 3), ('b', 2), ('c', 1)]])
                        elif action == 'critique':
                            review = next(r for r in packet['untrusted']['reviews']
                                if r['status'] == 'challenged' and
                                r['reason'] == 'This alternative does not advance composition')
                            edge = next(r['id'] for r in packet['untrusted']['relationships']
                                if r['id'] == review['relationship_id'] and r['to_id'] == focus['id'])
                            test.assertEqual(current['request']['review_ids'], [review['id']])
                            value = dict(graph_schema='solvenet.graph.v1', reviews=[dict(key='redirect',
                                relationship=edge, status='promising', reason='Use the checked lemma in composition')])
                        elif focus['statement'] == A:
                            proof = FIXTURE['failed_proof'] if rejected else FIXTURE['proof_a']
                            value = artifact(focus, proof, [])
                            if not rejected:
                                edge = next(r['id'] for r in packet['untrusted']['relationships']
                                    if r['to_id'] == focus['id'] and r['kind'] == 'suggests_using')
                                value['reviews'] = [dict(key='challenge', relationship=edge, status='challenged',
                                    reason='This alternative does not advance composition')]
                        elif focus['statement'] == B:
                            predecessor = lemmas[A]
                            test.assertEqual(predecessor['proof_id'], proofs['a'])
                            test.assertEqual(predecessor['name'], declaration_name(proofs['a']))
                            value = artifact(focus, FIXTURE['proof_b'].format(a_name=predecessor['name']), [predecessor['proof_id']])
                        elif action == 'synthesize':
                            if B in lemmas:
                                predecessor = lemmas[B]
                                test.assertEqual(predecessor['proof_id'], proofs['b'])
                                test.assertEqual(predecessor['name'], declaration_name(proofs['b']))
                                value = FIXTURE['proof_target'].format(b_name=predecessor['name'])
                            else:
                                value = FIXTURE['failed_proof']  # Deliberate dead end opens B's frontier.
                        else:
                            raise AssertionError((action, focus))
                        observed.append((action, focus['statement'], request, value))
                        self.reply({'done': True, 'model': request['model'], 'done_reason': 'stop',
                            'message': {'content': json.dumps({'proof' if action == 'synthesize' else 'text':
                                value if isinstance(value, str) else json.dumps(value)})},
                            'prompt_eval_count': 23, 'eval_count': 11, 'total_duration': 1000000})
                    except Exception as error:
                        errors.append(repr(error))
                        self.send_error(500)

                def log_message(self, *args):
                    pass

            def artifact(focus, proof, prerequisites):
                return dict(graph_schema='solvenet.graph.v1', artifacts=[dict(key='proof',
                    claim=focus['id'], statement=focus['statement'], imports=['Init'],
                    environment=ENVIRONMENT, proof=proof, prerequisite_proof_ids=prerequisites)])

            with serving(make_server(coordinator, ('127.0.0.1', 0))) as url, \
                    serving(ThreadingHTTPServer(('127.0.0.1', 0), Ollama)) as provider:
                created = api(url, '/v1/groups', dict(request_key='g6', mode='graph',
                    statement=TARGET, imports=['Init'], environment=ENVIRONMENT, max_work=24,
                    models={r: MODEL for r in ('planner', 'investigator', 'critic', 'synthesizer')}))
                group_id = created['id']
                claims = {}
                next_action = None
                for step in range(12):
                    for _ in range(8):
                        coordinator.tick()
                        snapshot = api(url, '/v1/groups/' + group_id)
                        decisions = snapshot['group']['frontier']['decisions']
                        if decisions and decisions[-1]['job_id'] not in packets:
                            break
                        if snapshot['loop']['phase'] == 'stopped':
                            break
                    if snapshot['loop']['phase'] == 'stopped':
                        break
                    decision = decisions[-1]
                    if step == 2:
                        next_action = (decision['action'], decision['reason'])
                        self.assertTrue(decision['deferred'])
                        if not rejected:
                            self.assertEqual(decision['action'], 'critique')
                            self.assertEqual(decision['claim_id'], claims['a'])
                            self.assertIn('reviewed challenge', decision['reason'])
                            self.assertTrue(any(d['claim_id'] == claims['b'] and d['action'] == 'investigate'
                                and d['reason'] == 'lower_rank_than_selected' for d in decision['deferred']))
                        if rejected:
                            break
                    if decision['action'] == 'critique' and decision['claim_id'] == claims.get('a') and not rejected:
                        self.assertIn('independent reconsideration of reviewed challenge', decision['reason'])
                        self.assertTrue(any(d['claim_id'] == claims['b'] and
                            d['reason'] == 'lower_rank_than_selected' for d in decision['deferred']))
                    job_id = decision['job_id']
                    current = coordinator.store.job_context_packet(job_id)
                    packets[job_id] = current
                    self.assertEqual(hashlib.sha256(current['packet']).hexdigest(), current['sha256'])
                    self.assertLessEqual(current['budget']['packet_bytes'], 6144)
                    self.assertLessEqual(current['budget']['input_byte_upper_bound'], 8192)
                    self.assertEqual(current['graph_revision'], decision['graph_revision'])
                    for lemma in json.loads(current['packet'])['checked_lemmas']:
                        self.assertEqual((lemma['formal_status'], lemma['current_status']), ('verified', 'eligible'))
                        source = next(a for a in coordinator.store.group(group_id)['artifacts']
                            if a['id'] == lemma['proof_id'])
                        for field in ('agent_id', 'task_id', 'job_id', 'source'):
                            self.assertEqual(lemma['provenance'][field], source[field])
                        self.assertEqual(lemma['name'], declaration_name(source['id']))
                    if decision['claim_id'] == claims.get('b'):
                        coordinator.store = Store(path)
                        coordinator.tick()  # A queued decision survives fresh verifier binding.
                        self.assertEqual(coordinator.store.job_context_packet(job_id), current)
                    process = subprocess.Popen([self.worker, '-once', '-coordinator', url,
                        '-provider', 'ollama', '-model', 'graph-fixture:latest', '-ollama-url', provider,
                        '-ollama-context', '8192', '-id', 'graph-worker-' + str(int(step > 1))],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        env={**os.environ, 'OPENAI_API_KEY': '', 'OPENAI_API_KEY_FILE': ''})
                    try:
                        output, _ = process.communicate(timeout=15)
                        self.assertEqual(process.returncode, 0, output.decode())
                        self.assertEqual(errors, [])
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.communicate(timeout=5)
                    if step == 0:
                        receipt = coordinator.store.ingest_group_graph_response(group_id, job_id)
                        self.assertIn('claims', receipt, (receipt, observed))
                        claims = receipt['claims']
                    for _ in range(2):
                        coordinator.tick()
                    for row in coordinator.store.group(group_id)['artifacts']:
                        proofs['a' if row['statement'] == A else 'b'] = row['id']
                if rejected:
                    self.assertEqual(next_action[0], 'critique')
                    self.assertIn('negative formal', next_action[1])
                    self.assertEqual(snapshot['group']['frontier']['lean']['operations'], 1)
                    self.assertEqual(snapshot['group']['frontier']['lean']['subprocesses'], 2)
                    self.assertEqual(snapshot['group']['frontier']['lean']['unknown_elapsed'], 0)
                    self.assertEqual(snapshot['group']['cost']['leases'], 2)
                    return next_action
                self.assertEqual(snapshot['loop']['reason'], 'verified_target', snapshot)
                group = snapshot['group']
                run = api(url, '/v1/runs/' + group['run']['run_id'])
                self.assertEqual(run['status'], 'solved')
                self.assertIn(('critique', A), [(a, s) for a, s, _, _ in observed])
                self.assertEqual(len(observed), 7)
                self.assertEqual(group['cost']['requests'], 7)
                self.assertEqual(group['cost']['input_tokens'], {'known': 161, 'unknown': 0})
                self.assertEqual(group['cost']['output_tokens'], {'known': 77, 'unknown': 0})
                self.assertEqual(group['cost']['failures'], 0)
                self.assertEqual(group['cost']['leases'], 7)
                self.assertEqual(group['cost']['retries'], 0)
                self.assertEqual(group['cost']['provider_duration_ns'], {'known': 7000000, 'unknown': 0})
                self.assertEqual(group['cost']['lean_checks'], 5)
                self.assertEqual(group['cost']['lean_elapsed_ms']['unknown'], 0)
                self.assertEqual(group['frontier']['model'], dict(reserved_work=14,
                    reserved_assignments=14, reserved_planning_critique_assignments=4))
                self.assertEqual(group['frontier']['lean']['operations'], 5)
                self.assertEqual(group['frontier']['lean']['subprocesses'], 10)
                self.assertEqual(group['frontier']['lean']['unknown_elapsed'], 0)
                self.assertEqual(group['frontier']['lean']['unknown_subprocesses'], 0)
                self.assertLess(group['frontier']['lean']['elapsed_ms'], 180000)
                job_keys = {j['job_id']: j['request_key'] for j in group['jobs']}
                calls = {j: next(c for c in group['calls'] if c['request_key'] == k) for j, k in job_keys.items()}
                for row in group['artifacts']:
                    self.assertEqual(row['agent_id'], calls[row['job_id']]['agent_id'])
                    self.assertEqual(row['task_id'], calls[row['job_id']]['task_id'])
                    self.assertEqual(row['assignment_id'], calls[row['job_id']]['assignment_id'])
                    self.assertEqual(api(url, '/v1/proofs/' + row['id'] + '/bundle'),
                        coordinator.store.export_proof_bundle(row['id']))
                    self.assertTrue(api(url, '/v1/proofs/' + row['id'] + '/evidence'))
                graph = api(url, '/v1/groups/' + group_id + '/graph')
                publications = [p for p in graph['publications'] if p['source'] == 'job']
                self.assertEqual(len(publications), 5)  # Three planner claims plus two proof publications.
                for publication in publications:
                    source = calls[publication['job_id']]
                    for field in ('agent_id', 'task_id', 'assignment_id'):
                        self.assertEqual(publication[field], source[field])
                self.assertEqual([r['status'] for r in graph['reviews']], ['challenged', 'promising'])
                self.assertNotEqual(calls[group['artifacts'][0]['job_id']]['worker_id'],
                    calls[group['artifacts'][1]['job_id']]['worker_id'])
                attempt = run['attempts'][-1]
                evidence = coordinator.store.composed_evidence(attempt['id'])[-1]
                self.assertEqual(evidence['usage']['status'], 'known')
                self.assertEqual(evidence['usage']['direct'], [declaration_name(proofs['b'])])
                self.assertEqual(coordinator.store.composed_evidence(proofs['b'])[-1]['usage']['direct'],
                    [declaration_name(proofs['a'])])
                self.assertEqual(set(evidence['usage']['transitive']),
                    {declaration_name(proofs['a']), declaration_name(proofs['b'])})
                bundle = api(url, '/v1/proofs/' + attempt['id'] + '/bundle')
                for endpoint in ('/v1/proofs/' + '0' * 32 + '/bundle',
                                 '/v1/groups/' + '0' * 32 + '/graph'):
                    with self.assertRaises(HTTPError) as error:
                        api(url, endpoint)
                    self.assertEqual(error.exception.code, 404)
                    error.exception.close()
                self.assertEqual(api(url, '/v1/proofs/' + attempt['id'] + '/evidence')[-1], evidence)
                trace = subprocess.run(['python3', str(ROOT / 'integration/group_trace.py'), group_id,
                    '--coordinator', url], capture_output=True, text=True, timeout=10, check=True).stdout
                self.assertIn('proof-use status=known', trace)
                self.assertIn('graph cost:', trace)
                self.assertIn('planning ', trace)
                self.assertIn('promising', trace)
                exported = Path(directory) / 'replay.json'
                exported.write_text(json.dumps(bundle))
                coordinator.store = Store(path)
                for job_id, packet in packets.items():
                    self.assertEqual(coordinator.store.job_context_packet(job_id), packet)
                self.assertFalse(coordinator.tick())
                replay, usage = verifier.verify_composed(json.loads(exported.read_text()))
                self.assertTrue(replay.verified, replay.diagnostics)
                self.assertEqual(usage['subprocesses'], 2)
                missing, missing_usage = verifier.verify_composed(bundle | {
                    'declarations': [], 'selected_proof_ids': []})
                self.assertFalse(missing.verified)  # The candidate requires B's exact declaration.
                self.assertEqual(missing_usage['subprocesses'], 2)
                self.assertEqual(usage['status'], 'known')
                self.assertEqual(set(usage['transitive']), set(evidence['usage']['transitive']))
                self.assertGreaterEqual(replay.elapsed_ms, 0)
                self.assertGreaterEqual(missing.elapsed_ms, 0)
                # Two explicit external operations add four subprocesses to the five/ten
                # coordinator checks; neither is silently billed as group model work.
                self.assertEqual(coordinator.store.frontier_trace(group_id)['lean']['operations'], 5)
                return next_action

    def test_paired_graph_loop_composition_and_redirect(self):
        accepted = self.scenario()
        rejected = self.scenario(rejected=True)
        self.assertEqual(accepted[0], 'critique')
        self.assertIn('reviewed challenge', accepted[1])
        self.assertIn('negative formal', rejected[1])
