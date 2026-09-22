"""Verify an untrusted proof body against a fixed Lean theorem statement."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Sequence


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    VERIFIER_ERROR = "verifier_error"


@dataclass(frozen=True)
class VerificationResult:
    status: VerificationStatus
    diagnostics: str
    elapsed_ms: int

    @property
    def verified(self) -> bool:
        return self.status is VerificationStatus.VERIFIED


class LeanVerifier:
    """Run Lean in a pinned Lake project.

    This class limits wall-clock time and output size, but a subprocess alone is
    not a security sandbox. Production deployments must also isolate the process
    from the network, credentials, and the host filesystem.
    """

    DEFAULT_ALLOWED_AXIOMS = frozenset(
        {"propext", "Classical.choice", "Quot.sound"}
    )

    def __init__(
        self,
        project_dir: Path,
        *,
        command: Sequence[str] = ("lake", "env", "lean"),
        timeout_seconds: float = 10,
        max_diagnostics_bytes: int = 64 * 1024,
        allowed_axioms: frozenset[str] = DEFAULT_ALLOWED_AXIOMS,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds
        self.max_diagnostics_bytes = max_diagnostics_bytes
        self.allowed_axioms = allowed_axioms
        if not self.command or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("A command and a positive finite timeout are required")
        if max_diagnostics_bytes <= 0:
            raise ValueError("The diagnostic limit must be positive")

    def verify(
        self,
        statement: str,
        candidate: str,
        *,
        imports: Sequence[str] = ("Init",),
    ) -> VerificationResult:
        started = time.monotonic()

        problem = self._validate_input(statement, candidate, imports)
        if problem is not None:
            return self._result(VerificationStatus.REJECTED, problem, started)

        try:
            with tempfile.TemporaryDirectory(prefix="solvenet-verify-") as temp_dir:
                source_path = Path(temp_dir) / "Candidate.lean"
                receipt = Path(temp_dir) / "accepted"
                source = self._build_source(statement, candidate, imports, receipt=receipt)
                source_path.write_text(source, encoding="utf-8")
                # Check the toolchain, imports and trusted statement separately.
                # Their failures are infrastructure/problem errors, not bad proofs.
                preflight = Path(temp_dir) / "Preflight.lean"
                preflight.write_text(
                    self._build_source(statement, None, imports), encoding="utf-8"
                )
                status, diagnostics = self._run(preflight, started)
                if status is not VerificationStatus.VERIFIED:
                    return self._result(
                        VerificationStatus.VERIFIER_ERROR,
                        f"Environment/problem preflight failed: {diagnostics}", started,
                    )
                status, diagnostics = self._run(source_path, started)
                if status is VerificationStatus.VERIFIED and not receipt.is_file():
                    status = VerificationStatus.VERIFIER_ERROR
                    diagnostics += "\nLean exited without completing the verification checks"
        except (OSError, ValueError) as error:
            return self._result(
                VerificationStatus.VERIFIER_ERROR,
                f"Could not run Lean: {error}",
                started,
            )

        return self._result(status, diagnostics, started)

    def _run(self, path: Path, started: float) -> tuple[VerificationStatus, str]:
        output = bytearray()
        with subprocess.Popen(
            [*self.command, str(path)], cwd=self.project_dir,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True,
        ) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while selector.get_map():
                        remaining = self.timeout_seconds - (time.monotonic() - started)
                        if remaining <= 0:
                            return VerificationStatus.TIMEOUT, self._decode_output(output) + "\nLean timed out"
                        for key, _ in selector.select(min(remaining, 0.1)):
                            chunk = os.read(key.fileobj.fileno(), 8192)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            available = self.max_diagnostics_bytes - len(output)
                            output.extend(chunk[:available])
                            if len(chunk) > available:
                                return VerificationStatus.REJECTED, self._decode_output(output) + "\n[output limit exceeded]"
                    remaining = self.timeout_seconds - (time.monotonic() - started)
                    try:
                        code = process.wait(timeout=max(0, remaining))
                    except subprocess.TimeoutExpired:
                        return VerificationStatus.TIMEOUT, self._decode_output(output) + "\nLean timed out"
                status = (VerificationStatus.VERIFIED if code == 0 else
                          VerificationStatus.REJECTED if code == 1 else
                          VerificationStatus.VERIFIER_ERROR)
                diagnostics = self._decode_output(output)
                if status is VerificationStatus.VERIFIER_ERROR:
                    diagnostics = f"Lean exited abnormally ({code})\n{diagnostics}"
                return status, diagnostics
            finally:
                # Lake may spawn Lean; terminate the whole process group, including
                # descendants retaining the output pipe after the parent exits.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()

    def _validate_input(
        self, statement: str, candidate: str, imports: Sequence[str]
    ) -> str | None:
        if not statement.strip():
            return "The theorem statement is empty"
        if not candidate.strip():
            return "The candidate proof is empty"
        if "\x00" in statement or "\x00" in candidate:
            return "Lean input contains a NUL byte"
        if not imports:
            return "At least one Lean import is required"
        for module in imports:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_'.]*", module):
                return f"Invalid Lean import name: {module!r}"
        return None

    def _build_source(
        self, statement: str, candidate: str | None, imports: Sequence[str],
        *, receipt: Path | None = None,
    ) -> str:
        import_lines = "\n".join(f"import {module}" for module in dict.fromkeys(["Lean", *imports]))
        source = (
            f"{import_lines}\n\n"
            "set_option autoImplicit false\n"
            "set_option Elab.async false\n"
            f"axiom SolveNetExpected {statement.strip()}\n"
        )
        if candidate is None:
            return source
        proof = "\n".join(f"  {line}" for line in candidate.splitlines())
        declaration = f"theorem SolveNetCandidate {statement.strip()} := by\n{proof}\n"
        # JSON string escaping with literal Unicode is also valid Lean escaping.
        quoted = json.dumps(declaration, ensure_ascii=False)
        allowed = ", ".join(json.dumps(n) for n in sorted(self.allowed_axioms))
        return source + f'''
open Lean Elab Command in
run_cmd do
  let expected ← getConstInfo `SolveNetExpected
  let stx ← match Parser.runParserCategory (← getEnv) `command {quoted} with
    | .ok stx => pure stx
    | .error error => throwError "{{error}}"
  elabCommand stx
  let actual ← getConstInfo `SolveNetCandidate
  unless actual matches .thmInfo _ do
    throwError "Candidate must be a theorem"
  unless actual.type == expected.type && actual.levelParams == expected.levelParams do
    throwError "Candidate theorem type differs from the original problem"
  let allowed : List String := [{allowed}]
  for axiomName in (← collectAxioms `SolveNetCandidate) do
    unless allowed.contains axiomName.toString do
      throwError "Candidate uses disallowed axiom: {{axiomName}}"
  liftIO <| IO.FS.writeFile {json.dumps(str(receipt))} "accepted"
'''

    def _decode_output(self, output: bytes | str | None) -> str:
        if output is None:
            return ""
        if isinstance(output, str):
            output = output.encode("utf-8", errors="replace")
        clipped = output[: self.max_diagnostics_bytes]
        text = clipped.decode("utf-8", errors="replace").strip()
        if len(output) > len(clipped):
            text += "\n[diagnostics truncated]"
        return text

    @staticmethod
    def _result(
        status: VerificationStatus, diagnostics: str, started: float
    ) -> VerificationResult:
        return VerificationResult(
            status=status,
            diagnostics=diagnostics,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a Lean proof body")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--statement", required=True, help="Text after the theorem name")
    parser.add_argument("--candidate-file", type=Path, required=True)
    parser.add_argument("--import", dest="imports", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)

    verifier = LeanVerifier(args.project, timeout_seconds=args.timeout)
    result = verifier.verify(
        args.statement,
        args.candidate_file.read_text(encoding="utf-8"),
        imports=args.imports or ("Init",),
    )
    print(json.dumps(asdict(result)))
    return 0 if result.verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
