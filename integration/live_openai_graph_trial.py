"""Opt-in OpenAI graph observation; preflight is the default and makes no calls."""

import argparse
import json
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from test_group_collaboration import api
from test_site_worker_lean import ROOT, serving

from solvenet.graph_response import graph_batch
from solvenet.server import Coordinator, make_server
from solvenet.store import Store
from solvenet.verifier import LeanVerifier


@dataclass(frozen=True)
class Budget:
    """All assignments, including failed/expired retries, reserve a full call."""

    total_usd: Decimal
    prior_reserved_usd: Decimal
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    max_work: int = 20
    model_cost: int = 2
    context_capacity: int = 24576
    output_capacity: int = 2048

    @property
    def max_jobs(self):
        return self.max_work // (2 * self.model_cost)

    @property
    def max_calls(self):
        # choose() reserves 2*model_cost; frontier jobs have <=2 assignments.
        return 2 * self.max_jobs

    @property
    def projected_usd(self):
        return self.prior_reserved_usd + self.max_calls * (
            self.context_capacity * self.input_usd_per_million
            + self.output_capacity * self.output_usd_per_million
        ) / Decimal(1000000)

    def validate(self):
        for value, low, high in (
            (self.max_work, 12, 256),
            (self.model_cost, 1, 16),
            (self.context_capacity, 4096, 1048576),
            (self.output_capacity, 2048, 32768),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid work/context/output bound")
        values = (
            self.total_usd,
            self.prior_reserved_usd,
            self.input_usd_per_million,
            self.output_usd_per_million,
        )
        if any(not n.is_finite() or n < 0 for n in values):
            raise ValueError("Prices and allowances must be finite and nonnegative")
        if (
            self.total_usd <= 0
            or self.input_usd_per_million <= 0
            or self.output_usd_per_million <= 0
            or self.projected_usd > self.total_usd
        ):
            raise ValueError("Conservative total projection exceeds authorized budget")

    def record(self):
        return {
            **{key: str(value) for key, value in asdict(self).items()},
            "max_jobs": self.max_jobs,
            "max_attempted_calls_including_retries": self.max_calls,
            "projected_total_usd": str(self.projected_usd),
            "billing_guarantee": False,
            "input_bound": "Full request bytes plus framing upper-bound input tokens",
            "output_bound": "Includes reasoning; existing graph jobs use 512/2048",
        }


class TrialStore(Store):
    """Local claim gate; no provider traffic or credential handling in Python."""

    def __init__(self, path, budget):
        super().__init__(path)
        self.budget = budget
        self.cutoff = time.monotonic() + 300
        self.halt_reason = None
        self.claim_lock = threading.Lock()

    def admission_reason(self, *, claiming=False, check_cap=True):
        with self.transaction() as db:
            rows = db.execute(
                "SELECT a.status,a.result,j.task_type,j.id AS job_id,gj.group_id FROM assignments a "
                "JOIN jobs j ON j.id=a.job_id LEFT JOIN group_jobs gj ON gj.job_id=j.id ORDER BY a.rowid"
            ).fetchall()
        for row in rows:
            result = json.loads(row["result"]) if row["result"] else None
            if row["status"] == "expired" or (
                result is not None
                and any(
                    result.get("usage", {}).get(key) is None
                    for key in ("input_tokens", "output_tokens")
                )
            ):
                return "usage_unknown_pause"
            if result is not None and (
                result.get("failure_category") == "formatting_failure"
                or result.get("generation", {}).get("raw_response_truncated")
            ):
                return "formatting_failure_pause"
        if (
            rows
            and rows[0]["result"]
            and rows[0]["task_type"] == "plan"
            and not structured_result(json.loads(rows[0]["result"]))
        ):
            return "structured_plan_failed"
        if (
            rows
            and rows[0]["result"]
            and rows[0]["task_type"] == "plan"
            and rows[0]["group_id"]
        ):
            receipt = self.ingest_group_graph_response(
                rows[0]["group_id"], rows[0]["job_id"]
            )
            if not receipt or receipt["status"] != "accepted":
                return "structured_plan_rejected"
        if (
            check_cap
            and len(rows) >= self.budget.max_calls
            and (claiming or not any(row["status"] == "active" for row in rows))
        ):
            return "assignment_cap"
        return None

    def claim(self, *args, **kwargs):
        with self.claim_lock:
            # Store.claim expires leases itself; do this before the usage gate too.
            self.expire()
            if time.monotonic() >= self.cutoff:
                self.halt_reason = "process_cutoff"
            reason = self.halt_reason or self.admission_reason(claiming=True)
            if reason == "assignment_cap":
                # Refuse another call, but let the final paid assignment report usage.
                return None
            self.halt_reason = reason
            if self.halt_reason:
                return None
            assignment = super().claim(*args, **kwargs)
            if assignment:
                # A lease can cross its expiry between the two transactions above.
                self.halt_reason = self.admission_reason(check_cap=False)
                if self.halt_reason:
                    return None
                # Tighten the existing worker context deadline, never raise it.
                assignment["job"]["timeout_seconds"] = min(
                    assignment["job"]["timeout_seconds"], 45
                )
            return assignment


def structured_plan_ready(snapshot):
    """Admit expansion only after a successful bounded structured planning reply."""
    calls = snapshot["group"]["calls"]
    completed = [call for call in calls if call["result"] is not None]
    if not completed:
        return None
    return structured_result(completed[0]["result"])


def structured_result(result):
    if result.get("status") != "completed":
        return False
    try:
        return graph_batch(result["output"]["text"]) is not None
    except (ValueError, KeyError, TypeError):
        return False


def usage_estimate(snapshot, budget):
    if snapshot is None:
        return {"known_usage_estimate_usd": "0", "total_estimate_usd": None}
    cost = snapshot["group"]["cost"]
    known = (
        cost["input_tokens"]["known"] * budget.input_usd_per_million
        + cost["output_tokens"]["known"] * budget.output_usd_per_million
    ) / Decimal(1000000)
    unknown = cost["input_tokens"]["unknown"] + cost["output_tokens"]["unknown"]
    return {
        "known_usage_estimate_usd": str(known),
        "graph_estimate_usd": str(known) if not unknown else None,
        "total_estimate_usd": str(known)
        if not unknown and budget.prior_reserved_usd == 0
        else None,
        "unknown_usage_entries": unknown,
        "prior_probe_actual_usd": None,
        "prior_probe_reserved_usd": str(budget.prior_reserved_usd),
        "price_basis": "Operator-confirmed conservative rates; not a provider bill",
    }


def observe(args, budget):
    worker_path = args.worker.resolve(strict=True)
    # Sidecars persist immutable packets, receipts, failures and composed-use evidence.
    evidence = args.output.with_suffix(args.output.suffix + ".data")
    evidence.mkdir(mode=0o700)
    store = TrialStore(evidence / "state.db", budget)
    coordinator = Coordinator(store, LeanVerifier(ROOT / "lean", timeout_seconds=10))
    readiness = coordinator.verifier.readiness()
    if not readiness.ready:
        raise RuntimeError(readiness.diagnostics)
    fixture = json.loads(
        (ROOT / "integration/fixtures/graph-nat-reorder.json").read_text()
    )
    model = "openai/" + args.model
    with (
        args.output.open("x") as output,
        serving(make_server(coordinator, ("127.0.0.1", 0))) as url,
    ):
        worker_session(
            args,
            budget,
            store,
            coordinator,
            fixture,
            model,
            url,
            evidence,
            output,
            worker_path,
        )
    print(
        json.dumps({"output": str(args.output), "terminal_reason": store.halt_reason})
    )


def worker_session(
    args, budget, store, coordinator, fixture, model, url, evidence, output, worker_path
):
    snapshot, run, group_id, error = None, None, None, None
    with (evidence / "worker.log").open("x") as log:
        worker = None
        cutoff_timer = None
        try:
            worker = launch_worker(args, budget, worker_path, url, log)
            cutoff_timer = threading.Timer(
                max(0, store.cutoff - time.monotonic()),
                terminate_at_cutoff,
                args=(store, worker),
            )
            cutoff_timer.start()
            created = api(
                url,
                "/v1/groups",
                {
                    "request_key": "o4-live-openai",
                    "mode": "graph",
                    "statement": fixture["statement"],
                    "imports": fixture["imports"],
                    "environment": fixture["environment"],
                    "max_work": budget.max_work,
                    "deadline": time.time() + 285,
                    "models": dict.fromkeys(
                        ("planner", "investigator", "critic", "synthesizer"), model
                    ),
                    "model_capabilities": {
                        model: {
                            "cost": budget.model_cost,
                            "context_tokens": budget.context_capacity,
                            "context_bytes": budget.context_capacity,
                        }
                    },
                    "graph_limits": {
                        "planning_calls": 2,
                        "verification_operations": 4,
                        "lean_elapsed_ms": 40000,
                        "retries": 0,
                    },
                },
            )
            group_id = created["id"]
            while time.monotonic() < store.cutoff:
                snapshot = api(url, "/v1/groups/" + group_id)
                reason = store.halt_reason or store.admission_reason()
                if reason == "assignment_cap" and coordinator.tick():
                    # Claims remain capped; drain already-paid results through Lean
                    # before freezing the final observation.
                    continue
                if reason:
                    store.halt_reason = reason
                    break
                if structured_plan_ready(snapshot) is False:
                    store.halt_reason = "structured_plan_failed"
                    break
                if snapshot["loop"]["phase"] == "stopped":
                    break
                if worker.poll() is not None:
                    store.halt_reason = "worker_exited"
                    break
                # Single operator-driven scheduler: inspect completion before buying next job.
                coordinator.tick()
                time.sleep(0.2)
            else:
                store.halt_reason = "process_cutoff"
        except Exception as exc:
            error = type(exc).__name__
            store.halt_reason = "runner_error"
            raise
        finally:
            if cutoff_timer is not None:
                cutoff_timer.cancel()
                cutoff_timer.join(timeout=5)
            store.halt_reason = store.halt_reason or "observation_complete"
            if worker is not None:
                try:
                    stop_worker(worker)
                except Exception as exc:
                    error = error or type(exc).__name__
            if group_id:
                if store.halt_reason != "observation_complete":
                    store.stop_frontier(group_id, store.halt_reason)
                try:
                    snapshot = api(url, "/v1/groups/" + group_id)
                    group_run = snapshot["group"]["run"]
                    run = (
                        api(url, "/v1/runs/" + group_run["run_id"])
                        if group_run
                        else None
                    )
                except Exception as exc:
                    error = error or type(exc).__name__
            json.dump(
                {
                    "budget": budget.record(),
                    "model": args.model,
                    "profile": args.profile,
                    "reasoning_effort": "low",
                    "single_model_collaboration": True,
                    "per_call_timeout_seconds": 45,
                    "process_cutoff_seconds": 300,
                    "terminal_reason": store.halt_reason,
                    "error_type": error,
                    "snapshot": snapshot,
                    "run": run,
                    "usage_estimate": usage_estimate(snapshot, budget),
                    "evidence_directory": str(evidence),
                    "limitations": "One-model observation; no hierarchy or efficiency claim. Probe usage unknown. Local trusted Lean boundary.",
                },
                output,
                indent=2,
            )
            output.flush()
            os.fsync(output.fileno())


def launch_worker(args, budget, worker_path, url, log):
    return subprocess.Popen(  # noqa: S603 -- explicitly selected compiled local worker; no shell
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
            "-openai-context",
            str(budget.context_capacity),
            "-openai-context-bytes",
            str(budget.context_capacity),
            "-openai-max-output",
            str(budget.output_capacity),
            "-id",
            "o4-local-trial",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        env={
            **os.environ,
            "OPENAI_API_KEY": "",
            "OPENAI_API_KEY_FILE": str(args.key_file.expanduser()),
        },
    )


def terminate_at_cutoff(store, worker):
    store.halt_reason = "process_cutoff"
    stop_worker(worker)


def stop_worker(worker):
    """Enforce cleanup even while the operator is waiting for a Lean check."""
    worker.terminate()
    try:
        worker.wait(timeout=5)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--profile", required=True, choices=["responses-reasoning"])
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--total-budget-usd", required=True, type=Decimal)
    parser.add_argument("--prior-reserved-usd", required=True, type=Decimal)
    parser.add_argument("--input-usd-per-million", required=True, type=Decimal)
    parser.add_argument("--output-usd-per-million", required=True, type=Decimal)
    parser.add_argument(
        "--execute-paid",
        action="store_true",
        help="authorize bounded graph calls; no compatibility probe",
    )
    args = parser.parse_args()
    budget = Budget(
        args.total_budget_usd,
        args.prior_reserved_usd,
        args.input_usd_per_million,
        args.output_usd_per_million,
    )
    try:
        budget.validate()
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(budget.record(), indent=2), flush=True)
    if args.execute_paid:
        observe(args, budget)


if __name__ == "__main__":
    main()
