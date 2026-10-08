import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solvenet.sandbox import ContainerVerifier
from solvenet.server import Coordinator
from solvenet.store import Conflict, Store
from solvenet.verifier import LeanVerifier, VerificationResult, VerificationStatus


class GroupArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.db"
        self.store = Store(self.path)
        self.group = self.store.create_group(
            "target", ": True ∧ True", ["Init"], "lean-test"
        )
        self.agent = self.store.add_agent(self.group, "investigator", "investigator")
        self.task = self.store.add_group_task(
            self.group, "subgoal", self.agent, self.agent, "Find lemma", 2
        )
        verifier = LeanVerifier(
            Path(__file__).resolve().parents[2] / "lean",
            command=(str(Path.home() / ".elan/bin/lake"), "env", "lean"),
        )
        self.coordinator = Coordinator(self.store, verifier)

    def propose(self, key, statement, proof, *, imports=None, environment="lean-test"):
        return self.store.propose_group_artifact(
            self.group,
            key,
            self.agent,
            self.task,
            statement,
            imports or ["Init"],
            environment,
            proof,
        )

    def test_precise_claim_false_claim_and_environment(self):
        good = self.propose("good", ": True", "trivial")
        false = self.propose("false", ": False", "trivial")
        wrong = self.propose(
            "wrong", ": False", "trivial", environment="other-toolchain"
        )
        # A proof for True does not prove a different recorded statement.
        self.assertTrue(self.coordinator.tick())
        self.assertTrue(self.coordinator.tick())
        self.assertTrue(self.coordinator.tick())
        states = {a["id"]: a for a in self.store.group(self.group)["artifacts"]}
        self.assertEqual(states[good]["status"], "verified")
        self.assertEqual(states[false]["status"], "rejected")
        self.assertEqual(states[wrong]["status"], "incompatible")
        self.assertIsNone(self.store.group(self.group)["run"])
        self.assertFalse(self.coordinator.tick())

    def test_timeout_is_terminal_artifact_outcome(self):
        self.propose("slow", ": True", "trivial")
        with patch.object(
            self.coordinator.verifier,
            "verify",
            return_value=VerificationResult(
                VerificationStatus.TIMEOUT, "Lean timed out", 10000
            ),
        ):
            self.assertTrue(self.coordinator.tick())
        self.assertEqual(
            self.store.group(self.group)["artifacts"][0]["status"], "timeout"
        )
        self.assertIsNone(self.store.pending_group_artifact())
        self.assertFalse(self.coordinator.tick())

    def test_transient_identity_unavailable_keeps_proposal_pending(self):
        self.propose("retry", ": True", "trivial")
        verifier = self.coordinator.verifier
        actual = verifier.artifact_identity()
        self.assertIsNotNone(actual)
        with (
            patch.object(
                verifier, "artifact_identity", side_effect=[None, None, actual, actual]
            ) as identify,
            patch.object(verifier, "verify", wraps=verifier.verify) as verify,
        ):
            self.assertFalse(self.coordinator.tick())
            self.assertFalse(self.coordinator.tick())
            self.assertEqual(
                self.store.group(self.group)["artifacts"][0]["status"], "pending"
            )
            verify.assert_not_called()
            self.assertTrue(self.coordinator.tick())
            self.assertEqual(
                self.store.group(self.group)["artifacts"][0]["status"], "verified"
            )
            self.assertFalse(self.coordinator.tick())
            self.assertEqual(identify.call_count, 4)  # no expensive idle scan

    def test_binding_change_during_replay_cannot_commit_stale_check(self):
        self.propose("race", ": True", "trivial", imports=["Init", "Lean"])
        image_a = "docker:sha256:" + "a" * 64
        image_b = "docker:sha256:" + "b" * 64
        other = Store(self.path)
        calls = []
        verifier = ContainerVerifier(image="mutable:test")

        def verify(statement, proof, *, imports, identity):
            calls.append(identity)
            if len(calls) == 1:
                other.bind_group_artifact_verifier(image_b)
            return VerificationResult(VerificationStatus.VERIFIED, "", 1)

        with (
            patch.object(verifier, "artifact_identity", return_value=image_a),
            patch.object(verifier, "verify_artifact", side_effect=verify),
        ):
            Coordinator(self.store, verifier).tick()
        self.assertEqual(calls, [image_a, image_a])
        self.assertEqual(
            self.store.group(self.group)["artifacts"][0]["status"], "pending"
        )
        with (
            patch.object(verifier, "artifact_identity", return_value=image_b),
            patch.object(
                verifier,
                "verify_artifact",
                return_value=VerificationResult(VerificationStatus.VERIFIED, "", 1),
            ),
        ):
            Coordinator(other, verifier).tick()
        artifact = other.group(self.group)["artifacts"][0]
        self.assertEqual(
            (artifact["status"], artifact["verifier_identity"]), ("verified", image_b)
        )

    def test_binding_revision_rejects_aba_check(self):
        aid = self.propose("aba", ": True", "trivial")
        old = self.store.bind_group_artifact_verifier("local:a")
        other = Store(self.path)
        other.bind_group_artifact_verifier("local:b")
        current = other.bind_group_artifact_verifier("local:a")
        self.assertNotEqual(old, current)
        self.store.checked_group_artifact(aid, "verified", binding=old)
        self.assertEqual(
            self.store.group(self.group)["artifacts"][0]["status"], "pending"
        )
        other.checked_group_artifact(aid, "verified", binding=current)
        self.assertEqual(
            self.store.group(self.group)["artifacts"][0]["status"], "verified"
        )

    def test_target_imports_replay_and_bounded_diagnostics(self):
        # Init alone proves this, but the target environment must also replay it.
        aid = self.propose("replay", ": True", "trivial", imports=["Init", "Lean"])
        with patch.object(
            self.coordinator.verifier, "verify", wraps=self.coordinator.verifier.verify
        ) as verify:
            self.coordinator.tick()
        self.assertEqual(
            [call.kwargs["imports"] for call in verify.call_args_list],
            [["Init", "Lean"], ["Init"]],
        )
        self.assertEqual(
            self.store.group(self.group)["artifacts"][0]["status"], "verified"
        )
        self.assertEqual(
            self.store.group(self.group)["artifacts"][0]["current_status"],
            "needs_recheck",
        )
        self.assertEqual(
            self.store.group(
                self.group, verifier_identity=self.store.artifact_verifier_binding[0]
            )["artifacts"][0]["current_status"],
            "verified",
        )
        self.assertEqual(
            self.store.group(self.group, verifier_identity="local:changed")[
                "artifacts"
            ][0]["current_status"],
            "needs_recheck",
        )
        self.store.checked_group_artifact(
            aid, "rejected", "overwrite", binding=self.store.artifact_verifier_binding
        )
        self.assertEqual(
            self.store.group(self.group)["artifacts"][0]["status"], "verified"
        )
        bad = self.propose("diagnostics", ": False", "trivial")
        self.store.checked_group_artifact(
            bad, "rejected", "ü" * 9000, binding=self.store.artifact_verifier_binding
        )
        self.assertLessEqual(
            len(self.store.group(self.group)["artifacts"][1]["diagnostics"].encode()),
            2100,
        )

    def test_provenance_immutable_and_worker_label_not_authority(self):
        content = json.dumps(
            {
                "artifact": {
                    "statement": ": True",
                    "imports": ["Init"],
                    "environment": "lean-test",
                    "proof": "trivial",
                    "status": "verified",
                }
            }
        )
        job = self.store.enqueue_group_job(
            self.group,
            self.task,
            self.agent,
            "call",
            "lean-test",
            "scripted",
            "finding",
            [{"role": "user", "content": "find"}],
        )
        lease = self.store.claim("worker", ["scripted"], supports_model_respond=True)
        self.store.result(
            lease["assignment_id"],
            {
                "lease_token": lease["lease_token"],
                "status": "completed",
                "output": {"type": "finding", "text": content},
            },
        )
        aid = self.store.propose_group_artifact(
            self.group,
            "lemma",
            self.agent,
            self.task,
            ": True",
            ["Init"],
            "lean-test",
            "trivial",
            job_id=job,
        )
        self.assertEqual(
            self.store.group(self.group)["artifacts"][0]["status"], "pending"
        )
        self.assertEqual(
            self.store.propose_group_artifact(
                self.group,
                "lemma",
                self.agent,
                self.task,
                ": True",
                ["Init"],
                "lean-test",
                "trivial",
                job_id=job,
            ),
            aid,
        )
        with self.assertRaises(Conflict):
            self.store.propose_group_artifact(
                self.group,
                "lemma",
                self.agent,
                self.task,
                ": False",
                ["Init"],
                "lean-test",
                "trivial",
                job_id=job,
            )
        other = self.store.add_agent(self.group, "other", "investigator")
        with self.assertRaises(Conflict):
            self.store.propose_group_artifact(
                self.group,
                "spoof",
                other,
                self.task,
                ": True",
                ["Init"],
                "lean-test",
                "trivial",
                job_id=job,
            )
        self.store = Store(self.path)
        artifact = self.store.group(self.group)["artifacts"][0]
        self.assertEqual(
            (artifact["agent_id"], artifact["task_id"], artifact["job_id"]),
            (self.agent, self.task, job),
        )
        self.assertEqual(artifact["status"], "pending")
        self.assertEqual(artifact["source"], "job")
        self.assertEqual(
            self.store.group(self.group)["tasks"][0]["owner_id"], self.agent
        )

    def test_non_owner_cannot_propose_even_without_job(self):
        other = self.store.add_agent(self.group, "other", "investigator")
        with self.assertRaises(Conflict):
            self.store.propose_group_artifact(
                self.group,
                "forged",
                other,
                self.task,
                ": True",
                ["Init"],
                "lean-test",
                "trivial",
            )
        aid = self.propose("manual", ": True", "trivial")
        manual = self.store.group(self.group)["artifacts"][0]
        self.assertEqual(manual["id"], aid)
        self.assertEqual(manual["source"], "coordinator")
        self.assertIsNone(manual["job_id"])

    def test_local_verifier_identity_tracks_project_inputs(self):
        source = Path(__file__).resolve().parents[2] / "lean"
        project = self.path.parent / "project"
        project.mkdir()
        for name in ("lean-toolchain", "lakefile.toml", "lake-manifest.json"):
            shutil.copyfile(source / name, project / name)
        verifier = LeanVerifier(
            project, command=(str(Path.home() / ".elan/bin/lake"), "env", "lean")
        )
        original = verifier.artifact_identity()
        self.assertIsNotNone(original)
        with (project / "lakefile.toml").open("a") as output:
            output.write("\n-- project revision\n")
        self.assertNotEqual(verifier.artifact_identity(), original)

        package = project / ".lake/packages/example"
        package.mkdir(parents=True)
        dependency = package / "Example.lean"
        dependency.write_text("theorem example : True := by trivial\n")
        # A synthetic package may make Lake reject the fixture manifest. Keep
        # its version response fixed to isolate fingerprinting of project files.
        prefix = self.path.parent / "toolchain"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "lib/lean").mkdir(parents=True)
        selected = prefix / "bin/lean"
        selected.write_bytes(b"local Lean binary")

        def runtime(command, **kwargs):
            output = (
                str(prefix).encode()
                if "--print-prefix" in command
                else json.dumps({"_LEAN_EXECUTABLE": str(selected)}).encode()
                if "-c" in command
                else b"Lean version 4.19.0\n"
            )
            return subprocess.CompletedProcess(command, 0, stdout=output)

        with patch("solvenet.verifier.subprocess.run", side_effect=runtime):
            with_dependency = verifier.artifact_identity()
            self.assertIsNotNone(with_dependency)
            dependency.write_text("theorem example : True := by exact True.intro\n")
            after_source = verifier.artifact_identity()
            self.assertNotEqual(after_source, with_dependency)
            compiled = package / ".lake/build/lib/lean/Example.olean"
            compiled.parent.mkdir(parents=True)
            compiled.write_bytes(b"compiled dependency revision")
            self.assertNotEqual(verifier.artifact_identity(), after_source)

    def test_same_version_selected_lean_binary_replacement_changes_identity(self):
        project = self.path.parent / "project"
        project.mkdir()
        source = Path(__file__).resolve().parents[2] / "lean"
        for name in ("lean-toolchain", "lakefile.toml", "lake-manifest.json"):
            shutil.copyfile(source / name, project / name)
        prefix = self.path.parent / "toolchain"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "lib/lean").mkdir(parents=True)
        selected = prefix / "bin/lean"
        selected.write_bytes(b"lean-binary-A")
        verifier = LeanVerifier(
            project, command=(str(Path.home() / ".elan/bin/lake"), "env", "lean")
        )
        calls = []

        def runtime(command, **kwargs):
            calls.append(command)
            output = (
                str(prefix).encode()
                if "--print-prefix" in command
                else json.dumps({"_LEAN_EXECUTABLE": str(selected)}).encode()
                if "-c" in command
                else b"Lean version unchanged\n"
            )
            return subprocess.CompletedProcess(command, 0, stdout=output)

        with patch("solvenet.verifier.subprocess.run", side_effect=runtime):
            before = verifier.artifact_identity()
            self.assertEqual(before, verifier.artifact_identity())
            replacement = project / "new-lean"
            replacement.write_bytes(b"lean-binary-B")  # same size and reported version
            stamp = selected.stat().st_mtime_ns
            os.utime(replacement, ns=(stamp, stamp))
            replacement.replace(selected)
            self.assertNotEqual(verifier.artifact_identity(), before)
        self.assertTrue(any("--print-prefix" in command for command in calls))

    def test_container_artifact_pins_image_across_retag_and_import_replay(self):
        self.propose("docker-lemma", ": True", "trivial", imports=["Init", "Lean"])
        original = "sha256:" + "a" * 64
        retagged = "sha256:" + "b" * 64
        tag = {"id": original}
        images_used = []

        def docker_command(command, **kwargs):
            if command[:3] == ["docker", "image", "inspect"]:
                resolved = tag["id"]
                tag["id"] = retagged  # tag changes before docker run
                return subprocess.CompletedProcess(
                    command, 0, stdout=(resolved + "\n").encode()
                )
            return subprocess.CompletedProcess(command, 0)

        def execute(command, timeout):
            images_used.append(command[-1])
            mount = command[command.index("--mount") + 1]
            directory = Path(
                mount.removeprefix("type=bind,src=").removesuffix(",dst=/work")
            )
            (directory / "result.json").write_text(
                json.dumps(
                    {
                        "status": "verified" if command[-1] == original else "rejected",
                        "diagnostics": "",
                        "elapsed_ms": 1,
                    }
                )
            )
            return subprocess.CompletedProcess(command, 0, stderr=b"")

        verifier = ContainerVerifier(image="mutable:test")
        with (
            patch("solvenet.sandbox.subprocess.run", side_effect=docker_command),
            patch("solvenet.sandbox._run_docker", side_effect=execute),
        ):
            Coordinator(self.store, verifier).tick()
            self.assertEqual(
                self.store.group(self.group)["artifacts"][0]["status"], "verified"
            )
            self.assertEqual(images_used, [original, original])
            self.assertEqual(
                self.store.group(self.group)["artifacts"][0]["verifier_identity"],
                "docker:" + original,
            )
            self.assertEqual(
                verifier.verify(": True", "trivial").status, VerificationStatus.REJECTED
            )
            self.assertEqual(images_used[-1], "mutable:test")
            # A standalone verified artifact is rescanned when reuse is
            # requested; this manual group has no synthesis to consume it.
            self.store.bind_group_artifact_verifier(verifier.artifact_identity())
            Coordinator(self.store, verifier).tick()
            self.assertEqual(images_used[-1], retagged)
            self.assertEqual(
                self.store.group(self.group)["artifacts"][0]["status"], "rejected"
            )


if __name__ == "__main__":
    unittest.main()
