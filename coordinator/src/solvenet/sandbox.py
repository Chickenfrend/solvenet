"""Docker transport for the local Lean verifier; no Docker socket in the guest."""

import json
import os
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .verifier import (
    DIAGNOSTICS_TRUNCATION_MARKER,
    MAX_DIAGNOSTICS_BYTES,
    LeanVerifier,
    VerificationResult,
    VerificationStatus,
    truncate_diagnostics,
)


# JSON control characters may occupy six bytes (for example, ``\u0000``).
# The fixed allowance covers field names, status, elapsed time, and the marker.
MAX_CONTAINER_RESULT_BYTES = (
    6 * (MAX_DIAGNOSTICS_BYTES + len(DIAGNOSTICS_TRUNCATION_MARKER.encode("utf-8")))
    + 1024
)


def _encode_result(result: VerificationResult) -> bytes:
    fields = asdict(result)
    fields['diagnostics'] = truncate_diagnostics(fields['diagnostics'])
    encoded = json.dumps(
        fields, ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8')
    if len(encoded) > MAX_CONTAINER_RESULT_BYTES:
        raise ValueError('Container result exceeded size limit')
    return encoded


def _decode_result(raw: bytes) -> VerificationResult:
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
    return VerificationResult(status, truncate_diagnostics(diagnostics), elapsed_ms)


class ContainerVerifier:
    def __init__(self, image='solvenet-verifier:local', timeout_seconds=30):
        self.image = image
        self.timeout_seconds = timeout_seconds

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
                        {'statement': statement, 'candidate': candidate, 'imports': imports},
                        ensure_ascii=False,
                    ),
                    encoding='utf-8',
                )
                command = [
                    'docker', 'run', '--rm', '--pull=never', '--name', name,
                    '--network=none', '--read-only', '--cap-drop=ALL',
                    '--security-opt=no-new-privileges', '--pids-limit=64',
                    '--memory=1g', '--memory-swap=1g', '--cpus=1',
                    '--ulimit', 'fsize=1048576:1048576',
                    '--user', f'{os.getuid()}:{os.getgid()}',
                    '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m,mode=1777',
                    '--mount', f'type=bind,src={path},dst=/work', self.image,
                ]
                try:
                    completed = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                               timeout=self.timeout_seconds, check=False)
                except subprocess.TimeoutExpired:
                    # Stop the guest before deleting its bind-mounted workspace.
                    self._remove(name)
                    raise
                if completed.returncode != 0:
                    diagnostics = f'Container exited with code {completed.returncode}; check Docker and image {self.image}'
                else:
                    with (path / 'result.json').open('rb') as output:
                        raw = output.read(MAX_CONTAINER_RESULT_BYTES + 1)
                    if len(raw) > MAX_CONTAINER_RESULT_BYTES:
                        raise ValueError('Container result exceeded size limit')
                    result = _decode_result(raw)
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
    verifier = LeanVerifier(Path('/opt/solvenet/lean'), command=('lean',))
    result = verifier.verify(request['statement'], request['candidate'], imports=request['imports'])
    Path('/work/result.json').write_bytes(_encode_result(result))


if __name__ == '__main__':
    main()
