import json
import tempfile
import unittest
from pathlib import Path

from solvenet.group_routing import choose
from solvenet.store import Store


class RoutingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "state.db"
        self.now = [100.0]
        self.store = Store(self.path, clock=lambda: self.now[0])
        self.models = {
            "planner": ["expert"],
            "investigator": ["cheap", "expert"],
            "critic": ["expert"],
            "synthesizer": ["expert"],
        }
        self.capabilities = {
            "cheap": {"tasks": ["finding"], "context_bytes": 8192, "cost": 1},
            "expert": {"context_bytes": 8192, "cost": 2},
        }

    def register(self, name, *, status="ready"):
        self.store.claim(
            name + "-worker",
            [name],
            supports_model_respond=True,
            provider_health={"status": status},
        )

    def advance_to_claim(self, name):
        for _ in range(12):
            self.store.advance_group(self.group)
            lease = self.store.claim(
                name + "-worker", [name], supports_model_respond=True
            )
            if lease:
                return lease
        self.fail("No lease for " + name)

    def complete(self, lease, text):
        self.store.result(
            lease["assignment_id"],
            {
                "lease_token": lease["lease_token"],
                "status": "completed",
                "output": {"type": lease["job"]["task_type"], "text": text},
                "usage": {"input_tokens": 10, "output_tokens": 4},
                "generation": {"total_duration_ns": 123},
            },
        )

    def start(self, budget=20):
        self.group = self.store.start_group_loop(
            "local",
            ": True",
            ["Init"],
            "lean-test",
            self.models,
            model_capabilities=self.capabilities,
            max_work=budget,
        )

    def test_scoped_escalation_same_agent_and_cost_trace(self):
        self.register("cheap")
        self.register("expert")
        self.start()
        self.complete(
            self.advance_to_claim("expert"),
            json.dumps({"approaches": ["first", "second"]}),
        )
        first = self.advance_to_claim("cheap")
        self.assertIn("first", first["job"]["messages"][0]["content"])
        self.store.result(
            first["assignment_id"],
            {
                "lease_token": first["lease_token"],
                "status": "failed",
                "failure_class": "permanent",
                "failure_category": "provider_failure",
                "usage": {"input_tokens": 3},
            },
        )
        for _ in range(4):
            self.store.advance_group(self.group)
        escalated = self.advance_to_claim("expert")
        snapshot = self.store.group(self.group)
        jobs = {j["request_key"]: j for j in snapshot["jobs"]}
        self.assertEqual(
            jobs["investigate-1"]["agent_id"],
            jobs["investigate-1-escalate"]["agent_id"],
        )
        tasks = {t["request_key"]: t for t in snapshot["tasks"]}
        self.assertEqual(
            tasks["investigate-1-escalate"]["parent_id"], tasks["investigate-1"]["id"]
        )
        self.assertEqual(escalated["job"]["model"], "expert")
        decision = next(
            d
            for d in snapshot["routing"]
            if d["request_key"] == "investigate-1-escalate"
        )
        self.assertEqual(decision["explanation"]["avoid_after_failure"], "cheap")
        self.complete(escalated, "new finding")
        # Negative quality evidence now outweighs the cheaper model's cost.
        self.complete(self.advance_to_claim("expert"), "second finding")
        self.store.advance_group(self.group)
        self.store = Store(self.path, clock=lambda: self.now[0])
        snapshot = self.store.group(self.group)
        self.assertEqual(len(snapshot["messages"]), 3)
        self.assertEqual(snapshot["cost"]["requests"], 4)
        self.assertEqual(snapshot["cost"]["failures"], 1)
        self.assertEqual(snapshot["cost"]["input_tokens"]["known"], 33)
        self.assertEqual(snapshot["cost"]["output_tokens"]["unknown"], 1)
        self.assertEqual(snapshot["cost"]["provider_duration_ns"]["unknown"], 1)

    def test_unavailable_and_budget_have_explicit_trace(self):
        self.register("cheap", status="unavailable")
        self.register("expert")
        self.start(budget=12)
        self.complete(
            self.advance_to_claim("expert"),
            json.dumps({"approaches": ["first", "second"]}),
        )
        for _ in range(4):
            self.store.advance_group(self.group)
        decision = next(
            d
            for d in self.store.group(self.group)["routing"]
            if d["request_key"] == "investigate-1"
        )
        self.assertEqual(decision["explanation"]["selected"], "expert")
        self.assertIn(
            "unavailable", decision["explanation"]["candidates"][0]["reasons"]
        )
        self.assertEqual(self.store.group(self.group)["remaining_work"], 0)
        self.complete(self.advance_to_claim("expert"), "first")
        self.complete(self.advance_to_claim("expert"), "second")
        for _ in range(4):
            self.store.advance_group(self.group)
        self.assertEqual(self.store.group_loop(self.group)["reason"], "budget")
        self.assertEqual(
            self.store.group(self.group)["routing"][-1]["explanation"]["selected"], None
        )

    def test_single_model_fallback_is_labeled(self):
        models = dict.fromkeys(self.models, "only")
        group = self.store.start_group_loop(
            "one", ": True", ["Init"], "lean-test", models
        )
        self.store.advance_group(group)
        self.assertEqual(
            self.store.group(group)["routing"][0]["explanation"]["evidence"],
            "single_model_fallback_not_hierarchy_evidence",
        )

    def test_proof_only_presence_does_not_route_nonproof(self):
        self.register("cheap")
        self.register("expert")
        self.start()
        # A newer proof-only claim updates the exact worker/model capability.
        self.store.claim("cheap-worker", ["cheap"], supports_model_respond=False)
        self.complete(
            self.advance_to_claim("expert"),
            json.dumps({"approaches": ["first", "second"]}),
        )
        for _ in range(4):
            self.store.advance_group(self.group)
        route = next(
            d
            for d in self.store.group(self.group)["routing"]
            if d["request_key"] == "investigate-1"
        )["explanation"]
        self.assertEqual(route["selected"], "expert")
        self.assertEqual(route["candidates"][0]["availability"], "proof_only")

        other_models = dict.fromkeys(self.models, "only")
        self.store.claim("proof-worker", ["only"], supports_model_respond=False)
        blocked = self.store.start_group_loop(
            "proof-only", ": True", ["Init"], "lean-test", other_models
        )
        self.store.advance_group(blocked)
        self.assertEqual(
            self.store.group_loop(blocked)["reason"], "model_unavailable_or_unfit"
        )
        self.assertEqual(
            self.store.group(blocked)["routing"][0]["explanation"]["candidates"][0][
                "availability"
            ],
            "proof_only",
        )

    def test_invalid_plan_is_completed_not_quality_evidence(self):
        self.register("expert")
        self.start()
        self.complete(self.advance_to_claim("expert"), "invalid plan")
        with self.store.connect() as db:
            _, _, trace = choose(
                db,
                self.group,
                {
                    role: [model]
                    for role, model in [
                        ("planner", "expert"),
                        ("investigator", "expert"),
                        ("critic", "expert"),
                        ("synthesizer", "expert"),
                    ]
                },
                self.capabilities,
                "planner",
                "plan",
                100,
                16,
                now=self.now[0],
                lease_seconds=self.store.lease_seconds,
            )
        candidate = json.loads(trace)["candidates"][0]
        self.assertEqual(candidate["observed_completed_calls"], 1)
        self.assertEqual(candidate["observed_accepted_findings"], 0)
        self.assertNotIn("observed_successes", candidate)
        self.store.advance_group(self.group)
        self.store.advance_group(self.group)
        self.assertEqual(self.store.group_loop(self.group)["reason"], "invalid_plan")

    def test_accepted_finding_outweighs_cheaper_failed_calls(self):
        self.register("cheap")
        self.register("expert")
        group = self.store.create_group(
            "quality", ": True", ["Init"], "lean-test", max_work=4
        )
        agent = self.store.add_agent(group, "one", "investigator")
        cheap_task = self.store.add_group_task(
            group, "cheap", agent, agent, "Try cheap", 1
        )
        expert_task = self.store.add_group_task(
            group, "expert", agent, agent, "Try expert", 1
        )
        msg = [{"role": "user", "content": "Find something"}]
        self.store.enqueue_group_job(
            group, cheap_task, agent, "cheap-call", "lean-test", "cheap", "finding", msg
        )
        failed = self.store.claim(
            "cheap-worker", ["cheap"], supports_model_respond=True
        )
        self.store.result(
            failed["assignment_id"],
            {
                "lease_token": failed["lease_token"],
                "status": "failed",
                "failure_class": "permanent",
                "error": "failed",
            },
        )
        job = self.store.enqueue_group_job(
            group,
            expert_task,
            agent,
            "expert-call",
            "lean-test",
            "expert",
            "finding",
            msg,
        )
        lease = self.store.claim(
            "expert-worker", ["expert"], supports_model_respond=True
        )
        self.complete(lease, "useful finding")
        finding = self.store.add_group_message(
            group,
            "finding",
            agent,
            expert_task,
            "finding",
            "useful finding",
            job_id=job,
        )
        self.store.review_group_message(group, finding, agent, "review", "accepted")
        with self.store.connect() as db:
            selected, _, _ = choose(
                db,
                group,
                self.models,
                self.capabilities,
                "investigator",
                "finding",
                100,
                12,
                now=self.now[0],
                lease_seconds=self.store.lease_seconds,
            )
        self.assertEqual(selected, "expert")

    def test_byte_capacity_and_known_token_window_are_distinct(self):
        self.register("expert")
        self.capabilities["expert"]["context_tokens"] = 4096
        self.start()
        with self.store.connect() as db:
            selected, _, trace = choose(
                db,
                self.group,
                self.models,
                self.capabilities,
                "planner",
                "plan",
                3000,
                20,
                context_token_bound=5000,
                now=self.now[0],
                lease_seconds=self.store.lease_seconds,
            )
        self.assertIsNone(selected)
        self.assertEqual(
            json.loads(trace)["candidates"][0]["reasons"], ["context_window_exceeded"]
        )
        del self.capabilities["expert"]["context_tokens"]
        with self.store.connect() as db:
            selected, _, trace = choose(
                db,
                self.group,
                self.models,
                self.capabilities,
                "planner",
                "plan",
                3000,
                20,
                context_token_bound=5000,
                now=self.now[0],
                lease_seconds=self.store.lease_seconds,
            )
        self.assertEqual(selected, "expert")
        self.assertIsNone(json.loads(trace)["candidates"][0]["context_tokens"])


if __name__ == "__main__":
    unittest.main()
