"""Offline budget/gate regressions; no real credential or provider calls."""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from live_openai_graph_trial import (
    ROOT,
    Budget,
    TrialStore,
    api,
    main,
    observe,
    structured_result,
    terminate_at_cutoff,
    usage_estimate,
)


def budget():
    return Budget(Decimal("5"), Decimal(".08"), Decimal("2.50"), Decimal("10"))


class LiveOpenAIBudgetTests(unittest.TestCase):
    def test_independent_dollar_work_and_prompt_bounds(self):
        cap = replace(budget(), prior_reserved_usd=Decimal("0"))
        for changes in (
            {"max_work": 20},
            {"total_usd": Decimal("5.01")},
            {"per_run_usd": Decimal(".98")},
            {"per_run_usd": Decimal("1.51")},
            {"context_capacity": 65536},
            {"output_capacity": 32768},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(cap, **changes).validate()
        replace(cap, max_work=32).validate()
        self.assertEqual(cap.record()["graph_limits"]["target_corrections"], 1)
        self.assertEqual(cap.record()["graph_limits"]["completion_reserve"], 1)
        self.assertEqual(cap.record()["graph_limits"]["direct_auxiliary_correction"], 1)

    def test_total_includes_prior_unknown_probe_and_every_retry(self):
        cap = budget()
        cap.validate()
        self.assertEqual(cap.max_jobs, 6)
        self.assertEqual(cap.max_calls, 12)
        self.assertEqual(cap.projected_usd, Decimal("1.06304"))
        with self.assertRaises(ValueError):
            replace(cap, total_usd=Decimal("1.06303")).validate()
        replace(cap, total_usd=cap.projected_usd).validate()
        with self.assertRaises(ValueError):
            replace(cap, context_capacity=65536).validate()
        for price in ("NaN", "Infinity", "-1", "0"):
            with self.subTest(price=price), self.assertRaises(ValueError):
                replace(cap, input_usd_per_million=Decimal(price)).validate()

    def test_preflight_does_not_open_key_or_launch_worker(self):
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("live_openai_graph_trial.py")),
                "--model",
                "synthetic-model",
                "--profile",
                "responses-reasoning",
                "--worker",
                "/does-not-exist",
                "--key-file",
                "/does-not-exist",
                "--output",
                "/does-not-exist/observation.json",
                "--total-budget-usd",
                "5",
                "--prior-reserved-usd",
                ".08",
                "--input-usd-per-million",
                "2.5",
                "--output-usd-per-million",
                "10",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            Decimal(json.loads(result.stdout)["projected_total_usd"]),
            Decimal("1.06304"),
        )

    def test_retry_budget_includes_first_consecutive_product_observation(self):
        cap = replace(budget(), prior_reserved_usd=Decimal(".0075725"))
        cap.validate()
        self.assertEqual(cap.projected_usd, Decimal(".9906125"))
        self.assertEqual(
            Decimal(cap.record()["projected_graph_usd"]), Decimal(".98304")
        )

    def test_fresh_repair_cycle_keeps_twelve_call_dollar_bound(self):
        cap = replace(budget(), prior_reserved_usd=Decimal("0"))
        cap.validate()
        self.assertEqual(cap.projected_usd, Decimal(".98304"))
        self.assertEqual(cap.max_jobs, 6)
        self.assertEqual(cap.max_calls, 12)
        self.assertEqual(cap.record()["graph_limits"]["planning_calls"], 4)
        self.assertEqual(cap.record()["graph_limits"]["retries"], 1)

    def test_structured_stage_requires_actual_graph_envelope(self):
        for text, expected in (
            ("plain prose", False),
            ('{"graph_schema":"wrong"}', False),
            ('{"graph_schema":"solvenet.graph.v1"}', True),
        ):
            self.assertEqual(
                structured_result({"status": "completed", "output": {"text": text}}),
                expected,
            )
        self.assertFalse(structured_result({"status": "failed"}))

    def test_cli_selects_fixture_and_preserves_default(self):
        for fixture in (
            None,
            "integration/fixtures/graph-nat-consecutive-product.json",
        ):
            argv = [
                "trial",
                "--model",
                "synthetic",
                "--profile",
                "responses-reasoning",
                "--worker",
                "/does-not-exist",
                "--key-file",
                "/does-not-exist",
                "--output",
                "/does-not-exist/result.json",
                "--total-budget-usd",
                "1",
                "--prior-reserved-usd",
                "0",
                "--input-usd-per-million",
                "2.5",
                "--output-usd-per-million",
                "10",
                "--execute-paid",
            ]
            if fixture:
                argv.extend(["--fixture", fixture])
            with (
                self.subTest(fixture=fixture),
                patch.object(sys, "argv", argv),
                patch("live_openai_graph_trial.observe") as observation,
                patch("builtins.print"),
            ):
                main()
            args, cap = observation.call_args.args
            self.assertEqual(
                args.fixture,
                Path(fixture)
                if fixture
                else ROOT / "integration/fixtures/graph-nat-reorder.json",
            )
            self.assertEqual(cap.max_calls, 12)
            self.assertEqual(cap.projected_usd, Decimal(".98304"))

    def test_unknown_usage_is_not_zero_or_a_total_price(self):
        estimate = usage_estimate(
            {
                "group": {
                    "cost": {
                        "input_tokens": {"known": 100, "unknown": 1},
                        "output_tokens": {"known": 50, "unknown": 0},
                    }
                }
            },
            budget(),
        )
        self.assertIsNone(estimate["total_estimate_usd"])
        self.assertEqual(estimate["known_usage_estimate_usd"], "0.00075")

    def test_known_graph_usage_does_not_make_unknown_probe_cost_known(self):
        estimate = usage_estimate(
            {
                "group": {
                    "cost": {
                        "input_tokens": {"known": 100, "unknown": 0},
                        "output_tokens": {"known": 50, "unknown": 0},
                    }
                }
            },
            budget(),
        )
        self.assertEqual(estimate["graph_estimate_usd"], "0.00075")
        self.assertIsNone(estimate["total_estimate_usd"])
        self.assertIsNone(estimate["prior_probe_actual_usd"])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = TrialStore(Path(temp.name) / "state.db", budget())

    def dispatch(self, assignments=2):
        self.store.submit(
            ": True",
            imports=["Init"],
            model="scripted",
            attempts=1,
            max_assignments=assignments,
        )
        return self.store.claim("offline-worker", ["scripted"])

    def fail_assignment(self, assignment, usage):
        self.store.result(
            assignment["assignment_id"],
            {
                "lease_token": assignment["lease_token"],
                "status": "failed",
                "error": {"message": "offline injected failure", "retryable": True},
                "usage": usage,
            },
        )

    def test_unknown_failure_prevents_even_assignment_retry(self):
        assignment = self.dispatch()
        self.fail_assignment(assignment, {})
        self.assertIsNone(self.store.claim("offline-worker", ["scripted"]))
        self.assertEqual(self.store.halt_reason, "usage_unknown_pause")

    def test_expiry_is_checked_before_retry_can_be_returned(self):
        assignment = self.dispatch()
        with self.store.transaction() as db:
            db.execute(
                "UPDATE assignments SET expires=0 WHERE id=?",
                (assignment["assignment_id"],),
            )
        self.assertIsNone(self.store.claim("offline-worker", ["scripted"]))
        self.assertEqual(self.store.halt_reason, "usage_unknown_pause")
        with self.store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM assignments").fetchone()[0], 1
            )

    def test_known_usage_formatting_failure_stops_more_calls(self):
        assignment = self.dispatch()
        self.store.result(
            assignment["assignment_id"],
            {
                "lease_token": assignment["lease_token"],
                "status": "failed",
                "failure_category": "formatting_failure",
                "error": {"message": "offline truncation", "retryable": False},
                "usage": {"input_tokens": 1, "output_tokens": 2048},
            },
        )
        self.assertIsNone(self.store.claim("offline-worker", ["scripted"]))
        self.assertEqual(self.store.halt_reason, "formatting_failure_pause")

    def test_final_assignment_can_finish_without_an_eleventh_call(self):
        assignment = self.dispatch(assignments=20)
        for _ in range(self.store.budget.max_calls - 1):
            self.fail_assignment(assignment, {"input_tokens": 1, "output_tokens": 1})
            assignment = self.store.claim("offline-worker", ["scripted"])
        self.assertIsNotNone(assignment)
        self.assertIsNone(self.store.claim("offline-worker", ["scripted"]))
        self.assertIsNone(self.store.halt_reason)
        self.assertIsNone(self.store.admission_reason())
        self.fail_assignment(assignment, {"input_tokens": 1, "output_tokens": 1})
        self.assertEqual(self.store.admission_reason(), "assignment_cap")

    def test_hard_gate_counts_failed_assignment_retries(self):
        assignment = self.dispatch(assignments=20)
        for _ in range(self.store.budget.max_calls):
            self.assertIsNotNone(assignment)
            self.assertLessEqual(assignment["job"]["timeout_seconds"], 45)
            self.fail_assignment(assignment, {"input_tokens": 1, "output_tokens": 1})
            assignment = self.store.claim("offline-worker", ["scripted"])
        self.assertIsNone(assignment)
        self.assertEqual(self.store.admission_reason(), "assignment_cap")
        with self.store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM assignments").fetchone()[0], 12
            )

    def test_process_cutoff_prevents_first_request(self):
        self.store.cutoff = time.monotonic() - 1
        self.assertIsNone(self.dispatch())
        self.assertEqual(self.store.halt_reason, "process_cutoff")

    def test_cutoff_kills_worker_that_ignores_termination(self):
        worker = Mock()
        worker.wait.side_effect = [subprocess.TimeoutExpired("offline", 5), None]
        terminate_at_cutoff(self.store, worker)
        self.assertEqual(self.store.halt_reason, "process_cutoff")
        worker.terminate.assert_called_once()
        worker.kill.assert_called_once()
        self.assertEqual(worker.wait.call_count, 2)

    def test_worker_exit_preserves_observation_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "observation.json"
            args = SimpleNamespace(
                worker=Path(sys.executable),
                fixture=ROOT
                / "integration/fixtures/graph-nat-consecutive-product.json",
                output=output,
                model="synthetic",
                profile="responses-reasoning",
                key_file=Path("/does-not-exist"),
            )
            worker = Mock()
            worker.poll.return_value = 0
            verifier = Mock()
            verifier.readiness.return_value.ready = True
            verifier.artifact_identity.return_value = "offline-verifier"
            with (
                patch(
                    "live_openai_graph_trial.subprocess.Popen", return_value=worker
                ) as launch,
                patch("live_openai_graph_trial.LeanVerifier", return_value=verifier),
                patch("live_openai_graph_trial.api", wraps=api) as requests,
            ):
                observe(args, budget())
            target = json.loads(args.fixture.read_text())
            self.assertEqual(set(target), {"statement", "imports", "environment"})
            self.assertEqual(
                target["statement"],
                "(n : Nat) : 6 ∣ n * (n + 1) * (n + 2)",  # noqa: RUF001 -- Lean divisibility notation
            )
            submitted = requests.call_args_list[0].args[2]
            self.assertEqual(submitted["graph_limits"]["response_output_tokens"], 2048)
            self.assertEqual(submitted["graph_limits"]["planning_calls"], 4)
            self.assertEqual(submitted["graph_limits"]["retries"], 1)
            self.assertEqual({key: submitted[key] for key in target}, target)
            self.assertFalse(any("proof" in key for key in submitted))
            observation = json.loads(output.read_text())
            self.assertEqual(observation["budget"]["response_output_tokens"], 2048)
            self.assertEqual(observation["budget"]["synthesis_output_tokens"], 2048)
            self.assertEqual(observation["terminal_reason"], "worker_exited")
            self.assertEqual(observation["snapshot"]["loop"]["reason"], "worker_exited")
            self.assertEqual(observation["snapshot"]["group"]["cost"]["leases"], 0)
            worker.terminate.assert_called_once()
            worker.wait.assert_called_once_with(timeout=5)
            self.assertEqual(launch.call_args.kwargs["env"]["OPENAI_API_KEY"], "")
            self.assertEqual(
                launch.call_args.kwargs["env"]["OPENAI_API_KEY_FILE"], "/does-not-exist"
            )
            self.assertNotIn("/does-not-exist", launch.call_args.args[0])

    def test_launch_failure_preserves_error_observation(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "observation.json"
            args = SimpleNamespace(
                worker=Path(sys.executable),
                fixture=ROOT / "integration/fixtures/graph-nat-reorder.json",
                output=output,
                model="synthetic",
                profile="responses-reasoning",
                key_file=Path("/does-not-exist"),
            )
            verifier = Mock()
            verifier.readiness.return_value.ready = True
            with (
                patch(
                    "live_openai_graph_trial.subprocess.Popen",
                    side_effect=OSError("offline injected launch failure"),
                ),
                patch("live_openai_graph_trial.LeanVerifier", return_value=verifier),
                self.assertRaises(OSError),
            ):
                observe(args, budget())
            observation = json.loads(output.read_text())
            self.assertEqual(observation["terminal_reason"], "runner_error")
            self.assertEqual(observation["error_type"], "OSError")
            self.assertIsNone(observation["usage_estimate"]["total_estimate_usd"])
            self.assertTrue(
                output.with_suffix(".json.data").joinpath("state.db").exists()
            )


if __name__ == "__main__":
    unittest.main()
