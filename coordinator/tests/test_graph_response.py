import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solvenet.graph_response import SCHEMA, graph_batch
from solvenet.server import Coordinator
from solvenet.store import Conflict, Store


class GraphResponseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "state.db"
        self.store = Store(self.path)
        self.group = self.store.create_group(
            "g", ": True ∧ True", ["Init"], "lean-test"
        )
        self.root = self.store.group(self.group)["graph"]["root_id"]
        self.one = self.store.add_agent(self.group, "one", "investigator")
        self.two = self.store.add_agent(self.group, "two", "critic")
        self.index = 0

    def claim(self, key="lemma", **extra):
        return dict(
            key=key,
            statement=": True",
            imports=["Init"],
            environment="lean-test",
            **extra,
        )

    def batch(self, **arrays):
        return {"graph_schema": SCHEMA, **arrays}

    def job(self, agent=None, kind="finding", cost=1):
        self.index += 1
        agent = agent or self.one
        task = self.store.add_group_task(
            self.group, f"t{self.index}", agent, agent, "Investigate", 2
        )
        job = self.store.enqueue_group_job(
            self.group,
            task,
            agent,
            f"j{self.index}",
            "lean-test",
            "scripted",
            kind,
            [{"role": "user", "content": "Respond"}],
            cost=cost,
        )
        lease = self.store.claim("worker", ["scripted"], supports_model_respond=True)
        self.assertEqual(job, lease["job"]["id"])
        return job, task, lease

    def deliver(self, batch, agent=None, kind="finding"):
        job, task, lease = self.job(agent, kind)
        payload = {
            "status": "completed",
            "lease_token": lease["lease_token"],
            "output": {
                "type": kind,
                "text": json.dumps(batch) if isinstance(batch, dict) else batch,
            },
        }
        self.store.result(lease["assignment_id"], payload)
        return (
            job,
            task,
            lease,
            payload,
            self.store.ingest_group_graph_response(self.group, job),
        )

    def test_two_sources_exact_dedup_job_local_refs_and_identity_is_ignored(self):
        deliveries = []
        for agent in (self.one, self.two):
            batch = self.batch(
                claims=[self.claim(verified=True, agent_id="fake", job_id="wrong")],
                relationships=[
                    dict(
                        key="edge",
                        **{"from": self.root, "to": "$lemma"},
                        kind="suggests_using",
                        reason="Potentially useful",
                    )
                ],
                findings=[
                    {
                        "key": "evidence",
                        "claim": "$lemma",
                        "text": "Distinct evidence " + agent,
                        "verification_status": "verified",
                        "agent_id": "fake",
                    }
                ],
            )
            deliveries.append(self.deliver(batch, agent))
        first, second = [d[4] for d in deliveries]
        self.assertEqual(first["claims"], second["claims"])
        self.assertNotEqual(first["publications"], second["publications"])
        graph = self.store.group_claim_neighborhood(self.group)
        self.assertEqual(len(graph["claims"]), 2)
        self.assertEqual(len(graph["messages"]), 2)
        self.assertEqual(
            {m["verification_status"] for m in graph["messages"]}, {"unverified"}
        )
        for agent, (job, task, lease, payload, receipt) in zip(
            (self.one, self.two), deliveries, strict=True
        ):
            pub = next(p for p in graph["publications"] if p["job_id"] == job)
            self.assertEqual(
                (pub["agent_id"], pub["task_id"], pub["assignment_id"]),
                (agent, task, lease["assignment_id"]),
            )
            revision = self.store.group(self.group)["graph"]["revision"]
            self.store.result(lease["assignment_id"], payload)
            self.store = Store(self.path)
            self.assertEqual(
                Coordinator(self.store, None).ingest_group_graph_response(
                    self.group, job
                ),
                receipt,
            )
            self.assertEqual(
                self.store.group(self.group)["graph"]["revision"], revision
            )
            changed = payload | {"output": payload["output"] | {"text": "{}"}}
            with self.assertRaises(Conflict):
                self.store.result(lease["assignment_id"], changed)
        self.assertEqual(graph["artifacts"], [])

    def test_review_provenance_and_redirect_does_not_change_formal_status(self):
        delivery = self.deliver(
            self.batch(
                claims=[self.claim()],
                relationships=[
                    dict(
                        key="edge",
                        **{"from": self.root, "to": "$lemma"},
                        kind="suggests_using",
                    )
                ],
                artifacts=[
                    {
                        "key": "proof",
                        "claim": "$lemma",
                        "statement": ": True",
                        "imports": ["Init"],
                        "environment": "lean-test",
                        "proof": "trivial",
                        "status": "verified",
                        "verifier_identity": "fake",
                    }
                ],
            )
        )
        artifact = delivery[4]["artifacts"]["proof"]
        self.assertEqual(
            self.store.group(self.group)["artifacts"][0]["status"], "pending"
        )
        self.assertEqual(
            self.store.group(self.group)["artifacts"][0]["assignment_id"],
            delivery[2]["assignment_id"],
        )
        binding = self.store.bind_group_artifact_verifier("actual-pinned-verifier")
        self.store.checked_group_artifact(artifact, "verified", binding=binding)
        critic = self.deliver(
            self.batch(
                reviews=[
                    {
                        "key": "review",
                        "relationship": delivery[4]["relationships"]["edge"],
                        "status": "challenged",
                        "reason": "Try direct proof",
                        "reviewer_id": "fake",
                        "job_id": delivery[0],
                    }
                ]
            ),
            self.two,
            "critique",
        )
        graph = self.store.group_claim_neighborhood(self.group)
        review = graph["reviews"][0]
        self.assertEqual(
            (
                review["reviewer_id"],
                review["task_id"],
                review["job_id"],
                review["assignment_id"],
            ),
            (self.two, critic[1], critic[0], critic[2]["assignment_id"]),
        )
        self.assertEqual(review["source"], "job")
        self.assertEqual(graph["artifacts"][0]["status"], "verified")
        redirected = self.store.add_group_task(
            self.group,
            "redirect",
            self.two,
            self.one,
            "Try direct proof after challenge",
            1,
            claim_id=self.root,
            parent_id=critic[1],
        )
        self.assertNotEqual(redirected, critic[1])
        self.assertEqual(
            self.store.group(self.group)["artifacts"][0]["status"], "verified"
        )
        self.assertNotEqual(
            self.store.run(self.store.group(self.group)["run"]["run_id"])["status"],
            "solved",
        )

    def test_malformed_and_cross_group_batches_roll_back_all_graph_writes(self):
        other = self.store.create_group("other", ": False", ["Init"], "lean-test")
        foreign = self.store.group(other)["graph"]["root_id"]
        cases = [
            self.batch(
                claims=[self.claim()],
                relationships=[
                    dict(
                        key="edge",
                        **{"from": self.root, "to": foreign},
                        kind="suggests_using",
                    )
                ],
            ),
            self.batch(
                claims=[self.claim()],
                relationships=[
                    dict(
                        key="edge",
                        **{"from": "$lemma", "to": "$lemma"},
                        kind="suggests_using",
                    )
                ],
            ),
            self.batch(
                claims=[self.claim()],
                findings=[{"key": "note", "claim": "$unknown", "text": "Bad"}],
            ),
            self.batch(
                claims=[self.claim()],
                reviews=[
                    {"key": "review", "relationship": foreign, "status": "challenged"}
                ],
            ),
            self.batch(
                claims=[self.claim()],
                artifacts=[
                    {
                        "key": "proof",
                        "claim": "$lemma",
                        "statement": ": False",
                        "imports": ["Init"],
                        "environment": "lean-test",
                        "proof": "trivial",
                    }
                ],
            ),
            self.batch(
                claims=[self.claim()],
                artifacts=[
                    {
                        "key": "proof",
                        "claim": "$lemma",
                        "statement": ": True",
                        "imports": ["Init"],
                        "environment": "different",
                        "proof": "trivial",
                    }
                ],
            ),
            self.batch(
                claims=[self.claim()],
                artifacts=[
                    {
                        "key": "proof",
                        "claim": "$lemma",
                        "statement": ": True",
                        "imports": ["Lean"],
                        "environment": "lean-test",
                        "proof": "trivial",
                    }
                ],
            ),
            self.batch(claims=[self.claim(), self.claim()]),
            self.batch(
                claims=[self.claim()], findings=[{"key": "note", "claim": "$lemma"}]
            ),
            self.batch(claims="not an array"),
            self.batch(claims=[self.claim(str(i)) for i in range(9)]),
            self.batch(claims=[self.claim(reason="x" * 2049)]),
            self.batch(
                claims=[self.claim()],
                relationships=[
                    dict(
                        key="edge",
                        **{"from": self.root, "to": "$lemma"},
                        kind="proof_uses",
                    )
                ],
            ),
            self.batch(
                claims=[self.claim()],
                reviews=[
                    {"key": "review", "relationship": "$missing", "status": "verified"}
                ],
            ),
            self.batch(graph_schema="solvenet.graph.v99"),
            '{"graph_schema":"solvenet.graph.v1","claims":[],"claims":[]}',
        ]
        for batch in cases:
            with self.subTest(batch=batch):
                before = self.store.group_claim_neighborhood(self.group)
                delivery = self.deliver(batch)
                self.assertEqual(delivery[4]["status"], "rejected")
                after = self.store.group_claim_neighborhood(self.group)
                # Creating/completing the source task/job legitimately advances revision.
                for name in (
                    "claims",
                    "publications",
                    "relationships",
                    "reviews",
                    "messages",
                    "artifacts",
                ):
                    self.assertEqual(after[name], before[name])
                revision = after["revision"]
                self.store.result(delivery[2]["assignment_id"], delivery[3])
                self.assertEqual(
                    self.store.group_claim_neighborhood(self.group)["revision"],
                    revision,
                )

    def test_failure_unfinished_wrong_group_and_non_graph_text(self):
        job, _task, lease = self.job()
        with self.assertRaises(Conflict):
            self.store.ingest_group_graph_response(self.group, job)
        self.store.result(
            lease["assignment_id"],
            {
                "status": "failed",
                "failure_class": "permanent",
                "lease_token": lease["lease_token"],
                "output": {"text": json.dumps(self.batch(claims=[self.claim()]))},
            },
        )
        with self.assertRaises(Conflict):
            self.store.ingest_group_graph_response(self.group, job)
        self.assertEqual(self.store.group(self.group)["graph_responses"], [])
        for text in (
            "Informal verified finding",
            '{"claims":[]}',
            '```json\n{"graph_schema":"solvenet.graph.v1"}\n```',
        ):
            self.assertIsNone(self.deliver(text)[4])
        good = self.deliver(self.batch(claims=[self.claim()]))
        other = self.store.create_group("other", ": False", ["Init"], "lean-test")
        with self.assertRaises(Conflict):
            self.store.ingest_group_graph_response(other, good[0])

    def test_crash_rolls_back_completion_and_replay_recovers_preexisting_completion(
        self,
    ):
        job, _task, lease = self.job()
        payload = {
            "status": "completed",
            "lease_token": lease["lease_token"],
            "output": {
                "type": "finding",
                "text": json.dumps(self.batch(claims=[self.claim()])),
            },
        }
        original = self.store._apply_graph_batch

        def crash(*args):
            original(*args)
            raise RuntimeError("crash after graph writes before receipt")

        before = self.store.group_claim_neighborhood(self.group)
        with (
            patch.object(self.store, "_apply_graph_batch", crash),
            self.assertRaises(RuntimeError),
        ):
            self.store.result(lease["assignment_id"], payload)
        self.store = Store(self.path)
        self.assertEqual(self.store.group_claim_neighborhood(self.group), before)
        with self.store.connect() as db:
            self.assertIsNone(
                db.execute(
                    "SELECT result FROM assignments WHERE id=?",
                    (lease["assignment_id"],),
                ).fetchone()[0]
            )
        self.store.result(lease["assignment_id"], payload)
        self.assertEqual(
            self.store.ingest_group_graph_response(self.group, job)["status"],
            "accepted",
        )
        # A committed completion from before automatic ingestion (upgrade/recovery).
        job2, _task2, lease2 = self.job()
        with patch.object(self.store, "_ingest_completed_graph_job"):
            self.store.result(
                lease2["assignment_id"],
                payload | {"lease_token": lease2["lease_token"]},
            )
        self.store = Store(self.path)
        recovered = self.store.ingest_group_graph_response(self.group, job2)
        self.assertEqual(
            recovered["claims"],
            self.store.ingest_group_graph_response(self.group, job)["claims"],
        )
        self.assertNotEqual(
            recovered["publications"],
            self.store.ingest_group_graph_response(self.group, job)["publications"],
        )

    def test_capacity_and_encoded_overhead_are_atomic(self):
        for constant in (
            "MAX_CLAIMS",
            "MAX_PUBLICATIONS",
            "MAX_RELATIONSHIPS",
            "MAX_REVIEWS",
            "MAX_EVIDENCE_LINKS",
        ):
            with (
                self.subTest(constant=constant),
                patch("solvenet.claim_graph." + constant, 0),
            ):
                delivery = self.deliver(
                    self.batch(
                        claims=[self.claim()],
                        relationships=[
                            dict(
                                key="edge",
                                **{"from": self.root, "to": "$lemma"},
                                kind="suggests_using",
                            )
                        ],
                        reviews=[
                            {
                                "key": "review",
                                "relationship": "$edge",
                                "status": "promising",
                            }
                        ],
                        findings=[{"key": "note", "claim": "$lemma", "text": "Idea"}],
                    )
                )
                self.assertEqual(delivery[4]["status"], "rejected")
                self.assertEqual(
                    len(self.store.group_claim_neighborhood(self.group)["claims"]), 1
                )
        # Limits include JSON quotes, escapes and UTF-8, not only the human text.
        base = json.dumps(self.batch(note=""), ensure_ascii=False)
        exact = json.dumps(
            self.batch(note="x" * (8192 - len(base.encode()))), ensure_ascii=False
        )
        self.assertEqual(len(exact.encode()), 8192)
        self.assertEqual(self.deliver(exact)[4]["status"], "accepted")
        _job, _task, lease = self.job()
        with self.assertRaises(ValueError):
            self.store.result(
                lease["assignment_id"],
                {
                    "status": "completed",
                    "lease_token": lease["lease_token"],
                    "output": {"type": "finding", "text": exact + " "},
                },
            )
        for char in ('"', "\\", "λ"):
            encoded = json.dumps(self.batch(note=char * 5000), ensure_ascii=False)
            self.assertGreater(len(encoded.encode()), 8192)
            with self.assertRaises(ValueError):
                graph_batch(encoded)
        with self.assertRaises(ValueError):
            graph_batch(
                json.dumps(
                    self.batch(
                        claims=[self.claim(str(i)) for i in range(8)],
                        findings=[
                            {"key": "f" + str(i), "claim": "$0", "text": "x"}
                            for i in range(8)
                        ],
                        relationships=[{"key": "extra"}],
                    )
                )
            )

    def test_coordinator_authored_sources_stay_distinct(self):
        job = self.deliver(
            self.batch(
                claims=[self.claim()],
                relationships=[
                    dict(
                        key="edge",
                        **{"from": self.root, "to": "$lemma"},
                        kind="suggests_using",
                    )
                ],
            )
        )
        claim = job[4]["claims"]["lemma"]
        self.store.propose_group_claim(
            self.group, "operator", ": True", ["Init"], "lean-test"
        )
        self.store.review_group_relationship(
            self.group,
            "operator-review",
            job[4]["relationships"]["edge"],
            self.two,
            "promising",
        )
        graph = self.store.group_claim_neighborhood(self.group, claim)
        self.assertEqual(
            {p["source"] for p in graph["publications"] if p["claim_id"] == claim},
            {"job", "coordinator"},
        )
        self.assertEqual(graph["reviews"][0]["source"], "coordinator")
        self.assertIsNone(graph["reviews"][0]["assignment_id"])

    def test_successful_retry_uses_successful_assignment_and_not_worker_identity(self):
        job, task, failed = self.job(cost=2)
        self.store.result(
            failed["assignment_id"],
            {"status": "failed", "lease_token": failed["lease_token"]},
        )
        success = self.store.claim(
            "different-worker", ["scripted"], supports_model_respond=True
        )
        self.assertEqual(success["job"]["id"], job)
        self.store.result(
            success["assignment_id"],
            {
                "status": "completed",
                "lease_token": success["lease_token"],
                "output": {
                    "type": "finding",
                    "text": json.dumps(self.batch(claims=[self.claim()])),
                },
            },
        )
        publication = next(
            p
            for p in self.store.group_claim_neighborhood(
                self.group,
                self.store.ingest_group_graph_response(self.group, job)["claims"][
                    "lemma"
                ],
            )["publications"]
            if p["source"] == "job"
        )
        self.assertEqual(
            (
                publication["agent_id"],
                publication["task_id"],
                publication["assignment_id"],
            ),
            (self.one, task, success["assignment_id"]),
        )
        self.assertNotEqual(publication["assignment_id"], failed["assignment_id"])

    def test_artifact_capacity_and_wrong_task_type_roll_back_publications(self):
        batch = self.batch(
            claims=[self.claim()],
            artifacts=[
                {
                    "key": "proof",
                    "claim": "$lemma",
                    "statement": ": True",
                    "imports": ["Init"],
                    "environment": "lean-test",
                    "proof": "trivial",
                }
            ],
        )
        for kind in ("plan", "critique"):
            self.assertEqual(self.deliver(batch, kind=kind)[4]["status"], "rejected")
        with patch("solvenet.group_artifacts.MAX_ARTIFACTS", 0):
            self.assertEqual(self.deliver(batch)[4]["status"], "rejected")
        graph = self.store.group_claim_neighborhood(self.group)
        self.assertEqual(len(graph["claims"]), 1)
        self.assertEqual(len(graph["publications"]), 1)
        self.assertEqual(graph["artifacts"], [])

    def test_explicit_graph_cannot_fall_back_to_legacy_artifact(self):
        from solvenet.group_artifacts import artifact_from_finding

        text = json.dumps(
            {
                "graph_schema": "unknown",
                "artifact": {
                    "statement": ": True",
                    "imports": ["Init"],
                    "environment": "lean-test",
                    "proof": "trivial",
                },
            }
        )
        self.assertEqual(self.deliver(text)[4]["status"], "rejected")
        self.assertIsNone(artifact_from_finding(text))
        self.assertEqual(self.store.group(self.group)["artifacts"], [])


if __name__ == "__main__":
    unittest.main()
