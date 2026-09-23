import tempfile
import unittest
from pathlib import Path

from solvenet.server import validate_generation
from solvenet.store import Store, validate_settings
from solvenet.verifier import VerificationResult, VerificationStatus


class GenerationSettingsTests(unittest.TestCase):
    def test_initial_seeds_follow_chain_order_across_groups_and_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.db'
            store = Store(path)
            run = store.submit(': True', ['Init'], initial_jobs=[
                {'model': 'ollama/a', 'count': 2},
                {'model': 'ollama/b', 'count': 1}],
                generation_settings={'temperature': 0.6, 'seed': 11})['run_id']
            store = Store(path)
            detail = store.run(run)
            self.assertEqual(detail['generation_settings'], {'temperature': 0.6, 'seed': 11})
            self.assertEqual([job['generation_settings']['seed'] for job in detail['jobs']], [11, 12, 13])
            self.assertEqual([job['model'] for job in detail['jobs']],
                             ['ollama/a', 'ollama/a', 'ollama/b'])
            claims = [store.claim('worker', ['ollama/a', 'ollama/b'],
                                  supports_generation_settings=True) for _ in range(3)]
            self.assertEqual([claim['job']['generation_settings']['seed'] for claim in claims],
                             [11, 12, 13])
            for claim in claims:
                store.result(claim['assignment_id'], {'lease_token': claim['lease_token'],
                             'status': 'completed', 'output': {'text': 'candidate'}})
            detail = Store(path).run(run)
            self.assertEqual([a['generation_settings']['seed'] for a in detail['attempts']],
                             [11, 12, 13])
            self.assertEqual([a['generation_settings']['seed'] for a in detail['assignments']],
                             [11, 12, 13])
            self.assertEqual([a['generation'] for a in detail['attempts']], [{}, {}, {}])

    def test_repair_inherits_seed_and_overflow_rejected_before_insertion(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.db'
            store = Store(path)
            maximum = 2**63 - 1
            for groups, seed in ((None, maximum - 1),
                                 ([{'model': 'ollama/a', 'count': 1},
                                   {'model': 'ollama/b', 'count': 2}], maximum - 1)):
                with self.subTest(groups=groups), self.assertRaisesRegex(ValueError, 'chain index'):
                    options = ({'model': 'ollama/a'} if groups is None else {'initial_jobs': groups})
                    store.submit(': True', ['Init'], generation_settings={'seed': seed}, **options)
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
            bounded = store.submit(': True', ['Init'], attempts=2, model='ollama/b',
                                   generation_settings={'seed': maximum - 1})['run_id']
            self.assertEqual([j['generation_settings']['seed'] for j in store.run(bounded)['jobs']],
                             [maximum - 1, maximum])
            run = store.submit(': True', ['Init'], attempts=1, model='ollama/a', max_repairs=2,
                               generation_settings={'seed': maximum})['run_id']
            for depth in range(3):
                store = Store(path)
                claim = store.claim('worker', ['ollama/a'], supports_generation_settings=True)
                self.assertEqual(claim['job']['generation_settings']['seed'], maximum)
                self.assertEqual(claim['job']['repair_depth'], depth)
                store.result(claim['assignment_id'], {'lease_token': claim['lease_token'],
                             'status': 'completed', 'output': {'text': 'invalid'}})
                store.verified(store.pending()['id'], VerificationResult(VerificationStatus.REJECTED, 'bad', 1))
            detail = Store(path).run(run)
            self.assertEqual([j['generation_settings']['seed'] for j in detail['jobs']],
                             [maximum] * 3)
            self.assertEqual([a['generation_settings']['seed'] for a in detail['attempts']],
                             [maximum] * 3)

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
