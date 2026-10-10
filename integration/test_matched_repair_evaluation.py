"""Offline matched-budget and compiled-worker/real-Lean baseline checks."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_group_collaboration as fixed
from matched_repair_evaluation import (
    Ledger,
    SingleStore,
    baseline,
    fixed_budget,
    inputs,
    preflight,
    summarize,
)
from test_site_worker_lean import serving

from solvenet.server import Coordinator
from solvenet.verifier import LeanVerifier


class MatchedBudgetTests(unittest.TestCase):
    def test_all_four_reserved_before_first_call_and_never_refunded(self):
        budget = fixed_budget()
        self.assertEqual(preflight(budget)["reserved_total_usd"], "3.93216")
        with self.assertRaises(ValueError):
            preflight(replace(budget, prior_reserved_usd=Decimal("1.1")))
        with self.assertRaises(ValueError):
            preflight(replace(budget, total_usd=Decimal("3.9")))
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory), budget)
            for name in ledger.record["runs"]:
                ledger.start(name)
                ledger.finish(
                    name,
                    {
                        "unknown_usage_assignments": 1,
                        "known_usage_estimate_usd": "0.1",
                        "total_usage_estimate_usd": None,
                    },
                    "injected_failure",
                )
                with self.assertRaises(ValueError):
                    ledger.start(name)
            persisted = json.loads(ledger.path.read_text())
            self.assertEqual(persisted["reserved_total_usd"], "3.93216")
            self.assertIsNone(persisted["actual_paid_usd"])
            self.assertTrue(
                all(
                    row["status"] == "failed"
                    for row in persisted["observations"].values()
                )
            )

    def test_fixed_inputs_have_no_reference_proofs(self):
        for target in inputs().values():
            self.assertEqual(set(target), {"statement", "imports", "environment"})
            self.assertEqual(target["imports"], ["Init"])

    def test_cli_preflight_never_launches_or_reads_key(self):
        reply = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("matched_repair_evaluation.py")),
                "--worker",
                "/absent-worker",
                "--key-file",
                "/absent-key",
                "--output",
                "/absent-output",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        record = json.loads(reply.stdout)
        self.assertEqual(record["actual_paid_usd"], "0")
        self.assertEqual(len(record["runs"]), 4)

    def test_single_agent_six_jobs_and_failed_retry_calls_are_hard_caps(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SingleStore(Path(directory) / "state.db", fixed_budget())
            target = inputs()["graph-nat-reorder"]
            for _ in range(6):
                store.dispatch(target, "scripted")
                for _ in range(2):
                    assignment = store.claim("offline", ["scripted"])
                    self.assertIsNotNone(assignment)
                    store.result(
                        assignment["assignment_id"],
                        {
                            "lease_token": assignment["lease_token"],
                            "status": "failed",
                            "error": "injected",
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        },
                    )
            self.assertIsNone(store.dispatch(target, "scripted"))
            self.assertIsNone(store.claim("offline", ["scripted"]))
            with store.connect() as db:
                self.assertEqual(
                    db.execute("SELECT count(*) FROM assignments").fetchone()[0], 12
                )

    def test_unknown_usage_blocks_later_single_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SingleStore(Path(directory) / "state.db", fixed_budget())
            store.dispatch(inputs()["graph-nat-reorder"], "scripted")
            assignment = store.claim("offline", ["scripted"])
            store.result(
                assignment["assignment_id"],
                {
                    "lease_token": assignment["lease_token"],
                    "status": "failed",
                    "error": "injected",
                    "usage": {},
                },
            )
            self.assertIsNone(store.claim("offline", ["scripted"]))
            self.assertEqual(store.halt_reason, "usage_unknown_pause")

    def test_group_repair_counts_include_context_suffix_only_for_dispatched_corrections(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "group.json"
            evidence = output.with_suffix(".json.data")
            evidence.mkdir()
            SingleStore(evidence / "state.db", fixed_budget())
            # Model persisted frontier rows: initial work, distinct correction
            # contexts, and an undispatched decision that must not count.
            with closing(sqlite3.connect(evidence / "state.db")) as db:
                for strategy, job in [
                    ("initial|context:a", "job"),
                    ("default|context:a", "job"),
                    ("target-correction|context:a", "job"),
                    ("target-correction|context:b", "job"),
                    ("target-correction", "job"),
                    ("auxiliary-correction|context:a", "job"),
                    ("auxiliary-correction", "job"),
                    ("target-correction|context:c", None),
                    ("auxiliary-correction|context:b", None),
                    ("target-correction-other|context:a", "job"),
                ]:
                    db.execute(
                        "INSERT INTO frontier_decisions "
                        "(group_id,claim_id,action,strategy,graph_revision,reason,"
                        "deferred,job_id,reservation) VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            "group",
                            "claim",
                            "synthesize",
                            strategy,
                            1,
                            "test",
                            "[]",
                            job,
                            4,
                        ),
                    )
                db.commit()
            output.write_text(json.dumps({"mode": "group"}))
            summary = summarize(output, fixed_budget())
            self.assertEqual(summary["target_repairs"], 3)
            self.assertEqual(summary["auxiliary_repairs"], 2)

    def test_internal_verifier_failure_time_is_unknown_not_free(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "single.json"
            evidence = output.with_suffix(".json.data")
            evidence.mkdir()
            store = SingleStore(evidence / "state.db", fixed_budget())
            run_id = store.dispatch(inputs()["graph-nat-reorder"], "scripted")
            assignment = store.claim("offline", ["scripted"])
            self.assertEqual(assignment["job"]["timeout_seconds"], 45)
            store.result(
                assignment["assignment_id"],
                {
                    "lease_token": assignment["lease_token"],
                    "status": "completed",
                    "output": {"text": "rfl"},
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
            with patch.object(
                LeanVerifier, "verify", side_effect=RuntimeError("offline")
            ):
                with self.assertLogs("solvenet.server", level="ERROR"):
                    Coordinator(store, LeanVerifier(Path(directory))).tick()
            output.write_text(
                json.dumps({"mode": "single", "runs": [store.run(run_id)]})
            )
            summary = summarize(output, fixed_budget())
            self.assertEqual(summary["target_lean_checks"], 1)
            self.assertEqual(summary["total_lean_known_elapsed_ms"], 0)
            self.assertEqual(summary["lean_unknown_elapsed_entries"], 1)
            self.assertFalse(summary["autonomous_target_accepted"])


class SingleAgentRealLeanTests(unittest.TestCase):
    setUpClass = classmethod(fixed.GroupCollaborationTests.setUpClass.__func__)
    tearDownClass = classmethod(fixed.GroupCollaborationTests.tearDownClass.__func__)

    def test_six_rejected_lean_attempts_cannot_dispatch_seventh(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SingleStore(Path(directory) / "state.db", fixed_budget())
            coordinator = Coordinator(
                store, LeanVerifier(fixed.ROOT / "lean", timeout_seconds=10)
            )
            previous = None
            target = inputs()["graph-nat-reorder"]
            for _ in range(6):
                run_id = store.dispatch(target, "scripted", previous)
                assignment = store.claim("offline", ["scripted"])
                store.result(
                    assignment["assignment_id"],
                    {
                        "lease_token": assignment["lease_token"],
                        "status": "completed",
                        "output": {"text": "rfl"},
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                )
                coordinator.tick()
                run = store.run(run_id)
                self.assertEqual(run["status"], "exhausted")
                previous = run["attempts"][0]
                self.assertEqual(previous["verification_status"], "rejected")
            self.assertIsNone(store.dispatch(target, "scripted", previous))

    def test_compiled_worker_rejects_then_repairs_with_exact_frozen_feedback(self):
        calls = []

        class Provider(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(body)
                proof = (
                    "rfl"
                    if len(calls) == 1
                    else "simp only [Nat.add_assoc, Nat.add_comm, Nat.add_left_comm]"
                )
                reply = {
                    "status": "completed",
                    "usage": {"input_tokens": 100, "output_tokens": 30},
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps({"proof": proof}),
                                }
                            ],
                        }
                    ],
                }
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(reply).encode())

        with (
            tempfile.TemporaryDirectory() as directory,
            serving(ThreadingHTTPServer(("127.0.0.1", 0), Provider)) as provider,
        ):
            args = SimpleNamespace(
                worker=Path(self.worker),
                output=Path(directory) / "single.json",
                model="gpt-6.1-sol",
                profile="responses-reasoning",
                key_file=Path("/never-read-real-key"),
            )

            def launch(args, budget, worker_path, url, log):
                return subprocess.Popen(
                    [
                        str(worker_path),
                        "-coordinator",
                        url,
                        "-provider",
                        "openai",
                        "-model",
                        args.model,
                        "-openai-profile",
                        args.profile,
                        "-openai-reasoning-effort",
                        "low",
                        "-openai-url",
                        provider,
                        "-openai-context",
                        "24576",
                        "-openai-context-bytes",
                        "24576",
                        "-openai-max-output",
                        "2048",
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env={
                        **os.environ,
                        "OPENAI_API_KEY": "synthetic-offline-key",
                        "OPENAI_API_KEY_FILE": "",
                    },
                )

            with patch("matched_repair_evaluation.launch_worker", side_effect=launch):
                baseline(
                    args,
                    replace(fixed_budget(), wall_seconds=60),
                    inputs()["graph-nat-reorder"],
                )
            record = json.loads(args.output.read_text())
            self.assertEqual(record["terminal_reason"], "verified_target")
            self.assertEqual(len(calls), 2)
            for call in calls:
                self.assertEqual(call["model"], "gpt-6.1-sol")
                self.assertEqual(call["reasoning"], {"effort": "low"})
                self.assertEqual(call["max_output_tokens"], 2048)
                self.assertNotIn("synthetic-offline-key", json.dumps(call))
            first, second = record["runs"]
            failed = first["attempts"][0]
            self.assertEqual(failed["verification_status"], "rejected")
            self.assertEqual(second["status"], "solved")
            wire = json.dumps(calls[1], ensure_ascii=False)
            self.assertIn("rfl", wire)
            # Nested JSON wire quoting differs; inspect the frozen coordinator packet.
            with closing(
                sqlite3.connect(args.output.with_suffix(".json.data") / "state.db")
            ) as db:
                self.assertEqual(
                    db.execute("SELECT count(*) FROM group_agents").fetchone()[0], 0
                )
                self.assertEqual(
                    db.execute("SELECT count(*) FROM group_runs").fetchone()[0], 0
                )
                packets = [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT packet FROM single_inputs ORDER BY rowid"
                    )
                ]
                feedback = json.loads(
                    packets[1]["messages"][0]["content"].split("\n", 1)[1]
                )
                self.assertEqual(feedback["candidate"], failed["candidate"])
                self.assertEqual(feedback["diagnostics"], failed["diagnostics"])
                self.assertEqual(feedback["attempt_id"], failed["id"])
                self.assertEqual(packets[0]["target"], packets[1]["target"])
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE single_inputs SET packet='{}'")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("DELETE FROM single_inputs")
            summary = summarize(args.output, fixed_budget())
            self.assertTrue(summary["autonomous_target_accepted"])
            self.assertEqual(summary["target_lean_checks"], 2)
            self.assertEqual(summary["target_repairs"], 1)
            self.assertEqual(summary["unknown_usage_assignments"], 0)


if __name__ == "__main__":
    unittest.main()
