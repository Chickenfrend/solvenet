"""Versioned local Lean fixtures and a small run-submission command."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path


DEFAULT_SET = Path(__file__).resolve().parents[3] / "problems" / "core-v1.json"
# Only these checked-in files can be selected through the experiment API.
EXPERIMENT_SETS = {
    ("core", 1): DEFAULT_SET,
    ("challenge", 1): DEFAULT_SET.parent / "challenge-v1.json",
}
MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_'.]*\Z")
ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


@dataclass(frozen=True)
class Problem:
    id: str
    title: str
    statement: str
    imports: tuple[str, ...]
    reference_proof: str
    category: str | None = None
    description: str | None = None
    difficulty: str | None = None

    def submission(self, *, model: str, attempts: int, max_repairs: int,
                   max_output_tokens: int) -> dict:
        """Only public problem fields cross the HTTP/model boundary."""
        return {
            "statement": self.statement,
            "imports": list(self.imports),
            "model": model,
            "attempts": attempts,
            "max_repairs": max_repairs,
            "max_output_tokens": max_output_tokens,
        }


@dataclass(frozen=True)
class ProblemSet:
    set_id: str
    version: int
    environment: str
    sha256: str
    problems: tuple[Problem, ...]


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{field} must be a nonempty string without NUL bytes")
    return value


def load(path: Path = DEFAULT_SET) -> ProblemSet:
    """Validate fixture structure; Lean elaboration is tested separately."""
    content = Path(path).read_bytes()
    data = json.loads(content.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Problem set must be an object")
    set_id = _nonempty(data.get("set_id"), "set_id")
    if not ID.fullmatch(set_id):
        raise ValueError("Invalid set_id")
    version = data.get("version")
    if type(version) is not int or version < 1:
        raise ValueError("version must be a positive integer")
    environment = _nonempty(data.get("environment"), "environment")
    if not re.fullmatch(r"leanprover/lean4:v[0-9]+\.[0-9]+\.[0-9]+", environment):
        raise ValueError("environment must pin a Lean release")
    entries = data.get("problems")
    if not isinstance(entries, list) or not entries:
        raise ValueError("problems must be a nonempty list")
    problems = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each problem must be an object")
        problem_id = _nonempty(entry.get("id"), "id")
        if not ID.fullmatch(problem_id):
            raise ValueError(f"Invalid problem id: {problem_id}")
        if problem_id in seen:
            raise ValueError(f"Duplicate problem id: {problem_id}")
        seen.add(problem_id)
        statement = _nonempty(entry.get("statement"), f"{problem_id}.statement")
        # The verifier inserts the statement after a theorem/axiom name.
        # Disallow extra commands here; elaboration catches invalid Lean types.
        if ("\n" in statement or "\r" in statement or not statement.strip().startswith(("(", ":"))
                or " : " not in statement and not statement.strip().startswith(": ")):
            raise ValueError(f"Malformed statement: {problem_id}")
        imports = entry.get("imports")
        if (not isinstance(imports, list) or not imports or
                any(not isinstance(m, str) or not MODULE.fullmatch(m) for m in imports)):
            raise ValueError(f"Malformed imports: {problem_id}")
        optional = {}
        for field in ("category", "description", "difficulty"):
            if field in entry:
                optional[field] = _nonempty(entry[field], f"{problem_id}.{field}")
        problems.append(Problem(
            id=problem_id, title=_nonempty(entry.get("title"), f"{problem_id}.title"),
            statement=statement, imports=tuple(imports),
            reference_proof=_nonempty(entry.get("reference_proof"), f"{problem_id}.reference_proof"),
            **optional,
        ))
    return ProblemSet(set_id, version, environment, hashlib.sha256(content).hexdigest(),
                      tuple(problems))


def load_experiment_set(set_id: object, version: object, sha256: object) -> ProblemSet:
    """Select a known local fixture by identity, never by a client-supplied path."""
    if type(set_id) is not str or type(version) is not int:
        raise ValueError("Unknown or changed checked-in problem set/version/hash")
    path = EXPERIMENT_SETS.get((set_id, version))
    if path is None:
        raise ValueError("Unknown or changed checked-in problem set/version/hash")
    fixture = load(path)
    if (fixture.set_id, fixture.version, fixture.sha256) != (set_id, version, sha256):
        raise ValueError("Unknown or changed checked-in problem set/version/hash")
    return fixture


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit versioned Lean fixtures to a local coordinator")
    parser.add_argument("--set", type=Path, default=DEFAULT_SET)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--id", action="append", help="Problem ID (repeatable; default: all)")
    parser.add_argument("--model", default="scripted")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--max-repairs", type=int, default=0)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--output", type=Path, help="Save the run-to-fixture manifest as JSON")
    args = parser.parse_args()
    fixture = load(args.set)
    selected = set(args.id or (p.id for p in fixture.problems))
    unknown = selected - {p.id for p in fixture.problems}
    if unknown:
        parser.error(f"Unknown problem ID(s): {', '.join(sorted(unknown))}")
    runs = []
    for problem in fixture.problems:
        if problem.id not in selected:
            continue
        payload = problem.submission(model=args.model, attempts=args.attempts,
                                     max_repairs=args.max_repairs,
                                     max_output_tokens=args.max_output_tokens)
        request = urllib.request.Request(
            args.url.rstrip("/") + "/v1/runs",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            run_id = json.load(response)["run_id"]
        runs.append({"problem_id": problem.id, "run_id": run_id})
    manifest = {"set_id": fixture.set_id, "version": fixture.version,
                "environment": fixture.environment, "sha256": fixture.sha256,
                "runs": runs}
    output = json.dumps(manifest, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
