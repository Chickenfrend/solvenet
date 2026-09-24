import unittest

from report import summarize


def attempt(status='rejected', input_tokens=10, output_tokens=5):
    return {'candidate': 'rfl', 'outcome': 'other_rejection', 'usage': {'input_tokens': input_tokens, 'output_tokens': output_tokens},
            'generation': {'total_duration_ns': 100}, 'verification': {'status': status, 'elapsed_ms': 10}}


class ReportTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
