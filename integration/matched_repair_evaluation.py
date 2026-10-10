"""Four fixed observations. Default preflight is offline; paid execution is opt-in."""

import argparse
import hashlib
import json
import os
import threading
import time
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from live_openai_graph_trial import (
    ROOT,
    Budget,
    TrialStore,
    api,
    launch_worker,
    observe,
    serving,
    stop_worker,
    terminate_at_cutoff,
)

from solvenet.context_packet import prompt_cost
from solvenet.server import Coordinator, make_server
from solvenet.verifier import LeanVerifier

PROBLEMS = ("graph-nat-consecutive-product", "graph-nat-reorder")
MODES = ("group", "single")


def inputs():
    """Whitelist target inputs: the reorder fixture also contains offline proofs."""
    return {
        name: {key: fixture[key] for key in ("statement", "imports", "environment")}
        for name in PROBLEMS
        for fixture in [
            json.loads((ROOT / f"integration/fixtures/{name}.json").read_text())
        ]
    }


def fixed_budget():
    return Budget(Decimal("5"), Decimal("0"), Decimal("2.50"), Decimal("10"))


def preflight(budget):
    budget.validate()
    reservation = budget.projected_usd - budget.prior_reserved_usd
    total = budget.prior_reserved_usd + 4 * reservation
    if total > budget.total_usd:
        raise ValueError("All four worst-case reservations exceed cumulative ceiling")
    return {
        "schema": "solvenet.matched-repair.v1",
        "budget": budget.record(),
        "inputs": inputs(),
        "runs": [f"{problem}-{mode}" for problem in PROBLEMS for mode in MODES],
        "reserved_per_run_usd": str(reservation),
        "reserved_total_usd": str(total),
        "actual_paid_usd": "0",
        "limitations": (
            "Two problems, one observation per mode; reorder is an easier control. "
            "Single agent has up to six target checks/five corrections; graph spends "
            "jobs/checks on planning and auxiliary work and has one target correction. "
            "Equal capacity ceilings do not imply equal work, prompts, or success rates. "
            "No reference proofs submitted; local trusted Lean boundary."
        ),
    }


def durable_json(path, value):
    """Replace only our ledger; callers hold an exclusive directory ownership lock."""
    temporary = path.with_suffix(".tmp")
    with temporary.open("x") as output:
        json.dump(value, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Ledger:
    """Reserve the complete batch irrevocably before calls, including failed runs."""

    def __init__(self, directory, budget):
        self.path = directory / "ledger.json"
        self.record = preflight(budget)
        self.record["observations"] = {
            name: {
                "status": "reserved",
                "reserved_usd": self.record["reserved_per_run_usd"],
            }
            for name in self.record["runs"]
        }
        durable_json(self.path, self.record)

    def start(self, name):
        row = self.record["observations"][name]
        if row["status"] != "reserved":
            raise ValueError("Observation already consumed; retries are forbidden")
        row["status"] = "started"
        self.record["actual_paid_usd"] = None
        durable_json(self.path, self.record)

    def finish(self, name, result=None, error=None):
        row = self.record["observations"][name]
        if row["status"] != "started":
            raise ValueError("Observation was not started")
        row.update(
            status="finished" if error is None else "failed", result=result, error=error
        )
        observations = list(self.record["observations"].values())
        known = sum(
            (
                Decimal(item["result"]["known_usage_estimate_usd"])
                for item in observations
                if item.get("result")
            ),
            Decimal("0"),
        )
        self.record["aggregate_known_usage_estimate_usd"] = str(known)
        self.record["aggregate_total_usage_estimate_usd"] = (
            str(known)
            if all(
                item["status"] == "finished"
                and item["result"]["total_usage_estimate_usd"] is not None
                for item in observations
            )
            else None
        )
        # Reservations never shrink, even when every provider counter is present.
        durable_json(self.path, self.record)


class SingleStore(TrialStore):
    """One logical agent, sequential target submissions with frozen exact feedback."""

    def __init__(self, path, budget):
        super().__init__(path, budget)
        with self.transaction() as db:
            db.execute(
                "CREATE TABLE single_inputs(job_id TEXT PRIMARY KEY, packet TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TRIGGER single_inputs_frozen BEFORE UPDATE ON single_inputs BEGIN SELECT RAISE(ABORT, 'immutable single input'); END"
            )
            db.execute(
                "CREATE TRIGGER single_inputs_no_delete BEFORE DELETE ON single_inputs BEGIN SELECT RAISE(ABORT, 'immutable single input'); END"
            )

    def dispatch(self, target, model, previous=None):
        with self.claim_lock:
            return self._dispatch(target, model, previous)

    def _dispatch(self, target, model, previous):
        messages = []
        if previous is not None:
            messages = [
                {
                    "role": "user",
                    "content": "Correct your previous rejected target proof using exact Lean feedback. "
                    "Return only a replacement Lean tactic proof.\n"
                    + json.dumps(
                        {
                            "attempt_id": previous["id"],
                            "candidate": previous["candidate"],
                            "status": previous["verification_status"],
                            "diagnostics": previous["diagnostics"],
                        },
                        ensure_ascii=False,
                    ),
                }
            ]
        if (
            prompt_cost(target["statement"], target["imports"], messages, 2048)
            > self.budget.context_capacity
        ):
            self.halt_reason = "exact_feedback_capacity"
            return None
        with self.transaction() as db:
            if db.execute("SELECT count(*) FROM jobs").fetchone()[0] >= 6:
                self.halt_reason = "job_cap"
                return None
        created = self.submit(
            target["statement"],
            target["imports"],
            model=model,
            attempts=1,
            max_repairs=0,
            max_assignments=2,
            max_output_tokens=2048,
            generation_timeout_seconds=45,
        )
        with self.transaction() as db:
            job = db.execute(
                "SELECT id FROM jobs WHERE run_id=?", (created["run_id"],)
            ).fetchone()[0]
            db.execute(
                "INSERT INTO single_inputs VALUES (?,?)",
                (
                    job,
                    json.dumps(
                        {
                            "target": target,
                            "messages": messages,
                            "parent_attempt_id": previous["id"] if previous else None,
                        }
                    ),
                ),
            )
        return created["run_id"]

    def claim(self, *args, **kwargs):
        assignment = super().claim(*args, **kwargs)
        if assignment:
            with self.connect() as db:
                row = db.execute(
                    "SELECT packet FROM single_inputs WHERE job_id=?",
                    (assignment["job"]["id"],),
                ).fetchone()
            packet = json.loads(row[0])
            assignment["job"]["messages"] = packet["messages"]
        return assignment


def baseline(args, budget, target):
    worker_path = args.worker.resolve(strict=True)
    evidence = args.output.with_suffix(".json.data")
    evidence.mkdir(mode=0o700)
    store = SingleStore(evidence / "state.db", budget)
    verifier = LeanVerifier(ROOT / "lean", timeout_seconds=10)
    readiness = verifier.readiness()
    if not readiness.ready:
        raise RuntimeError(readiness.diagnostics)
    coordinator = Coordinator(store, verifier)
    runs, error, worker, timer, current = [], None, None, None, None
    with (
        args.output.open("x") as output,
        serving(make_server(coordinator, ("127.0.0.1", 0))) as url,
        (evidence / "worker.log").open("x") as log,
    ):
        try:
            current = store.dispatch(target, "openai/" + args.model)
            worker = launch_worker(args, budget, worker_path, url, log)
            timer = threading.Timer(
                max(0, store.cutoff - time.monotonic()),
                terminate_at_cutoff,
                args=(store, worker),
            )
            timer.start()
            while time.monotonic() < store.cutoff:
                coordinator.tick()
                run = api(url, "/v1/runs/" + current)
                reason = store.halt_reason or store.admission_reason()
                if run["status"] != "running":
                    runs.append(run)
                    if run["status"] == "solved":
                        store.halt_reason = "verified_target"
                        break
                    attempts = run["attempts"]
                    if (
                        reason
                        or not attempts
                        or attempts[-1]["verification_status"] != "rejected"
                    ):
                        store.halt_reason = reason or run["status"]
                        break
                    current = store.dispatch(
                        target, "openai/" + args.model, attempts[-1]
                    )
                    if current is None:
                        break
                elif reason:
                    store.halt_reason = reason
                    break
                if worker.poll() is not None:
                    store.halt_reason = "worker_exited"
                    break
                time.sleep(0.2)
            else:
                store.halt_reason = "process_cutoff"
        except Exception as exc:
            error = type(exc).__name__
            store.halt_reason = "runner_error"
            raise
        finally:
            if timer:
                timer.cancel()
                timer.join(timeout=5)
            if worker:
                try:
                    stop_worker(worker)
                except Exception as exc:
                    error = error or type(exc).__name__
            if current and not any(run["id"] == current for run in runs):
                runs.append(store.run(current))
            json.dump(
                {
                    "mode": "single",
                    "model": args.model,
                    "profile": args.profile,
                    "reasoning_effort": "low",
                    "target": target,
                    "budget": budget.record(),
                    "runs": runs,
                    "terminal_reason": store.halt_reason,
                    "error_type": error,
                    "evidence_directory": str(evidence),
                    "logical_agents": 1,
                    "hidden_collaboration": False,
                },
                output,
                indent=2,
            )
            output.flush()
            os.fsync(output.fileno())


def summarize(output, budget):
    """Use assignment rows, including failures and active calls, for provider usage."""
    import sqlite3

    evidence = output.with_suffix(".json.data")
    with closing(sqlite3.connect(evidence / "state.db")) as db:
        assignments = db.execute("SELECT status,result FROM assignments").fetchall()
        # Group checks persist an explicit unknown-time marker. Independent runs
        # do not; a zero-time internal verifier error must not look cost-free.
        checks = db.execute(
            "SELECT v.status,CASE WHEN u.attempt_id IS NOT NULL "
            "OR (v.status='verifier_error' AND v.elapsed_ms=0) THEN NULL "
            "ELSE v.elapsed_ms END FROM verifications v "
            "LEFT JOIN group_unknown_lean_time u ON u.attempt_id=v.attempt_id"
        ).fetchall()
        jobs = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
        auxiliary_checks = db.execute(
            "SELECT status,elapsed_ms FROM group_lean_checks"
        ).fetchall()
        composed = [
            {
                "owner_id": owner,
                "owner_kind": kind,
                "status": status,
                "committed": bool(committed),
                "usage": json.loads(usage),
            }
            for owner, kind, status, committed, usage in db.execute(
                "SELECT owner_id,owner_kind,status,committed,usage FROM composed_checks ORDER BY id"
            )
        ]
        corrections = dict(
            db.execute(
                "SELECT strategy,count(*) FROM frontier_decisions WHERE job_id IS NOT NULL GROUP BY strategy"
            ).fetchall()
        )
    known_input, known_output, unknown = 0, 0, 0
    for _, raw in assignments:
        usage = json.loads(raw).get("usage", {}) if raw else {}
        i, o = usage.get("input_tokens"), usage.get("output_tokens")
        known_input += i if i is not None else 0
        known_output += o if o is not None else 0
        unknown += i is None or o is None
    cost = (
        known_input * budget.input_usd_per_million
        + known_output * budget.output_usd_per_million
    ) / Decimal(1000000)
    record = json.loads(output.read_text())
    single = record.get("mode") == "single"
    snapshot = record.get("snapshot")
    return {
        "record": str(output),
        "record_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "autonomous_target_accepted": any(
            run["status"] == "solved" for run in record.get("runs", [])
        )
        if single
        else bool(record.get("run") and record["run"]["status"] == "solved"),
        "jobs": jobs,
        "assignments_provider_call_upper_bound": len(assignments),
        "failed_assignments": sum(status != "completed" for status, _ in assignments),
        "input_tokens_known": known_input,
        "output_tokens_known": known_output,
        "unknown_usage_assignments": unknown,
        "known_usage_estimate_usd": str(cost),
        "total_usage_estimate_usd": None if unknown else str(cost),
        "provider_bill_usd": None,
        "target_lean_checks": len(checks),
        "target_lean_elapsed_ms": sum(ms for _, ms in checks if ms is not None),
        "target_rejections": sum(status == "rejected" for status, _ in checks),
        "target_repairs": max(0, len(record.get("runs", [])) - 1)
        if single
        else sum(
            count
            for strategy, count in corrections.items()
            if strategy.partition("|")[0] == "target-correction"
        ),
        "auxiliary_repairs": sum(
            count
            for strategy, count in corrections.items()
            if strategy.partition("|")[0] == "auxiliary-correction"
        ),
        "auxiliary_lean_checks": len(auxiliary_checks),
        "total_lean_checks": len(checks) + len(auxiliary_checks),
        "total_lean_known_elapsed_ms": sum(
            ms for _, ms in checks + auxiliary_checks if ms is not None
        ),
        "lean_unknown_elapsed_entries": sum(
            ms is None for _, ms in checks + auxiliary_checks
        ),
        "checked_dependency_use_receipts": composed,
        "graph_cost": snapshot["group"]["cost"] if snapshot else None,
        "terminal_reason": record.get("terminal_reason"),
    }


def execute_batch(args, budget):
    # Exclusive fresh directory forbids accidental resume/retry or concurrent spending.
    args.output.mkdir(mode=0o700)
    ledger = Ledger(args.output, budget)
    for problem in PROBLEMS:
        target = ledger.record["inputs"][problem]
        fixture = args.output / f"{problem}-input.json"
        with fixture.open("x") as stream:
            json.dump(target, stream, indent=2)
        for mode in MODES:
            name = f"{problem}-{mode}"
            output = args.output / f"{name}.json"
            observation_args = SimpleNamespace(
                **{**vars(args), "output": output, "fixture": fixture}
            )
            ledger.start(name)
            try:
                if mode == "group":
                    observe(observation_args, budget)
                else:
                    baseline(observation_args, budget, target)
                ledger.finish(name, summarize(output, budget))
            except Exception as exc:
                ledger.finish(name, error=type(exc).__name__)
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=["gpt-6.1-sol"], default="gpt-6.1-sol")
    parser.add_argument(
        "--profile", choices=["responses-reasoning"], default="responses-reasoning"
    )
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, required=True, help="Fresh batch directory"
    )
    parser.add_argument("--execute-paid", action="store_true")
    args = parser.parse_args()
    budget = fixed_budget()
    print(json.dumps(preflight(budget), indent=2), flush=True)
    if args.execute_paid:
        execute_batch(args, budget)


if __name__ == "__main__":
    main()
