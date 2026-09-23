import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from solvenet.sandbox import (
    ContainerVerifier,
    ContainerVerifierConfig,
    DockerResourceLimits,
    DOCKER_STDERR_TRUNCATION_MARKER,
    MAX_CONTAINER_RESULT_BYTES,
    MAX_DOCKER_STDERR_BYTES,
    _encode_result,
    _run_docker,
)
from solvenet.verifier import (
    DIAGNOSTICS_TRUNCATION_MARKER,
    MAX_DIAGNOSTICS_BYTES,
    LeanVerifierConfig,
    VerificationResult,
    VerificationStatus,
)


class ContainerTests(unittest.TestCase):
    def test_limits_and_result_transport(self):
        calls = []

        def execute(command, timeout):
            calls.append(command)
            for flag in ('--network=none', '--read-only', '--cap-drop=ALL',
                         '--security-opt=no-new-privileges', '--memory=1g', '--cpus=1', '--pids-limit=64'):
                self.assertIn(flag, command)
            mount = command[command.index('--mount') + 1]
            directory = Path(mount.removeprefix('type=bind,src=').removesuffix(',dst=/work'))
            request = json.loads((directory / 'request.json').read_text())
            self.assertEqual(request['candidate'], 'rfl')
            (directory / 'result.json').write_text(json.dumps({'status': 'verified', 'diagnostics': '', 'elapsed_ms': 1}))
            return subprocess.CompletedProcess(command, 0, stderr=b'ignored warning')

        with patch('solvenet.sandbox._run_docker', side_effect=execute), \
             patch('solvenet.sandbox.subprocess.run') as cleanup:
            result = ContainerVerifier().verify('(n : Nat) : n + 0 = n', 'rfl')
        self.assertTrue(result.verified)
        self.assertEqual(result.diagnostics, '')
        self.assertEqual(result.elapsed_ms, 1)
        self.assertEqual(cleanup.call_args.args[0][:3], ['docker', 'rm', '-f'])

    def test_configuration_is_propagated_to_request_and_docker(self):
        observed = {}

        def execute(command, timeout):
            observed['command'] = command
            observed['timeout'] = timeout
            mount = command[command.index('--mount') + 1]
            directory = Path(
                mount.removeprefix('type=bind,src=').removesuffix(',dst=/work')
            )
            observed['request'] = json.loads(
                (directory / 'request.json').read_text()
            )
            (directory / 'result.json').write_text(json.dumps({
                'status': 'verified', 'diagnostics': '', 'elapsed_ms': 1,
            }))
            return subprocess.CompletedProcess(command, 0, stderr=b'')

        verifier = ContainerVerifier(
            verifier_config=LeanVerifierConfig(
                timeout_seconds=12, max_diagnostics_bytes=2048,
            ),
            container_config=ContainerVerifierConfig(deadline_seconds=14),
            resources=DockerResourceLimits(
                cpus=2, memory='2g', memory_swap='2g', pids=32,
                file_size_bytes=2048, tmpfs_size='64m',
            ),
        )
        with patch('solvenet.sandbox._run_docker', side_effect=execute), \
             patch('solvenet.sandbox.subprocess.run'):
            self.assertTrue(verifier.verify(': True', 'trivial').verified)

        self.assertEqual(observed['timeout'], 14)
        self.assertEqual(observed['request']['timeout_seconds'], 12)
        self.assertEqual(observed['request']['max_diagnostics_bytes'], 2048)
        for option in (
            '--cpus=2', '--memory=2g', '--memory-swap=2g', '--pids-limit=32',
            'fsize=2048:2048', '/tmp:rw,noexec,nosuid,size=64m,mode=1777',
        ):
            self.assertIn(option, observed['command'])

    def test_verify_and_readiness_share_isolation_and_resource_flags(self):
        commands = []

        def execute(command, timeout):
            commands.append(command)
            if '--mount' in command:
                mount = command[command.index('--mount') + 1]
                directory = Path(mount.removeprefix('type=bind,src=').removesuffix(',dst=/work'))
                (directory / 'result.json').write_text(json.dumps({
                    'status': 'verified', 'diagnostics': '', 'elapsed_ms': 1,
                }))
            return subprocess.CompletedProcess(command, 0, stderr=b'')

        verifier = ContainerVerifier(
            image='verifier:test',
            resources=DockerResourceLimits(
                cpus=2, memory='2g', memory_swap='3g', pids=32,
                file_size_bytes=2048, tmpfs_size='64m',
            ),
        )
        with patch('solvenet.sandbox._run_docker', side_effect=execute), \
             patch('solvenet.sandbox.subprocess.run'):
            self.assertTrue(verifier.verify(': True', 'trivial').verified)
            self.assertTrue(verifier.readiness().ready)

        verify, readiness = commands
        self.assertEqual(verify[:verify.index('--name')], readiness[:readiness.index('--name')])
        self.assertEqual(verify[verify.index('--network=none'):verify.index('--mount')],
                         readiness[readiness.index('--network=none'):-2])
        for option in (
            '--network=none', '--read-only', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--pids-limit=32',
            '--memory=2g', '--memory-swap=3g', '--cpus=2',
            'fsize=2048:2048', f'{os.getuid()}:{os.getgid()}',
            '/tmp:rw,noexec,nosuid,size=64m,mode=1777',
        ):
            self.assertIn(option, verify)
        self.assertEqual(verify[-1], 'verifier:test')
        self.assertEqual(readiness[-2:], ['verifier:test', '--readiness'])
        self.assertNotIn('--mount', readiness)

    def test_container_deadline_reserves_overhead(self):
        with self.assertRaisesRegex(ValueError, 'minimum 1 second'):
            ContainerVerifier(
                verifier_config=LeanVerifierConfig(timeout_seconds=10),
                container_config=ContainerVerifierConfig(deadline_seconds=10.5),
            )

    def _transport(self, payload):
        def execute(command, timeout):
            mount = command[command.index('--mount') + 1]
            directory = Path(mount.removeprefix('type=bind,src=').removesuffix(',dst=/work'))
            (directory / 'result.json').write_bytes(payload)
            return subprocess.CompletedProcess(command, 0, stderr=b'')

        with patch('solvenet.sandbox._run_docker', side_effect=execute), \
             patch('solvenet.sandbox.subprocess.run'):
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
        with patch(
            'solvenet.sandbox._run_docker',
            side_effect=subprocess.TimeoutExpired(['docker', 'run'], 30),
        ), patch('solvenet.sandbox.subprocess.run') as cleanup:
            result = ContainerVerifier().verify(': True', 'trivial')
        self.assertEqual(result.status, VerificationStatus.TIMEOUT)
        self.assertEqual(result.diagnostics, 'Container verification deadline exceeded')
        self.assertGreaterEqual(cleanup.call_count, 1)
        self.assertEqual(cleanup.call_args_list[0].args[0][:3], ['docker', 'rm', '-f'])

    def test_missing_docker_is_infrastructure_error(self):
        with patch('solvenet.sandbox._run_docker', side_effect=FileNotFoundError('docker')), \
             patch('solvenet.sandbox.subprocess.run'):
            result = ContainerVerifier().verify(': True', 'trivial')
        self.assertEqual(result.status, VerificationStatus.VERIFIER_ERROR)

    def test_missing_image_includes_actionable_stderr(self):
        stderr = b'Unable to find image solvenet-verifier:local locally'
        completed = subprocess.CompletedProcess(['docker', 'run'], 125, stderr=stderr)
        with patch('solvenet.sandbox._run_docker', return_value=completed), \
             patch('solvenet.sandbox.subprocess.run'):
            result = ContainerVerifier().verify(': True', 'trivial')
        self.assertEqual(result.status, VerificationStatus.VERIFIER_ERROR)
        self.assertIn('exited with code 125', result.diagnostics)
        self.assertIn('Docker stderr: Unable to find image', result.diagnostics)

    def test_readiness_checks_configured_image_without_candidate_input(self):
        completed = subprocess.CompletedProcess(['docker', 'run'], 0, stderr=b'')
        with patch('solvenet.sandbox._run_docker', return_value=completed) as run, \
             patch('solvenet.sandbox.subprocess.run'):
            result = ContainerVerifier(image='verifier:test').readiness()
        self.assertTrue(result.ready)
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ['verifier:test', '--readiness'])
        self.assertNotIn('--mount', command)
        self.assertIn('--network=none', command)

    def test_readiness_reports_missing_docker_and_image(self):
        verifier = ContainerVerifier(image='missing:test')
        with patch(
            'solvenet.sandbox._run_docker', side_effect=FileNotFoundError('docker')
        ), patch('solvenet.sandbox.subprocess.run'):
            result = verifier.readiness()
        self.assertFalse(result.ready)
        self.assertIn('Could not run Docker', result.diagnostics)

        completed = subprocess.CompletedProcess(
            ['docker', 'run'], 125,
            stderr=b'No such image: missing:test',
        )
        with patch('solvenet.sandbox._run_docker', return_value=completed), \
             patch('solvenet.sandbox.subprocess.run'):
            result = verifier.readiness()
        self.assertFalse(result.ready)
        self.assertIn('missing:test', result.diagnostics)
        self.assertIn('No such image', result.diagnostics)

    def test_runtime_failure_stderr_is_truncated_utf8_safely_and_redacted(self):
        observed_workspace = None

        def execute(command, timeout):
            nonlocal observed_workspace
            mount = command[command.index('--mount') + 1]
            observed_workspace = mount.removeprefix('type=bind,src=').removesuffix(',dst=/work')
            stderr = (
                f'cannot mount {observed_workspace}: '.encode()
                + '€'.encode() * MAX_DOCKER_STDERR_BYTES
            )
            return subprocess.CompletedProcess(command, 125, stderr=stderr)

        with patch('solvenet.sandbox._run_docker', side_effect=execute), \
             patch('solvenet.sandbox.subprocess.run'):
            result = ContainerVerifier().verify(': True', 'trivial')
        self.assertEqual(result.status, VerificationStatus.VERIFIER_ERROR)
        self.assertNotIn(observed_workspace, result.diagnostics)
        self.assertIn('[verification workspace]', result.diagnostics)
        self.assertTrue(result.diagnostics.endswith(DOCKER_STDERR_TRUNCATION_MARKER))
        result.diagnostics.encode('utf-8')

    def test_docker_runner_retains_only_bounded_stderr(self):
        completed = _run_docker(
            [sys.executable, '-c', f'import sys; sys.stderr.write("x" * {MAX_DOCKER_STDERR_BYTES * 4})'],
            5,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(len(completed.stderr), MAX_DOCKER_STDERR_BYTES + 1)

    @unittest.skipUnless(os.environ.get('SOLVENET_DOCKER_TEST') == '1', 'Opt-in Docker smoke test')
    def test_real_container(self):
        verifier = ContainerVerifier()
        readiness = verifier.readiness()
        self.assertTrue(readiness.ready, readiness.diagnostics)
        good = verifier.verify('(n : Nat) : n + 0 = n', 'rfl')
        self.assertTrue(good.verified, good.diagnostics)
        bad = verifier.verify(': False', 'sorry')
        self.assertEqual(bad.status, VerificationStatus.REJECTED, bad.diagnostics)
