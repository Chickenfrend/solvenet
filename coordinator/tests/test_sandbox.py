import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from solvenet.sandbox import ContainerVerifier
from solvenet.verifier import VerificationStatus


class ContainerTests(unittest.TestCase):
    def test_limits_and_result_transport(self):
        calls = []

        def execute(command, **kwargs):
            calls.append(command)
            if command[1] == 'run':
                for flag in ('--network=none', '--read-only', '--cap-drop=ALL',
                             '--security-opt=no-new-privileges', '--memory=1g', '--cpus=1', '--pids-limit=64'):
                    self.assertIn(flag, command)
                mount = command[command.index('--mount') + 1]
                directory = Path(mount.removeprefix('type=bind,src=').removesuffix(',dst=/work'))
                request = json.loads((directory / 'request.json').read_text())
                self.assertEqual(request['candidate'], 'rfl')
                (directory / 'result.json').write_text(json.dumps({'status': 'verified', 'diagnostics': '', 'elapsed_ms': 1}))
            return subprocess.CompletedProcess(command, 0)

        with patch('solvenet.sandbox.subprocess.run', side_effect=execute):
            result = ContainerVerifier().verify('(n : Nat) : n + 0 = n', 'rfl')
        self.assertTrue(result.verified)
        self.assertEqual(calls[-1][:3], ['docker', 'rm', '-f'])

    def test_timeout_removes_container(self):
        calls = []

        def execute(command, **kwargs):
            calls.append(command)
            if command[1] == 'run':
                raise subprocess.TimeoutExpired(command, 30)
            return subprocess.CompletedProcess(command, 0)

        with patch('solvenet.sandbox.subprocess.run', side_effect=execute):
            result = ContainerVerifier().verify(': True', 'trivial')
        self.assertEqual(result.status, VerificationStatus.TIMEOUT)
        self.assertEqual(calls[1][:3], ['docker', 'rm', '-f'])

    def test_missing_docker_is_infrastructure_error(self):
        with patch('solvenet.sandbox.subprocess.run', side_effect=FileNotFoundError('docker')):
            result = ContainerVerifier().verify(': True', 'trivial')
        self.assertEqual(result.status, VerificationStatus.VERIFIER_ERROR)

    @unittest.skipUnless(os.environ.get('SOLVENET_DOCKER_TEST') == '1', 'Opt-in Docker smoke test')
    def test_real_container(self):
        verifier = ContainerVerifier()
        good = verifier.verify('(n : Nat) : n + 0 = n', 'rfl')
        self.assertTrue(good.verified, good.diagnostics)
        bad = verifier.verify(': False', 'sorry')
        self.assertEqual(bad.status, VerificationStatus.REJECTED, bad.diagnostics)
