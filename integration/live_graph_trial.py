"""Explicitly opt-in, capped trial against an already installed local Ollama model."""

import argparse
import json
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen

from solvenet.server import Coordinator, make_server
from solvenet.store import Store
from solvenet.verifier import LeanVerifier
from test_group_collaboration import api
from test_site_worker_lean import ROOT, serving


def execution_metadata():
    """Bounded read-only inventory; hardware presence does not assert model placement."""
    metadata = dict(collected_at_unix=time.time(), platform=platform.platform(),
        cpu=dict(model=None, logical_cpus=os.cpu_count(), unavailable=None),
        ram=dict(total_bytes=None, unavailable=None),
        gpu=dict(devices=None, unavailable=None), ollama=dict(version=None, unavailable=None),
        execution_device=dict(value=None, unavailable='Worker/provider does not report per-call CPU/GPU placement'),
        resource_usage=dict(peak_ram_bytes=None, peak_vram_bytes=None,
            unavailable='Per-call peak RAM/VRAM not measured by this trial'),
        generation_settings=dict(num_ctx=8192, num_predict=dict(plan=512, finding=512, critique=512, proof=2048),
            temperature=None, seed=None, top_p=None, top_k=None, num_gpu=None, num_thread=None,
            unspecified='Not sent by this trial; effective model/server defaults unavailable'))
    try:
        with Path('/proc/cpuinfo').open() as source:
            cpuinfo = source.read(65536)
        metadata['cpu']['model'] = next(line.split(':', 1)[1].strip() for line in cpuinfo.splitlines()
            if line.startswith('model name'))
    except (OSError, StopIteration) as error:
        metadata['cpu']['unavailable'] = str(error) or 'No model name in /proc/cpuinfo'
    try:
        with Path('/proc/meminfo').open() as source:
            meminfo = source.read(8192)
        metadata['ram']['total_bytes'] = int(next(line.split()[1] for line in meminfo.splitlines()
            if line.startswith('MemTotal:'))) * 1024
    except (OSError, StopIteration, ValueError) as error:
        metadata['ram']['unavailable'] = str(error) or 'No MemTotal in /proc/meminfo'
    if shutil.which('nvidia-smi'):
        try:
            result = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total,driver_version',
                '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=3, check=True)
            metadata['gpu']['devices'] = [dict(name=name.strip(), vram_total_mib=int(memory.strip()),
                driver_version=driver.strip()) for name, memory, driver in
                (line.split(',') for line in result.stdout.splitlines()[:16])]
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            metadata['gpu']['unavailable'] = str(error)[:512]
    else:
        metadata['gpu']['unavailable'] = 'nvidia-smi unavailable; GPU/VRAM inventory not collected'
    try:
        with urlopen('http://127.0.0.1:11434/api/version', timeout=3) as response:
            metadata['ollama']['version'] = json.loads(response.read(4096))['version']
    except (OSError, ValueError, KeyError) as error:
        metadata['ollama']['unavailable'] = str(error)[:512]
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True, help='exact installed Ollama tag; no downloads')
    parser.add_argument('--worker', required=True, type=Path, help='compiled Go worker')
    parser.add_argument('--output', required=True, type=Path, help='new local JSON observation file')
    args = parser.parse_args()
    with urlopen('http://127.0.0.1:11434/api/tags', timeout=3) as response:
        available = json.load(response)['models']
    installed = next((m for m in available if m['name'] == args.model), None)
    if installed is None:
        parser.error('model is not already installed on local Ollama')
    metadata = execution_metadata()
    worker_path = args.worker.resolve(strict=True)
    # Refuse to overwrite an earlier observation.
    with args.output.open('x') as output, tempfile.TemporaryDirectory(prefix='solvenet-g6-live-') as temp:
        coordinator = Coordinator(Store(Path(temp) / 'state.db'), LeanVerifier(ROOT / 'lean', timeout_seconds=10))
        readiness = coordinator.verifier.readiness()
        if not readiness.ready:
            raise RuntimeError(readiness.diagnostics)
        stop = threading.Event()
        with serving(make_server(coordinator, ('127.0.0.1', 0))) as url:
            scheduler = threading.Thread(target=coordinator.loop, args=(stop,))
            scheduler.start()
            with (Path(temp) / 'worker.log').open('w+') as log:
                worker = None
                try:
                    worker = subprocess.Popen([str(worker_path), '-coordinator', url, '-provider', 'ollama',
                        '-model', args.model, '-ollama-url', 'http://127.0.0.1:11434', '-ollama-context', '8192',
                        '-id', 'g6-local-trial'], stdout=log, stderr=subprocess.STDOUT,
                        env={**os.environ, 'OPENAI_API_KEY': '', 'OPENAI_API_KEY_FILE': ''})
                    model = 'ollama/' + args.model
                    created = api(url, '/v1/groups', dict(request_key='g6-live-local', mode='graph',
                        statement=': True ∧ (True ∧ True)', imports=['Init'], environment='local-pinned-lean',
                        max_work=12, deadline=time.time() + 75,
                        models={r: model for r in ('planner', 'investigator', 'critic', 'synthesizer')},
                        model_capabilities={model: dict(cost=3, context_tokens=8192, context_bytes=8192)},
                        graph_limits=dict(planning_calls=1, verification_operations=2,
                            lean_elapsed_ms=20000, retries=0)))
                    end = time.monotonic() + 80
                    while True:
                        snapshot = api(url, '/v1/groups/' + created['id'])
                        if snapshot['loop']['phase'] == 'stopped' or time.monotonic() >= end:
                            break
                        if worker.poll() is not None:
                            raise RuntimeError('worker exited before trial completed')
                        time.sleep(.5)
                    run = snapshot['group']['run']
                    run = api(url, '/v1/runs/' + run['run_id']) if run else None
                    json.dump(dict(installed_model=installed, execution_metadata=metadata,
                        snapshot=snapshot, run=run,
                        operator_cutoff=snapshot['loop']['phase'] != 'stopped'), output, indent=2)
                    print(json.dumps(dict(loop=snapshot['loop'], cost=snapshot['group']['cost']), indent=2))
                finally:
                    if worker is not None:
                        worker.terminate()
                        try:
                            worker.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            worker.kill()
                            worker.wait(timeout=5)
                    stop.set()
                    scheduler.join(timeout=15)
                    if scheduler.is_alive():
                        raise RuntimeError('scheduler did not drain')


if __name__ == '__main__':
    main()
