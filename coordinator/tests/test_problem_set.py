import json
import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from solvenet.problem_set import DEFAULT_SET, EXPERIMENT_SETS, load, load_experiment_set, main
from solvenet.verifier import LeanVerifier, VerificationStatus


ROOT = Path(__file__).resolve().parents[2]


class ProblemSetTests(unittest.TestCase):
    def test_submit_one_fixture_and_record_manifest(self):
        class Reply:
            def __enter__(self):
                return io.BytesIO(b'{"run_id":"example-run"}')

            def __exit__(self, *_):
                pass

        requests = []

        def respond(request, timeout):
            requests.append(request)
            return Reply()

        with tempfile.TemporaryDirectory() as directory, patch(
            "solvenet.problem_set.urllib.request.urlopen", side_effect=respond
        ), patch.object(sys, "argv", ["solvenet-problems", "--id", "and-swap",
                                    "--output", str(Path(directory) / "runs.json")]), redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
            manifest = json.loads((Path(directory) / "runs.json").read_text())
        self.assertEqual(manifest["runs"], [{"problem_id": "and-swap", "run_id": "example-run"}])
        self.assertEqual(manifest["environment"], "leanprover/lean4:v4.19.0")
        self.assertEqual(manifest["sha256"], load().sha256)
        self.assertEqual(len(requests), 1)
        payload = json.loads(requests[0].data)
        self.assertEqual(payload["statement"], load().problems[0].statement)
        self.assertNotIn("reference_proof", payload)

    def test_load_and_public_submission(self):
        fixture = load()
        self.assertGreaterEqual(len(fixture.problems), 10)
        self.assertEqual(fixture.environment, (ROOT / "lean" / "lean-toolchain").read_text().strip())
        for problem in fixture.problems:
            with self.subTest(id=problem.id):
                body = json.dumps(problem.submission(model="scripted", attempts=1,
                                                     max_repairs=0, max_output_tokens=256))
                self.assertNotIn("reference_proof", body)
                self.assertNotIn(problem.reference_proof, body)

    def test_duplicate_and_malformed_fields(self):
        original = json.loads(DEFAULT_SET.read_text())
        cases = [
            (lambda data: data["problems"].append(data["problems"][0].copy()), "Duplicate problem id"),
            (lambda data: data["problems"][0].update(imports=["Init\naxiom bad : False"]), "Malformed imports"),
            (lambda data: data["problems"][0].update(statement=": True\naxiom bad : False"), "Malformed statement"),
        ]
        for mutation, error in cases:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                data = json.loads(json.dumps(original))
                mutation(data)
                path = Path(directory) / "fixture.json"
                path.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, error):
                    load(path)

    def test_checked_in_experiment_set_selection(self):
        for (set_id, version), path in EXPERIMENT_SETS.items():
            with self.subTest(set_id=set_id):
                fixture = load(path)
                self.assertEqual(load_experiment_set(set_id, version, fixture.sha256), fixture)
                with self.assertRaisesRegex(ValueError, "Unknown or changed"):
                    load_experiment_set(set_id, version, "0" * 64)
        for set_id, version in (("unknown", 1), ("challenge", 2), ("challenge", True)):
            with self.assertRaisesRegex(ValueError, "Unknown or changed"):
                load_experiment_set(set_id, version, "0" * 64)

    def test_challenge_references_with_real_lean(self):
        self.assertIsNotNone(shutil.which("lake"), "Put the pinned Lean lake on PATH")
        fixture = load(EXPERIMENT_SETS[("challenge", 1)])
        self.assertEqual(len(fixture.problems), 12)
        self.assertEqual(fixture.environment, (ROOT / "lean" / "lean-toolchain").read_text().strip())
        verifier = LeanVerifier(ROOT / "lean")
        for problem in fixture.problems:
            with self.subTest(id=problem.id):
                result = verifier.verify(problem.statement, problem.reference_proof,
                                         imports=problem.imports)
                self.assertEqual(result.status, VerificationStatus.VERIFIED, result.diagnostics)


@unittest.skipUnless(shutil.which("lake"), "Lake is not installed")
class ReferenceProofTests(unittest.TestCase):
    def test_every_proof_with_real_lean(self):
        fixture = load()
        self.assertEqual(fixture.environment, (ROOT / "lean" / "lean-toolchain").read_text().strip())
        verifier = LeanVerifier(ROOT / "lean")
        for problem in fixture.problems:
            with self.subTest(id=problem.id):
                result = verifier.verify(problem.statement, problem.reference_proof,
                                         imports=problem.imports)
                self.assertEqual(result.status, VerificationStatus.VERIFIED, result.diagnostics)

    def test_lean_rejects_malformed_semantic_input(self):
        verifier = LeanVerifier(ROOT / "lean")
        for statement, imports in ((": MissingFixtureType", ("Init",)),
                                   (": True", ("NotAnInstalledModule",))):
            with self.subTest(statement=statement, imports=imports):
                result = verifier.verify(statement, "trivial", imports=imports)
                self.assertEqual(result.status, VerificationStatus.VERIFIER_ERROR)
