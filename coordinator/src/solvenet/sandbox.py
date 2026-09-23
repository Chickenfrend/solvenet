"""Docker transport for the local Lean verifier; no Docker socket in the guest."""

import json
import math
import os
import subprocess
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
    VerificationResult,
    VerificationStatus,
    truncate_diagnostics,
)


DEFAULT_CONTAINER_TIMEOUT_SECONDS = 30.0
MIN_CONTAINER_OVERHEAD_SECONDS = 1.0


def _max_container_result_bytes(max_diagnostics_bytes):
    # JSON control characters may occupy six bytes (for example, ``\u0000``).
    # The fixed allowance covers field names, status, elapsed time, and the marker.
    return 6 * (
        max_diagnostics_bytes + len(DIAGNOSTICS_TRUNCATION_MARKER.encode('utf-8'))
    ) + 1024


MAX_CONTAINER_RESULT_BYTES = _max_container_result_bytes(MAX_DIAGNOSTICS_BYTES)


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
        try:
            with tempfile.TemporaryDirectory(prefix='solvenet-container-') as directory:
                path = Path(directory)
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
                    completed = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                               timeout=self.container_config.deadline_seconds, check=False)
                except subprocess.TimeoutExpired:
                    # Stop the guest before deleting its bind-mounted workspace.
                    self._remove(name)
                    raise
                if completed.returncode != 0:
                    diagnostics = f'Container exited with code {completed.returncode}; check Docker and image {self.image}'
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
            diagnostics = f'Container verifier error: {error}'
        finally:
            # Killing the Docker CLI alone does not stop the container.
            self._remove(name)
        if elapsed_ms is None:
            elapsed_ms = round((time.monotonic() - started) * 1000)
        return VerificationResult(status, diagnostics, elapsed_ms)

    @staticmethod
    def _remove(name):
        try:
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass


def main():
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
