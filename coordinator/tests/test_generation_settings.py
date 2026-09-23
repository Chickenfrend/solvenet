import tempfile
import unittest
from pathlib import Path

from solvenet.server import validate_generation
from solvenet.store import Store, validate_settings
from solvenet.verifier import VerificationResult, VerificationStatus


class GenerationSettingsTests(unittest.TestCase):
    def test_bounds_and_unknown_controls(self):
        for value in ({'temperature': -0.1}, {'temperature': 2.01},
                      {'temperature': True}, {'temperature': float('nan')},
                      {'seed': -1}, {'seed': 2**63}, {'seed': 1.0},
                      {'seed': False}, {'top_p': 0.5}, [], None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_settings(value)
        self.assertEqual(validate_settings({'temperature': 0, 'seed': 0}),
                         {'temperature': 0, 'seed': 0})
        for value in ({'temperature': 3}, {'seed': -1}, {'model_digest': 3}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_generation({'generation': value})

    def test_settings_survive_restart_and_repair_and_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.db'
            store = Store(path)
            run = store.submit(': True', ['Init'], attempts=1, model='ollama/test',
                               max_repairs=1, generation_settings={'temperature': 0, 'seed': 42})['run_id']
            store = Store(path)
            self.assertIsNone(store.claim('old-worker', ['ollama/test']))
            first = store.claim('worker', ['ollama/test'], supports_generation_settings=True)
            self.assertEqual(first['job']['generation_settings'], {'temperature': 0, 'seed': 42})
            store.result(first['assignment_id'], {'lease_token': first['lease_token'],
                         'status': 'completed', 'output': {'text': 'invalid'},
                         'generation': {'model': 'test', 'temperature': 0, 'seed': 42,
                                        'model_digest': 'sha256:' + 'a' * 64}})
            self.assertEqual(store.run(run)['attempts'][0]['generation']['seed'], 42)
            store.verified(store.pending()['id'], VerificationResult(VerificationStatus.REJECTED, 'bad', 1))
            store = Store(path)
            self.assertEqual(store.run(run)['generation_settings'], {'temperature': 0, 'seed': 42})
            self.assertIsNone(store.claim('old-worker', ['ollama/test']))
            self.assertEqual(store.claim('worker', ['ollama/test'],
                                         supports_generation_settings=True)['job']['generation_settings'],
                             {'temperature': 0, 'seed': 42})
            old = store.submit(': True', ['Init'], attempts=1)['run_id']
            self.assertEqual(store.run(old)['generation_settings'], {})
            self.assertEqual(store.claim('old-worker', ['scripted'])['job']['generation_settings'], {})

    def test_scripted_configuration_error(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / 'state.db')
            for kwargs in ({'model': 'scripted'},
                           {'initial_jobs': [{'model': 'ollama/test', 'count': 1},
                                             {'model': 'scripted', 'count': 1}]}):
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, 'scripted'):
                    store.submit(': True', ['Init'],
                                 generation_settings={'seed': 7}, **kwargs)
