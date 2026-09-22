"""Docker transport for the local Lean verifier; no Docker socket in the guest."""

import json
import os
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .verifier import LeanVerifier, VerificationResult, VerificationStatus


class ContainerVerifier:
    def __init__(self, image='solvenet-verifier:local', timeout_seconds=30):
        self.image = image
        self.timeout_seconds = timeout_seconds

    def verify(self, statement, candidate, *, imports=('Init',)):
        started = time.monotonic()
        name = 'solvenet-verify-' + uuid4().hex
        status = VerificationStatus.VERIFIER_ERROR
        diagnostics = 'Container verifier did not complete'
        try:
            with tempfile.TemporaryDirectory(prefix='solvenet-container-') as directory:
                path = Path(directory)
                (path / 'request.json').write_text(json.dumps({'statement': statement, 'candidate': candidate, 'imports': imports}))
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
                        raw = output.read(128 * 1024 + 1)
                    if len(raw) > 128 * 1024:
                        raise ValueError('Container result exceeded size limit')
                    result = json.loads(raw)
                    status = VerificationStatus(result['status'])
                    diagnostics = result['diagnostics']
                    if not isinstance(diagnostics, str):
                        raise ValueError('Invalid container diagnostics')
        except subprocess.TimeoutExpired:
            status = VerificationStatus.TIMEOUT
            diagnostics = 'Container verification deadline exceeded'
        except (OSError, ValueError, KeyError) as error:
            status = VerificationStatus.VERIFIER_ERROR
            diagnostics = f'Container verifier error: {error}'
        finally:
            # Killing the Docker CLI alone does not stop the container.
            self._remove(name)
        return VerificationResult(status, diagnostics, round((time.monotonic() - started) * 1000))

    @staticmethod
    def _remove(name):
        try:
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass


def main():
    request = json.loads(Path('/work/request.json').read_text())
    verifier = LeanVerifier(Path('/opt/solvenet/lean'), command=('lean',))
    result = verifier.verify(request['statement'], request['candidate'], imports=request['imports'])
    Path('/work/result.json').write_text(json.dumps(asdict(result)))


if __name__ == '__main__':
    main()
