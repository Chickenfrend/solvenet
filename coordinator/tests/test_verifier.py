from __future__ import annotations

import shutil
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from solvenet.verifier import LeanVerifier, VerificationStatus


ROOT = Path(__file__).resolve().parents[2]
FAKE_LEAN = Path(__file__).with_name("fake_lean.py")


class LeanVerifierUnitTests(unittest.TestCase):
    def verifier(self, **kwargs: object) -> LeanVerifier:
        return LeanVerifier(
            ROOT / "lean",
            command=(sys.executable, str(FAKE_LEAN)),
            **kwargs,
        )

    def test_accepts_successful_lean_run(self) -> None:
        result = self.verifier().verify(": True", "exact True.intro")
        self.assertEqual(result.status, VerificationStatus.VERIFIED)

    def test_reports_invalid_proof(self) -> None:
        result = self.verifier().verify(": True", "exact FAKE_INVALID")
        self.assertEqual(result.status, VerificationStatus.REJECTED)
        self.assertIn("invalid proof", result.diagnostics)

    def test_times_out(self) -> None:
        result = self.verifier(timeout_seconds=0.3).verify(
            ": True", "exact True.intro -- FAKE_TIMEOUT"
        )
        self.assertEqual(result.status, VerificationStatus.TIMEOUT)

    def test_missing_lean_is_a_verifier_error(self) -> None:
        verifier = LeanVerifier(ROOT / "lean", command=("does-not-exist-solvenet",))
        result = verifier.verify(": True", "exact True.intro")
        self.assertEqual(result.status, VerificationStatus.VERIFIER_ERROR)

    def test_rejects_import_injection(self) -> None:
        result = self.verifier().verify(
            ": True", "exact True.intro", imports=("Init\naxiom bad : False",)
        )
        self.assertEqual(result.status, VerificationStatus.REJECTED)

    def test_output_is_bounded(self) -> None:
        result = self.verifier(max_diagnostics_bytes=1024).verify(": True", "FAKE_FLOOD")
        self.assertEqual(result.status, VerificationStatus.REJECTED)
        self.assertIn("output limit exceeded", result.diagnostics)
        self.assertLess(len(result.diagnostics), 1100)

    def test_crash_is_verifier_error(self) -> None:
        result = self.verifier().verify(": True", "FAKE_CRASH")
        self.assertEqual(result.status, VerificationStatus.VERIFIER_ERROR)


@unittest.skipUnless(shutil.which("lake"), "Lake is not installed")
class LeanVerifierIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = LeanVerifier(ROOT / "lean")

    def test_valid_proof(self) -> None:
        result = self.verifier.verify(": True", "exact True.intro")
        self.assertEqual(result.status, VerificationStatus.VERIFIED, result.diagnostics)

    def test_invalid_proof(self) -> None:
        result = self.verifier.verify(": False", "exact True.intro")
        self.assertEqual(result.status, VerificationStatus.REJECTED)

    def test_cannot_replace_theorem(self) -> None:
        result = self.verifier.verify(
            ": True", "exact True.intro\naxiom injected : False"
        )
        self.assertEqual(result.status, VerificationStatus.REJECTED)
        self.assertIn("expected end of input", result.diagnostics)

    def test_placeholders_and_spoofed_diagnostics(self) -> None:
        for proof in ("sorry", "admit", "exact sorryAx False true",
                      'trace "does not depend on any axioms"\nexact sorryAx False true'):
            with self.subTest(proof=proof):
                result = self.verifier.verify(": False", proof)
                self.assertEqual(result.status, VerificationStatus.REJECTED)
                self.assertIn("disallowed axiom: sorryAx", result.diagnostics)

    def test_comments_can_mention_sorry(self) -> None:
        result = self.verifier.verify(": True", "-- no sorry needed\nexact True.intro")
        self.assertTrue(result.verified, result.diagnostics)

    def test_expected_declaration_cannot_supply_proof(self) -> None:
        result = self.verifier.verify(": False", "exact SolveNetExpected")
        self.assertEqual(result.status, VerificationStatus.REJECTED)
        self.assertIn("disallowed axiom: SolveNetExpected", result.diagnostics)

    def test_broken_import_is_verifier_error(self) -> None:
        result = self.verifier.verify(": True", "trivial", imports=("MissingSolveNetModule",))
        self.assertEqual(result.status, VerificationStatus.VERIFIER_ERROR)

    def test_axiom_spoofing_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            shutil.copy(ROOT / "lean" / "lean-toolchain", project)
            shutil.copy(ROOT / "lean" / "lakefile.toml", project)
            (project / "Unsound.lean").write_text("axiom unsound : False\n")
            subprocess.run(
                ["lake", "env", "lean", "-o", "Unsound.olean", "Unsound.lean"],
                cwd=project, check=True, capture_output=True,
            )
            with patch.dict(os.environ, {"LEAN_PATH": directory}):
                result = LeanVerifier(project).verify(
                    ": False", 'trace "does not depend on any axioms"\nexact unsound',
                    imports=("Unsound",),
                )
            self.assertEqual(result.status, VerificationStatus.REJECTED)
            self.assertIn("disallowed axiom: unsound", result.diagnostics)

    def test_parameterized_theorem(self) -> None:
        result = self.verifier.verify("(n : Nat) : n + 0 = n", "rfl")
        self.assertTrue(result.verified, result.diagnostics)

    def test_early_process_exit_is_not_acceptance(self) -> None:
        result = self.verifier.verify(": False", "run_tac (IO.Process.exit 0 : IO Unit)")
        self.assertEqual(result.status, VerificationStatus.VERIFIER_ERROR)
        self.assertIn("without completing", result.diagnostics)

    def test_allows_standard_lean_axiom(self) -> None:
        result = self.verifier.verify(
            "(p : Prop) : Nonempty p → p",
            "intro h\nexact Classical.choice h",
        )
        self.assertEqual(result.status, VerificationStatus.VERIFIED, result.diagnostics)


if __name__ == "__main__":
    unittest.main()
