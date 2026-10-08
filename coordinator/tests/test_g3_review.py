"""G3 review regressions: effective runtime binding and authoritative winners."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solvenet.server import Coordinator
from solvenet.store import Store
from solvenet.verifier import LeanVerifier, VerificationResult, VerificationStatus

ROOT = Path(__file__).resolve().parents[2]


class EffectiveIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        for name in ("lean-toolchain", "lakefile.toml", "lake-manifest.json"):
            shutil.copyfile(ROOT / "lean" / name, self.project / name)
        self.prefix = self.toolchain("toolchain-A")
        self.environment = {
            "LEAN_PATH": str(self.root / "external"),
            "_LEAN_EXECUTABLE": str(self.prefix / "bin/lean"),
        }
        (self.root / "external").mkdir()
        self.calls = []

    def toolchain(self, name):
        prefix = self.root / name
        (prefix / "bin").mkdir(parents=True)
        (prefix / "lib/lean").mkdir(parents=True)
        (prefix / "bin/lean").write_bytes(b"compiler")
        (prefix / "lib/lean/Init.olean").write_bytes(b"stdlib")
        (prefix / "lib/libLean.so").write_bytes(b"runtime")
        return prefix

    def probe(self, command, **kwargs):
        self.calls.append(command)
        if command[-1] == "--print-prefix":
            output = str(self.prefix)
        elif command[-1] == "--version":
            output = "Lean version unchanged"
        elif "-c" in command:
            output = json.dumps(self.environment)
        else:
            self.fail("Unexpected identity probe: " + repr(command))
        return subprocess.CompletedProcess(command, 0, stdout=output.encode())

    def test_direct_elan_uses_actual_same_version_toolchain_stdlib_and_runtime(self):
        verifier = LeanVerifier(
            self.project, command=(str(Path.home() / ".elan/bin/lean"),)
        )
        with patch("solvenet.verifier_identity.subprocess.run", side_effect=self.probe):
            original = verifier.artifact_identity()
            self.assertIsNotNone(original)
            for relative in ("bin/lean", "lib/lean/Init.olean", "lib/libLean.so"):
                previous = verifier.artifact_identity()
                path = self.prefix / relative
                stamp = path.stat().st_mtime_ns
                # Same size and mtime: ctime/inode metadata still binds replacement.
                replacement = path.with_name(path.name + ".new")
                replacement.write_bytes(b"x" * path.stat().st_size)
                os.utime(replacement, ns=(stamp, stamp))
                replacement.replace(path)
                self.assertNotEqual(verifier.artifact_identity(), previous, relative)
            previous = verifier.artifact_identity()
            self.prefix = self.toolchain("toolchain-B")
            self.assertNotEqual(verifier.artifact_identity(), previous)

    def test_lake_effective_external_roots_order_and_metadata_scan_bounds(self):
        external = self.root / "external/External.olean"
        external.write_bytes(b"A")
        verifier = LeanVerifier(self.project)
        with (
            patch.dict(os.environ, {"LEAN_PATH": "/unused/parent/root"}),
            patch("solvenet.verifier_identity.subprocess.run", side_effect=self.probe),
        ):
            before = verifier.artifact_identity()
            self.assertIsNotNone(before)
            selected = self.root / "custom-lean"
            selected.write_bytes(b"compiler")
            self.environment["_LEAN_EXECUTABLE"] = str(selected)
            selected_before = verifier.artifact_identity()
            selected.write_bytes(b"changed!")
            self.assertNotEqual(verifier.artifact_identity(), selected_before)
            before = verifier.artifact_identity()
            external.write_bytes(b"B")
            after = verifier.artifact_identity()
            self.assertNotEqual(after, before)
            other = self.root / "other"
            other.mkdir()
            self.environment["LEAN_PATH"] = os.pathsep.join(
                [str(other), str(external.parent)]
            )
            forward = verifier.artifact_identity()
            self.environment["LEAN_PATH"] = os.pathsep.join(
                [str(external.parent), str(other)]
            )
            self.assertNotEqual(verifier.artifact_identity(), forward)
            self.assertTrue(
                any(c[:2] == ["lake", "env"] and "-c" in c for c in self.calls)
            )
            with patch("solvenet.verifier_identity.MAX_ENTRIES", 2):
                self.assertIsNone(verifier.artifact_identity())

    def test_direct_ld_preload_setting_and_external_library_metadata(self):
        library = self.root / "preload.so"
        library.write_bytes(b"library-A")
        verifier = LeanVerifier(
            self.project, command=(str(Path.home() / ".elan/bin/lean"),)
        )
        with (
            patch.dict(os.environ, {"LD_PRELOAD": ""}),
            patch("solvenet.verifier_identity.subprocess.run", side_effect=self.probe),
        ):
            empty = verifier.artifact_identity()
            self.assertIsNotNone(empty)
            os.environ["LD_PRELOAD"] = str(library)
            before = verifier.artifact_identity()
            self.assertIsNotNone(before)
            self.assertNotEqual(before, empty)
            self.assertEqual(verifier.artifact_identity(), before)
            stamp = library.stat().st_mtime_ns
            library.write_bytes(
                b"library-B"
            )  # same size, outside every scanned import/runtime root
            os.utime(library, ns=(stamp, stamp))
            after = verifier.artifact_identity()
            self.assertIsNotNone(after)
            self.assertNotEqual(after, before)
            os.environ["LD_PRELOAD"] = (
                "../preload.so"  # resolves against the Lean project cwd
            )
            self.assertIsNotNone(verifier.artifact_identity())
            self.assertNotEqual(verifier.artifact_identity(), after)

    def test_lake_effective_ld_preload_resolution_order_and_bounds(self):
        first, second = self.root / "preload-A.so", self.root / "preload-B.so"
        first.write_bytes(b"library-A")
        second.write_bytes(b"library-B")
        self.environment["LD_PRELOAD"] = str(first) + ":" + str(second)
        verifier = LeanVerifier(self.project)
        with (
            patch.dict(os.environ, {"LD_PRELOAD": "/missing/parent-setting.so"}),
            patch("solvenet.verifier_identity.subprocess.run", side_effect=self.probe),
        ):
            before = (
                verifier.artifact_identity()
            )  # use Lake's effective value, not the parent setting
            self.assertIsNotNone(before)
            self.assertTrue(any("LD_PRELOAD" in c[-1] for c in self.calls if "-c" in c))
            second.write_bytes(b"library-C")
            after = verifier.artifact_identity()
            self.assertIsNotNone(after)
            self.assertNotEqual(after, before)
            self.environment["LD_PRELOAD"] = str(second) + " " + str(first)
            self.assertNotEqual(verifier.artifact_identity(), after)
            for value in (
                str(self.root / "missing.so"),
                str(self.root),
                "libunknown.so",
                "$ORIGIN/preload.so",
                str(first) + ":" + str(self.root / "missing.so"),
            ):
                with self.subTest(value=value):
                    self.environment["LD_PRELOAD"] = value
                    self.assertIsNone(verifier.artifact_identity())
            self.environment["LD_PRELOAD"] = ":".join([str(first)] * 129)
            self.assertIsNone(verifier.artifact_identity())

    def test_real_external_import_change_invalidates_checked_artifact(self):
        external = self.root / "external"
        source = external / "External.lean"
        verifier = LeanVerifier(
            ROOT / "lean", command=("lean",)
        )  # direct elan selection
        store = Store(self.root / "state.db")
        group = store.create_group("external", ": True", ["Init", "External"], "pinned")
        agent = store.add_agent(group, "agent", "investigator")
        task = store.add_group_task(
            group, "task", agent, agent, "Check imported result", 1
        )

        def compile_module(proof):
            source.write_text("theorem externalWitness : True := by " + proof + "\n")
            subprocess.run(
                [
                    "lean",
                    "--root=" + str(external),
                    "-o",
                    str(external / "External.olean"),
                    str(source),
                ],
                cwd=ROOT / "lean",
                check=True,
                capture_output=True,
                timeout=10,
            )

        with patch.dict(os.environ, {"LEAN_PATH": str(external)}):
            compile_module("trivial")
            artifact = store.propose_group_artifact(
                group,
                "proof",
                agent,
                task,
                ": True",
                ["Init", "External"],
                "pinned",
                "exact externalWitness",
            )
            coordinator = Coordinator(store, verifier)
            coordinator.tick()
            self.assertEqual(store.group(group)["artifacts"][0]["status"], "verified")
            identity = store.artifact_verifier_binding[0]
            compile_module("sorry")
            changed = verifier.artifact_identity()
            self.assertNotEqual(changed, identity)
            store.bind_group_artifact_verifier(changed)
            self.assertEqual(store.group(group)["artifacts"][0]["status"], "pending")
            coordinator.tick()
            self.assertEqual(store.group(group)["artifacts"][0]["status"], "rejected")
            self.assertIn("sorryAx", store.group(group)["artifacts"][0]["diagnostics"])
            self.assertEqual(
                store.composed_evidence(artifact)[-1]["bundle"]["verifier_identity"],
                changed,
            )
            with patch.object(verifier, "artifact_identity") as identify:
                self.assertFalse(coordinator.tick())
                identify.assert_not_called()


class ComposedWinnerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "state.db")
        self.binding = self.store.bind_group_artifact_verifier("test:checker")
        self.group = self.store.create_group("group", ": True", ["Init"], "pinned")
        self.agent = self.store.add_agent(self.group, "agent", "investigator")
        self.task = self.store.add_group_task(
            self.group, "task", self.agent, self.agent, "Check", 2
        )
        self.good = VerificationResult(VerificationStatus.VERIFIED, "winner", 1)
        self.bad = VerificationResult(VerificationStatus.REJECTED, "loser", 2)
        self.usage = {"status": "known", "direct": [], "type": [], "transitive": []}

    def test_artifact_duplicate_and_competing_completions_cannot_replace_winner(self):
        artifact = self.store.propose_group_artifact(
            self.group,
            "proof",
            self.agent,
            self.task,
            ": True",
            ["Init"],
            "pinned",
            "trivial",
            prerequisite_proof_ids=[],
        )
        bundle = self.store.proof_context(artifact)
        first = self.store.begin_composed_check(artifact, "artifact", bundle)
        from solvenet.proof_context import VerificationBusy

        with self.assertRaises(VerificationBusy):
            self.store.begin_composed_check(artifact, "artifact", bundle)
        for result, check_id in (
            (self.good, first),
            (self.bad, first),
            (self.good, None),
        ):
            self.store.checked_group_artifact(
                artifact,
                str(result.status),
                result.diagnostics,
                binding=self.binding,
                bundle=bundle,
                result=result,
                usage=self.usage,
                check_id=check_id,
            )
        evidence = self.store.composed_evidence(artifact)
        self.assertEqual([e["committed"] for e in evidence], [1, 0])
        self.assertEqual(
            self.store.group(self.group)["artifacts"][0]["diagnostics"], "winner"
        )
        self.assertEqual(
            self.store.export_proof_bundle(artifact), evidence[0]["bundle"]
        )

    def test_attempt_duplicate_and_competing_completions_cannot_replace_winner(self):
        # Construct a queued group target through the existing job/provenance fixture.
        job = self.store.enqueue_group_job(
            self.group,
            self.task,
            self.agent,
            "target",
            "pinned",
            "scripted",
            "finding",
            [{"role": "user", "content": "Prove the target"}],
        )
        with self.store.transaction() as db:
            db.execute(
                "UPDATE jobs SET kind='model.generate',task_type=NULL WHERE id=?",
                (job,),
            )
        self.store.select_target_proof_context(job, [])
        lease = self.store.claim("worker", ["scripted"])
        self.store.result(
            lease["assignment_id"],
            {
                "lease_token": lease["lease_token"],
                "status": "completed",
                "output": {"text": "trivial"},
            },
        )
        attempt = self.store.pending()
        bundle = attempt["bundle"]
        first = self.store.begin_composed_check(attempt["id"], "attempt", bundle)
        from solvenet.proof_context import VerificationBusy

        with self.assertRaises(VerificationBusy):
            self.store.begin_composed_check(attempt["id"], "attempt", bundle)
        for result, check_id in (
            (self.bad, first),
            (self.good, first),
            (self.bad, None),
        ):
            self.store.verified(
                attempt["id"],
                result,
                bundle=bundle,
                usage=self.usage,
                check_id=check_id,
            )
        evidence = self.store.composed_evidence(attempt["id"])
        self.assertEqual([e["committed"] for e in evidence], [1, 0])
        run = self.store.group(self.group)["run"]["run_id"]
        self.assertNotEqual(self.store.run_status(run)["status"], "solved")
        self.assertEqual(self.store.run(run)["attempts"][0]["diagnostics"], "loser")
        self.assertEqual(
            self.store.export_proof_bundle(attempt["id"]), evidence[0]["bundle"]
        )


class EmptyGraphTargetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "state.db"
        self.store = Store(self.path)
        self.verifier = LeanVerifier(ROOT / "lean")
        self.group = self.store.start_group_loop(
            "empty",
            ": True",
            ["Init"],
            "pinned",
            dict.fromkeys(
                ("planner", "investigator", "critic", "synthesizer"), "scripted"
            ),
        )
        for text in (
            json.dumps({"approaches": ["first", "second"]}),
            "First note",
            "Second note",
            json.dumps({"decisions": ["accept", "redirect"]}),
            "Corrected note",
        ):
            for _ in range(12):
                self.store.advance_group(self.group)
                lease = self.store.claim(
                    "worker", ["scripted"], supports_model_respond=True
                )
                if lease:
                    break
            else:
                self.fail("No finding/critique job")
            self.store.result(
                lease["assignment_id"],
                {
                    "lease_token": lease["lease_token"],
                    "status": "completed",
                    "output": {"type": lease["job"]["task_type"], "text": text},
                },
            )
        for _ in range(3):
            self.store.advance_group(self.group)
        self.assertEqual(self.store.group_loop(self.group)["phase"], "synthesize")

    def complete_target(self):
        lease = self.store.claim("worker", ["scripted"], supports_model_respond=True)
        self.assertEqual(lease["job"]["kind"], "model.generate")
        self.store.result(
            lease["assignment_id"],
            {
                "lease_token": lease["lease_token"],
                "status": "completed",
                "output": {"text": "trivial"},
            },
        )
        return lease["job"]["id"], self.store.pending()

    def test_empty_context_waits_for_fresh_binding_and_uses_composed_check(self):
        self.assertFalse(self.store.advance_group(self.group))
        with (
            patch.object(self.verifier, "artifact_identity", return_value=None),
            patch.object(self.verifier, "verify") as standalone,
        ):
            self.assertFalse(Coordinator(self.store, self.verifier).tick())
            standalone.assert_not_called()
        identity = self.verifier.artifact_identity()
        self.store.bind_group_artifact_verifier(identity)
        # A stored non-None identity alone is not a fresh binding after restart.
        self.store = Store(self.path)
        self.assertFalse(self.store.advance_group(self.group))
        self.assertTrue(Coordinator(self.store, self.verifier).tick())
        self.assertTrue(
            self.store.advance_group(self.group)
        )  # synthesis_wait; no candidate yet
        with patch.object(self.verifier, "artifact_identity") as identify:
            self.assertFalse(Coordinator(self.store, self.verifier).tick())
            identify.assert_not_called()
        job, attempt = self.complete_target()
        context = self.store.proof_context(job)
        self.assertEqual(context["declarations"], [])
        self.assertEqual(context["verifier_identity"], identity)
        self.assertEqual(context["proof"], "")
        with self.assertRaisesRegex(ValueError, "frozen composed context"):
            self.store.verified(
                attempt["id"], VerificationResult(VerificationStatus.VERIFIED, "", 1)
            )
        with (
            patch.object(self.verifier, "verify") as standalone,
            patch.object(
                self.verifier, "verify_composed", wraps=self.verifier.verify_composed
            ) as composed,
        ):
            Coordinator(self.store, self.verifier).tick()
            standalone.assert_not_called()
            composed.assert_called_once()
        evidence = self.store.composed_evidence(attempt["id"])
        self.assertEqual(evidence[0]["committed"], 1)
        self.assertEqual(evidence[0]["bundle"]["declarations"], [])
        self.assertEqual(self.store.group_loop(self.group)["reason"], "verified_target")

    def test_missing_graph_manifest_never_falls_back_to_standalone(self):
        self.store.bind_group_artifact_verifier(self.verifier.artifact_identity())
        self.assertTrue(self.store.advance_group(self.group))
        job, _attempt = self.complete_target()
        with self.store.transaction() as db:
            db.execute("DELETE FROM proof_contexts WHERE owner_id=?", (job,))
        with (
            patch.object(self.verifier, "verify") as standalone,
            patch.object(self.verifier, "verify_composed") as composed,
        ):
            Coordinator(self.store, self.verifier).tick()
            standalone.assert_not_called()
            composed.assert_not_called()
        run = self.store.group(self.group)["run"]["run_id"]
        self.assertEqual(self.store.run_status(run)["status"], "error")
        self.assertEqual(
            self.store.run(run)["attempts"][0]["verification_status"], "verifier_error"
        )
