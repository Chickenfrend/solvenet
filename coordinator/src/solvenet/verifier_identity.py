"""Bounded metadata identity of the effective local Lean execution environment."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

MAX_PROBE_BYTES = 16 * 1024
MAX_ROOTS = 128
MAX_ENTRIES = 100_000
MAX_SCAN_SECONDS = 5
ENVIRONMENT_KEYS = (
    "LEAN_PATH",
    "LEAN_SRC_PATH",
    "LEAN_SYSROOT",
    "ELAN_TOOLCHAIN",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "LD_PRELOAD",
)


def probe(command, project):
    raw = subprocess.run(
        command, cwd=project, capture_output=True, timeout=5, check=True
    ).stdout
    if len(raw) > MAX_PROBE_BYTES:
        raise ValueError("Lean identity probe exceeded its output bound")
    return raw.decode().strip()


def metadata(path):
    stat = path.stat()
    return [
        str(path),
        str(path.resolve()),
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    ]


def preload_libraries(value, project):
    """Resolve explicit Linux preload paths without guessing loader soname lookup.

    LD_PRELOAD uses spaces/colons, not shell quoting. Relative paths containing
    '/' are relative to the verification cwd. Bare sonames and dynamic loader
    tokens cannot be resolved by this narrow metadata checker, so fail closed.
    """
    names = [
        name for name in re.split("[ :]+", value or "", maxsplit=MAX_ROOTS) if name
    ]
    if len(names) > MAX_ROOTS:
        raise ValueError("Preload library count exceeded identity bound")
    libraries = []
    for name in names:
        if "/" not in name or "$" in name:
            raise ValueError("Preload library needs an explicitly resolvable path")
        path = Path(name)
        if not path.is_absolute():
            path = project / path
        path.resolve(strict=True)
        if not path.is_file():
            raise ValueError("Preload path is not a library file")
        libraries.append(path)
    return libraries


def local_identity(project, command, allowed_axioms):  # noqa: C901 -- fail-closed runtime and import resolution
    """Probe through the same launcher/cwd as verification, including direct elan lean.

    The compiler's --print-prefix identifies the selected toolchain rather than
    hashing only the elan launcher. Lake's effective environment is obtained via
    `lake env` itself. Import order and external import/runtime roots are bound.
    No source, olean or runtime-library contents are read during metadata scans.
    """
    try:
        search_path = os.pathsep.join(
            str(project / (part or "."))
            if not Path(part or ".").is_absolute()
            else part
            for part in os.get_exec_path()
        )
        launcher = shutil.which(
            command[0]
            if Path(command[0]).is_absolute() or "/" not in command[0]
            else str(project / command[0]),
            path=search_path,
        )
        if not launcher:
            return None
        version = probe([*command, "--version"], project)
        prefix = Path(probe([*command, "--print-prefix"], project))
        if not version or not prefix.is_absolute():
            return None
        # The installation prefix comes from the actual running compiler, even
        # when command[0] resolves to elan and the version string is unchanged.
        lean_binary = prefix / "bin/lean"
        lean_binary.resolve(strict=True)
        library = prefix / "lib/lean"
        if not library.is_dir():
            return None
        lake = (
            len(command) >= 3
            and command[1] == "env"
            and Path(command[2]).name == "lean"
        )
        if lake:
            script = (
                "import json,os,shutil; e={k:os.environ.get(k) for k in "
                + repr(ENVIRONMENT_KEYS)
                + '}; e["_LEAN_EXECUTABLE"]=shutil.which('
                + repr(command[2])
                + "); print(json.dumps(e))"
            )
            environment = json.loads(
                probe([command[0], "env", sys.executable, "-c", script], project)
            )
        else:
            environment = {key: os.environ.get(key) for key in ENVIRONMENT_KEYS}
        if (
            not isinstance(environment, dict)
            or len(json.dumps(environment).encode()) > MAX_PROBE_BYTES
            or any(
                value is not None and not isinstance(value, str)
                for value in environment.values()
            )
        ):
            return None
        selected = Path(environment["_LEAN_EXECUTABLE"]) if lake else Path(launcher)
        if not selected.is_absolute():
            selected = project / selected
        preloads = preload_libraries(environment.get("LD_PRELOAD"), project)
        digest = hashlib.sha256()
        digest.update(
            json.dumps(
                [
                    "local-lean-composed-v3",
                    str(project),
                    command,
                    version,
                    metadata(Path(launcher)),
                    metadata(selected),
                    metadata(lean_binary),
                    environment,
                    [metadata(path) for path in preloads],
                    sorted(allowed_axioms),
                ],
                sort_keys=True,
            ).encode()
        )
        roots = [project, project / ".lake/build/lib", prefix / "lib", prefix / "bin"]
        packages = project / ".lake/packages"
        if packages.is_dir():
            children = []
            for child in packages.iterdir():
                children.append(child)
                if len(children) + len(roots) > MAX_ROOTS:
                    return None
            roots.extend(sorted(children))
        for key in (
            "LEAN_PATH",
            "LEAN_SRC_PATH",
            "LD_LIBRARY_PATH",
            "DYLD_LIBRARY_PATH",
        ):
            value = environment.get(key)
            if value is not None:
                for part in value.split(os.pathsep):
                    roots.append(Path(part or "."))
                    if len(roots) > MAX_ROOTS:
                        return None
        if environment.get("LEAN_SYSROOT"):
            roots.append(Path(environment["LEAN_SYSROOT"]) / "lib")
        for argument in command:
            if argument.startswith(("--plugin=", "--load-dynlib=")):
                roots.append(Path(argument.split("=", 1)[1]))
        if len(roots) + len(preloads) > MAX_ROOTS:
            return None
        started, entries, visited = time.monotonic(), 0, set()
        for root in roots:
            root = (project / root).absolute() if not root.is_absolute() else root
            # Root order matters for module shadowing; missing/new roots matter too.
            digest.update(
                json.dumps([str(root), str(root.resolve()), root.exists()]).encode()
            )
            todo = [root]
            while todo:
                entries += 1
                if (
                    entries > MAX_ENTRIES
                    or time.monotonic() - started > MAX_SCAN_SECONDS
                ):
                    return None
                path = todo.pop()
                if not path.exists():
                    continue
                resolved = path.resolve(strict=True)
                if resolved in visited:
                    continue
                visited.add(resolved)
                # Directory metadata also binds additions/removals and symlink changes.
                if path.is_dir():
                    digest.update(json.dumps(metadata(path)).encode())
                    children = []
                    for child in path.iterdir():
                        if (
                            len(children) + len(todo) + entries >= MAX_ENTRIES
                            or time.monotonic() - started > MAX_SCAN_SECONDS
                        ):
                            return None
                        children.append(child)
                    children.sort(reverse=True)
                    if path == project:
                        children = [p for p in children if p.name != ".lake"]
                    todo.extend(children)
                elif (
                    path.suffix
                    in {
                        ".lean",
                        ".olean",
                        ".ilean",
                        ".a",
                        ".so",
                        ".dll",
                        ".dylib",
                        ".toml",
                        ".json",
                    }
                    or ".so." in path.name
                    or path.parent == prefix / "bin"
                    or path == root
                ):
                    digest.update(json.dumps(metadata(path)).encode())
        for name in ("lean-toolchain", "lakefile.toml", "lake-manifest.json"):
            path = project / name
            digest.update(
                json.dumps([name, metadata(path) if path.exists() else None]).encode()
            )
        return "local:" + digest.hexdigest()
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ):
        return None
