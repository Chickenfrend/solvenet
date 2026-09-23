import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from solvenet.sandbox import ContainerVerifier, MAX_CONTAINER_RESULT_BYTES, _encode_result
from solvenet.verifier import (
    DIAGNOSTICS_TRUNCATION_MARKER,
    MAX_DIAGNOSTICS_BYTES,
    VerificationResult,
    VerificationStatus,
)


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
        self.assertEqual(result.elapsed_ms, 1)
        self.assertEqual(calls[-1][:3], ['docker', 'rm', '-f'])

    def _transport(self, payload):
        def execute(command, **kwargs):
            if command[1] == 'run':
                mount = command[command.index('--mount') + 1]
                directory = Path(mount.removeprefix('type=bind,src=').removesuffix(',dst=/work'))
                (directory / 'result.json').write_bytes(payload)
            return subprocess.CompletedProcess(command, 0)

        with patch('solvenet.sandbox.subprocess.run', side_effect=execute):
            return ContainerVerifier().verify(': False', 'trivial')

    def test_control_characters_at_diagnostic_limit_survive_json_expansion(self):
        diagnostics = '\x01' * MAX_DIAGNOSTICS_BYTES
        payload = _encode_result(
            VerificationResult(VerificationStatus.REJECTED, diagnostics, 7)
        )
        self.assertGreater(len(payload), 128 * 1024)
        self.assertLessEqual(len(payload), MAX_CONTAINER_RESULT_BYTES)
        result = self._transport(payload)
        self.assertEqual(result.status, VerificationStatus.REJECTED)
        self.assertEqual(result.diagnostics, diagnostics)
        self.assertEqual(result.elapsed_ms, 7)

    def test_oversized_unicode_diagnostics_are_utf8_safely_truncated(self):
        diagnostics = 'é' * (MAX_DIAGNOSTICS_BYTES // 2 + 1)
        payload = _encode_result(
            VerificationResult(VerificationStatus.REJECTED, diagnostics, 9)
        )
        result = self._transport(payload)
        self.assertEqual(result.status, VerificationStatus.REJECTED)
        self.assertTrue(result.diagnostics.endswith(DIAGNOSTICS_TRUNCATION_MARKER))
        prefix = result.diagnostics.removesuffix(DIAGNOSTICS_TRUNCATION_MARKER)
        self.assertEqual(len(prefix.encode('utf-8')), MAX_DIAGNOSTICS_BYTES)

    def test_invalid_elapsed_ms_is_an_infrastructure_error(self):
        for elapsed_ms in (-1, 1.5, True, '1'):
            with self.subTest(elapsed_ms=elapsed_ms):
                payload = json.dumps({
                    'status': 'rejected', 'diagnostics': 'bad proof',
                    'elapsed_ms': elapsed_ms,
                }).encode()
                result = self._transport(payload)
                self.assertEqual(result.status, VerificationStatus.VERIFIER_ERROR)
                self.assertIn('elapsed_ms', result.diagnostics)

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
