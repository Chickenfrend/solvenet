import io
import json
import unittest
from unittest.mock import patch

from report import summarize
import verifier_bridge


def attempt(status='rejected', input_tokens=10, output_tokens=5):
    return {'candidate': 'rfl', 'outcome': 'other_rejection', 'usage': {'input_tokens': input_tokens, 'output_tokens': output_tokens},
            'generation': {'total_duration_ns': 100}, 'verification': {'status': status, 'elapsed_ms': 10}}


class ReportTests(unittest.TestCase):
    def test_bridge_selects_current_verifier_image(self):
        class FakeVerifier:
            def __init__(self, image):
                self.image = image

            def readiness(self):
                self_test.assertEqual(self.image, 'solvenet-verifier:homelab')
                return type('Readiness', (), {'ready': False, 'diagnostics': 'test-only'})()

        self_test = self
        output = io.StringIO()
        with patch.object(verifier_bridge, 'ContainerVerifier', FakeVerifier), \
                patch.object(verifier_bridge.sys, 'stdin', io.StringIO('{"action":"readiness"}')), \
                patch.object(verifier_bridge.sys, 'stdout', output):
            verifier_bridge.main()
        self.assertEqual(json.loads(output.getvalue()), {'ready': False, 'error': 'test-only'})

    def test_shared_cost_and_threshold(self):
        blocks = [{'problem_id': f'p{n % 12}', 'base_seed': [121, 132, 143, 154, 165][n // 12],
                   'prefix': [attempt(), attempt()], 'baseline': attempt(), 'collaborative': attempt()}
                  for n in range(60)]
        blocks[0]['prefix'][0] = attempt('verified')
        blocks[0]['baseline'] = blocks[0]['collaborative'] = None
        blocks[0]['map'] = None
        blocks[1]['collaborative'] = attempt('verified')
        result = summarize({'complete': True, 'blocks': blocks, 'config': {}})
        self.assertEqual(result['physical_shared_prefix_cost']['requests'], 120)
        self.assertEqual(result['charged_cost']['baseline']['requests'], 179)
        self.assertEqual(result['charged_cost']['collaborative']['requests'], 179)
        self.assertEqual(result['incremental_third_cost']['baseline']['requests'], 59)
        self.assertEqual(result['paired_outcomes']['collaborative_only'], 1)
        self.assertTrue(result['cost_comparable'])
        self.assertFalse(result['next_local_protocol_experiment_supported'])
        blocks[1]['collaborative']['usage']['input_tokens'] = None
        self.assertFalse(summarize({'complete': True, 'blocks': blocks, 'config': {}})['cost_comparable'])

    def test_partial_has_no_comparison(self):
        result = summarize({'complete': False, 'blocks': [], 'partial': {'problem_id': 'p0'}, 'aborted': 'digest changed'})
        self.assertIsNone(result['comparison'])

    def test_completed_run_with_format_and_request_failures(self):
        blocks = [{'problem_id': f'p{n % 12}', 'base_seed': [121, 132, 143, 154, 165][n // 12],
                   'prefix': [attempt(), attempt()], 'baseline': attempt(), 'collaborative': attempt()}
                  for n in range(60)]
        blocks[0]['baseline'] = {'candidate': '', 'outcome': 'format', 'verification': None, 'usage': None}
        blocks[1]['collaborative'] = {'candidate': '', 'outcome': 'request_failure', 'verification': None,
                                      'usage': None}
        result = summarize({'complete': True, 'blocks': blocks, 'config': {}})
        self.assertEqual(result['charged_cost']['baseline']['format'], 1)
        self.assertEqual(result['charged_cost']['collaborative']['request_failure'], 1)
        self.assertFalse(result['cost_comparable'])

    def test_unrounded_cost_gate_and_all_unsolved_fixtures_are_reported(self):
        blocks = [{'problem_id': f'p{n % 12}', 'base_seed': [121, 132, 143, 154, 165][n // 12],
                   'prefix': [attempt(input_tokens=0), attempt(input_tokens=0)],
                   'baseline': attempt(input_tokens=0), 'collaborative': attempt(input_tokens=0)}
                  for n in range(60)]
        blocks[0]['baseline']['usage']['input_tokens'] = 17000
        blocks[0]['collaborative']['usage']['input_tokens'] = 20001
        result = summarize({'complete': True, 'blocks': blocks, 'config': {}})
        self.assertEqual(result['relative_cost_difference_percent']['all']['input_tokens'], 15.0)
        self.assertFalse(result['cost_comparable'])
        self.assertEqual(len(result['per_fixture_paired_outcomes']), 12)
        self.assertEqual(result['per_fixture_paired_outcomes']['p0']['neither'], 5)
        self.assertEqual(len(result['distinct_solved_per_seed']), 5)


if __name__ == '__main__':
    unittest.main()
