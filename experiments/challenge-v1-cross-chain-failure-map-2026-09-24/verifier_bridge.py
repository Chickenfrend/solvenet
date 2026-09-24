"""Small JSON/stdin bridge to the coordinator's restricted container verifier."""

import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "coordinator" / "src"))
from solvenet.sandbox import ContainerVerifier  # noqa: E402


def main():
    request = json.load(sys.stdin)
    verifier = ContainerVerifier(image="solvenet-verifier:homelab")
    if request["action"] == "verify":
        result = verifier.verify(request["statement"], request["candidate"], imports=request["imports"])
        print(json.dumps(asdict(result)))
        return
    if request["action"] != "readiness":
        raise ValueError("unknown action")
    readiness = verifier.readiness()
    if not readiness.ready:
        print(json.dumps({"ready": False, "error": readiness.diagnostics}))
        return
    image = subprocess.check_output(["docker", "image", "inspect", "--format={{.Id}}", verifier.image], text=True).strip()
    command = verifier._docker_base_command("solvenet-experiment-version") + ["--entrypoint", "lean", verifier.image, "--version"]
    version = subprocess.check_output(command, text=True, timeout=verifier.timeout_seconds).strip()
    files = ["coordinator/src/solvenet/verifier.py", "coordinator/src/solvenet/sandbox.py", "Dockerfile.verifier", "lean/lean-toolchain", "lean/lakefile.toml"]
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}
    print(json.dumps({"ready": True, "image": image, "version": version, "files": hashes,
                      "config": {"image": verifier.image, "lean_timeout_seconds": verifier.verifier_config.timeout_seconds,
                                 "container_deadline_seconds": verifier.container_config.deadline_seconds,
                                 "resources": asdict(verifier.resources)}}))


if __name__ == "__main__":
    main()
