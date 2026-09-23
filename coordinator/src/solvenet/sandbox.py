"""Docker transport for the local Lean verifier; no Docker socket in the guest."""

import json
import math
import os
import selectors
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from .verifier import (
    DIAGNOSTICS_TRUNCATION_MARKER,
    MAX_DIAGNOSTICS_BYTES,
    LeanVerifierConfig,
    LeanVerifier,
    VerifierReadiness,
    VerificationResult,
    VerificationStatus,
    truncate_diagnostics,
    unavailable_readiness,
)


DEFAULT_CONTAINER_TIMEOUT_SECONDS = 30.0
MIN_CONTAINER_OVERHEAD_SECONDS = 1.0
MAX_DOCKER_STDERR_BYTES = 8 * 1024
DOCKER_STDERR_TRUNCATION_MARKER = '\n[Docker stderr truncated]'


def _max_container_result_bytes(max_diagnostics_bytes):
    # JSON control characters may occupy six bytes (for example, ``\u0000``).
    # The fixed allowance covers field names, status, elapsed time, and the marker.
    return 6 * (
        max_diagnostics_bytes + len(DIAGNOSTICS_TRUNCATION_MARKER.encode('utf-8'))
    ) + 1024


MAX_CONTAINER_RESULT_BYTES = _max_container_result_bytes(MAX_DIAGNOSTICS_BYTES)


def _run_docker(command, timeout):
    """Run Docker while draining stderr and retaining only a bounded prefix."""
    retained = bytearray()
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stderr, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    available = MAX_DOCKER_STDERR_BYTES + 1 - len(retained)
                    if available > 0:
                        retained.extend(chunk[:available])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            returncode = process.wait(timeout=remaining)
        return subprocess.CompletedProcess(command, returncode, stderr=bytes(retained))
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait()
        raise
    finally:
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait()
        if process.stderr is not None:
            process.stderr.close()


def _docker_error_diagnostics(message, stderr, workspace):
    """Add a safe, UTF-8-valid Docker stderr excerpt to an infrastructure error."""
    clipped = stderr[:MAX_DOCKER_STDERR_BYTES]
    excerpt = clipped.decode('utf-8', errors='ignore').strip()
    if workspace is not None:
        excerpt = excerpt.replace(str(workspace), '[verification workspace]')
    if len(stderr) > MAX_DOCKER_STDERR_BYTES:
        excerpt += DOCKER_STDERR_TRUNCATION_MARKER
    if excerpt:
        return f'{message}\nDocker stderr: {excerpt}'
    return message


@dataclass(frozen=True)
class DockerResourceLimits:
    """Named Docker limits; intentionally not a general Docker option bag."""

    cpus: float = 1.0
    memory: str = '1g'
    memory_swap: str = '1g'
    pids: int = 64
    file_size_bytes: int = 1024 * 1024
    tmpfs_size: str = '128m'

    def __post_init__(self):
        if (
            not math.isfinite(self.cpus)
            or self.cpus <= 0
            or self.pids <= 0
            or self.file_size_bytes <= 0
        ):
            raise ValueError('Docker numeric resource limits must be positive')
        if not self.memory or not self.memory_swap or not self.tmpfs_size:
            raise ValueError('Docker size resource limits must be nonempty')


@dataclass(frozen=True)
class ContainerVerifierConfig:
    deadline_seconds: float = DEFAULT_CONTAINER_TIMEOUT_SECONDS
    overhead_seconds: float = MIN_CONTAINER_OVERHEAD_SECONDS

    def validate(self, lean: LeanVerifierConfig):
        if (
            not math.isfinite(self.deadline_seconds)
            or not math.isfinite(self.overhead_seconds)
            or self.deadline_seconds <= 0
            or self.overhead_seconds < 0
        ):
            raise ValueError('Container deadline must be positive and overhead nonnegative')
        minimum = lean.timeout_seconds + self.overhead_seconds
        if self.deadline_seconds < minimum:
            raise ValueError(
                'Container timeout must be at least the Lean timeout plus container '
                f'overhead (minimum {self.overhead_seconds:g} second(s))'
            )


def _encode_result(
    result: VerificationResult, max_diagnostics_bytes=MAX_DIAGNOSTICS_BYTES,
) -> bytes:
    fields = asdict(result)
    fields['diagnostics'] = truncate_diagnostics(
        fields['diagnostics'], max_diagnostics_bytes,
    )
    encoded = json.dumps(
        fields, ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8')
    if len(encoded) > _max_container_result_bytes(max_diagnostics_bytes):
        raise ValueError('Container result exceeded size limit')
    return encoded


def _decode_result(
    raw: bytes, max_diagnostics_bytes=MAX_DIAGNOSTICS_BYTES,
) -> VerificationResult:
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError('Invalid container result')
    try:
        status_value = result['status']
        diagnostics = result['diagnostics']
        elapsed_ms = result['elapsed_ms']
    except KeyError as error:
        raise ValueError(f'Missing container result field: {error.args[0]}') from error
    if not isinstance(status_value, str):
        raise ValueError('Invalid container status')
    try:
        status = VerificationStatus(status_value)
    except ValueError as error:
        raise ValueError('Invalid container status') from error
    if not isinstance(diagnostics, str):
        raise ValueError('Invalid container diagnostics')
    if type(elapsed_ms) is not int or elapsed_ms < 0:
        raise ValueError('Invalid container elapsed_ms')
    return VerificationResult(
        status, truncate_diagnostics(diagnostics, max_diagnostics_bytes), elapsed_ms,
    )


class ContainerVerifier:
    def __init__(
        self,
        image='solvenet-verifier:local',
        timeout_seconds=None,
        *,
        verifier_config=None,
        container_config=None,
        resources=None,
    ):
        if timeout_seconds is not None and container_config is not None:
            raise ValueError('Use either container config or timeout_seconds')
        self.image = image
        self.verifier_config = verifier_config or LeanVerifierConfig()
        self.container_config = container_config or ContainerVerifierConfig(
            deadline_seconds=(
                DEFAULT_CONTAINER_TIMEOUT_SECONDS
                if timeout_seconds is None
                else timeout_seconds
            )
        )
        self.timeout_seconds = self.container_config.deadline_seconds
        self.resources = resources or DockerResourceLimits()
        self.container_config.validate(self.verifier_config)

    def verify(self, statement, candidate, *, imports=('Init',)):
        started = time.monotonic()
        name = 'solvenet-verify-' + uuid4().hex
        status = VerificationStatus.VERIFIER_ERROR
        diagnostics = 'Container verifier did not complete'
        elapsed_ms = None
        docker_stderr = b''
        workspace = None
        try:
            with tempfile.TemporaryDirectory(prefix='solvenet-container-') as directory:
                path = Path(directory)
                workspace = path
                (path / 'request.json').write_text(
                    json.dumps(
                        {
                            'statement': statement,
                            'candidate': candidate,
                            'imports': imports,
                            'timeout_seconds': self.verifier_config.timeout_seconds,
                            'max_diagnostics_bytes': self.verifier_config.max_diagnostics_bytes,
                        },
                        ensure_ascii=False,
                    ),
                    encoding='utf-8',
                )
                command = [
                    'docker', 'run', '--rm', '--pull=never', '--name', name,
                    '--network=none', '--read-only', '--cap-drop=ALL',
                    '--security-opt=no-new-privileges',
                    f'--pids-limit={self.resources.pids}',
                    f'--memory={self.resources.memory}',
                    f'--memory-swap={self.resources.memory_swap}',
                    f'--cpus={self.resources.cpus:g}',
                    '--ulimit',
                    f'fsize={self.resources.file_size_bytes}:{self.resources.file_size_bytes}',
                    '--user', f'{os.getuid()}:{os.getgid()}',
                    '--tmpfs',
                    f'/tmp:rw,noexec,nosuid,size={self.resources.tmpfs_size},mode=1777',
                    '--mount', f'type=bind,src={path},dst=/work', self.image,
                ]
                try:
                    completed = _run_docker(
                        command, self.container_config.deadline_seconds,
                    )
                    docker_stderr = completed.stderr
                except (OSError, subprocess.TimeoutExpired):
                    # Stop the guest before deleting its bind-mounted workspace.
                    self._remove(name)
                    raise
                if completed.returncode != 0:
                    diagnostics = _docker_error_diagnostics(
                        f'Container exited with code {completed.returncode}; '
                        f'check Docker and image {self.image}',
                        docker_stderr,
                        workspace,
                    )
                else:
                    with (path / 'result.json').open('rb') as output:
                        result_limit = _max_container_result_bytes(
                            self.verifier_config.max_diagnostics_bytes
                        )
                        raw = output.read(result_limit + 1)
                    if len(raw) > result_limit:
                        raise ValueError('Container result exceeded size limit')
                    result = _decode_result(
                        raw, self.verifier_config.max_diagnostics_bytes,
                    )
                    status = result.status
                    diagnostics = result.diagnostics
                    elapsed_ms = result.elapsed_ms
        except subprocess.TimeoutExpired:
            status = VerificationStatus.TIMEOUT
            diagnostics = 'Container verification deadline exceeded'
        except (OSError, ValueError, KeyError) as error:
            status = VerificationStatus.VERIFIER_ERROR
            diagnostics = _docker_error_diagnostics(
                f'Container verifier error: {error}', docker_stderr, workspace,
            )
        finally:
            # Killing the Docker CLI alone does not stop the container.
            self._remove(name)
        if elapsed_ms is None:
            elapsed_ms = round((time.monotonic() - started) * 1000)
        return VerificationResult(status, diagnostics, elapsed_ms)

    def readiness(self):
        """Check Docker, the configured image, and its trusted Lean smoke test."""
        name = 'solvenet-ready-' + uuid4().hex
        command = [
            'docker', 'run', '--rm', '--pull=never', '--name', name,
            '--network=none', '--read-only', '--cap-drop=ALL',
            '--security-opt=no-new-privileges',
            f'--pids-limit={self.resources.pids}',
            f'--memory={self.resources.memory}',
            f'--memory-swap={self.resources.memory_swap}',
            f'--cpus={self.resources.cpus:g}',
            '--ulimit',
            f'fsize={self.resources.file_size_bytes}:{self.resources.file_size_bytes}',
            '--user', f'{os.getuid()}:{os.getgid()}',
            '--tmpfs',
            f'/tmp:rw,noexec,nosuid,size={self.resources.tmpfs_size},mode=1777',
            self.image, '--readiness',
        ]
        try:
            completed = _run_docker(command, self.container_config.deadline_seconds)
            if completed.returncode == 0:
                return VerifierReadiness(True)
            diagnostics = _docker_error_diagnostics(
                f'Verifier image readiness failed with code {completed.returncode}; '
                f'check Docker and image {self.image}',
                completed.stderr,
                None,
            )
            return unavailable_readiness(diagnostics)
        except subprocess.TimeoutExpired:
            return unavailable_readiness('Verifier image readiness check timed out')
        except OSError as error:
            return unavailable_readiness(f'Could not run Docker: {error}')
        finally:
            self._remove(name)

    @staticmethod
    def _remove(name):
        try:
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass


def main():
    if sys.argv[1:] == ['--readiness']:
        result = LeanVerifier(
            Path('/opt/solvenet/lean'), command=('lean',),
        ).readiness()
        if not result.ready:
            print(result.diagnostics, file=sys.stderr)
        raise SystemExit(0 if result.ready else 1)
    request = json.loads(Path('/work/request.json').read_text(encoding='utf-8'))
    config = LeanVerifierConfig(
        timeout_seconds=request['timeout_seconds'],
        max_diagnostics_bytes=request['max_diagnostics_bytes'],
    )
    verifier = LeanVerifier(
        Path('/opt/solvenet/lean'), command=('lean',), config=config,
    )
    result = verifier.verify(request['statement'], request['candidate'], imports=request['imports'])
    Path('/work/result.json').write_bytes(
        _encode_result(result, config.max_diagnostics_bytes)
    )


if __name__ == '__main__':
    main()
