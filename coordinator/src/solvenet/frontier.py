"""Small deterministic graph policy. Suggestions are advisory, never prerequisites."""

import hashlib
import json

from . import protocol_limits
from .composed import encode
from .context_packet import (
    build_auxiliary_correction,
    build_packet,
    build_target_correction,
    freeze_packet,
)
from .group_operations import create_group_run, insert_group_job, stop_group_jobs
from .group_routing import choose
from .group_state import _require_group

DEFAULT_LIMITS = {
    "planning_calls": 8,
    "verification_operations": 24,
    "lean_elapsed_ms": 180000,
    "included_lemmas": 8,
    "source_bytes": 32768,
    "packet_bytes": 6144,
    "retries": 1,
    "target_corrections": 0,
    "direct_auxiliary_correction": 0,
    "completion_reserve": 0,
    "completion_check_ms": 10000,
    "response_output_tokens": 512,
}

MIGRATION_23 = """
ALTER TABLE group_loops ADD COLUMN mode TEXT NOT NULL DEFAULT 'fixed';
ALTER TABLE group_loops ADD COLUMN limits TEXT NOT NULL DEFAULT '{}';
CREATE TABLE frontier_decisions (
 id INTEGER PRIMARY KEY, group_id TEXT NOT NULL REFERENCES agent_groups(id),
 claim_id TEXT NOT NULL, action TEXT NOT NULL, strategy TEXT NOT NULL,
 graph_revision INTEGER NOT NULL, reason TEXT NOT NULL, deferred TEXT NOT NULL,
 task_id TEXT REFERENCES group_tasks(id), job_id TEXT REFERENCES jobs(id),
 reservation INTEGER NOT NULL, processed INTEGER NOT NULL DEFAULT 0,
 UNIQUE(group_id,claim_id,action,strategy));
CREATE INDEX frontier_group ON frontier_decisions(group_id,id);
CREATE TABLE graph_action_proposals (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES agent_groups(id),
 job_id TEXT NOT NULL REFERENCES jobs(id), claim_id TEXT NOT NULL,
 action TEXT NOT NULL, priority INTEGER NOT NULL, reason TEXT NOT NULL);
CREATE INDEX graph_proposals_group ON graph_action_proposals(group_id,claim_id);
CREATE TABLE frontier_lean_reservations (
 check_id INTEGER PRIMARY KEY REFERENCES composed_checks(id),
 group_id TEXT NOT NULL REFERENCES agent_groups(id), deadline_ms INTEGER NOT NULL,
 elapsed_ms INTEGER, subprocesses INTEGER, finished INTEGER NOT NULL DEFAULT 0);
CREATE INDEX frontier_lean_group ON frontier_lean_reservations(group_id);
PRAGMA user_version = 23;
"""


class FrontierLimit(Exception):
    """A durable ceiling prevents another verifier execution."""


def validate_limits(value):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(DEFAULT_LIMITS):
        raise ValueError("Invalid graph limits")
    result = DEFAULT_LIMITS | value
    for key, maximum in DEFAULT_LIMITS.items():
        if key in (
            "target_corrections",
            "completion_reserve",
            "direct_auxiliary_correction",
        ):
            maximum = 1
        if key == "completion_check_ms":
            maximum = 180000
        if key == "response_output_tokens":
            maximum = protocol_limits.MAX_OUTPUT_TOKENS
        if (
            type(result[key]) is not int
            or not (
                0
                if key
                in (
                    "retries",
                    "target_corrections",
                    "completion_reserve",
                    "direct_auxiliary_correction",
                )
                else 1
            )
            <= result[key]
            <= maximum
        ):
            raise ValueError("Invalid graph limit: " + key)
    return result


def completion_tail(db, loop):
    """Unspent completion jobs; retry leases remain charged by ordinary admission.

    Use the largest configured synthesis cost so heterogeneous routing cannot
    consume a tail that only fits the cheapest model. Defaults leave policy intact.
    """
    limits = json.loads(loop["limits"])
    if not limits.get("completion_reserve", 0):
        return {"jobs": 0, "work": 0, "checks": 0, "lean_ms": 0}
    jobs = db.execute(
        "SELECT count(*) FROM frontier_decisions WHERE group_id=? AND action='synthesize' AND job_id IS NOT NULL",
        (loop["group_id"],),
    ).fetchone()[0]
    checks = db.execute(
        """SELECT count(*) FROM frontier_lean_reservations r
        JOIN composed_checks c ON c.id=r.check_id
        WHERE r.group_id=? AND c.owner_kind='attempt'""",
        (loop["group_id"],),
    ).fetchone()[0]
    slots = 1 + limits.get("target_corrections", 0)
    models = json.loads(loop["models"])["synthesizer"]
    models = [models] if isinstance(models, str) else models
    capabilities = json.loads(loop["capabilities"])
    cost = max(capabilities.get(m, {}).get("cost", 1) for m in models)
    return {
        "jobs": max(0, slots - jobs),
        "work": max(0, slots - jobs) * 2 * cost,
        "checks": max(0, slots - checks),
        "lean_ms": max(0, slots - checks) * limits["completion_check_ms"],
    }


def stop(db, store, group_id, reason):
    stop_group_jobs(db, group_id, reason)
    db.execute(
        "UPDATE group_tasks SET status='blocked' WHERE group_id=? AND status='open'",
        (group_id,),
    )
    store._refresh(db)
    return True


def lean_cost(db, group_id):
    rows = db.execute(
        "SELECT * FROM frontier_lean_reservations WHERE group_id=?", (group_id,)
    ).fetchall()
    return {
        "operations": len(rows),
        "elapsed_ms": sum(
            r["elapsed_ms"] if r["elapsed_ms"] is not None else r["deadline_ms"]
            for r in rows
        ),
        "unknown_elapsed": sum(r["elapsed_ms"] is None for r in rows),
        "subprocesses": sum(r["subprocesses"] or 0 for r in rows),
        "unknown_subprocesses": sum(r["subprocesses"] is None for r in rows),
    }


def reserve_check(db, store, check_id, owner_kind, bundle, deadline_ms):
    loop = db.execute(
        "SELECT * FROM group_loops WHERE group_id=?", (bundle["group_id"],)
    ).fetchone()
    if not loop or loop["mode"] != "graph":
        return
    if owner_kind == "artifact":
        packet = db.execute(
            """SELECT p.request,p.manifest FROM composed_checks c
            JOIN group_artifacts a ON a.id=c.owner_id
            JOIN context_packets p ON p.job_id=a.job_id WHERE c.id=?""",
            (check_id,),
        ).fetchone()
        if packet and json.loads(packet["request"]).get("strategy", "").startswith(
            "auxiliary-correction|"
        ):
            from .proof_context import binding_matches

            frozen = json.loads(packet["manifest"])
            if not binding_matches(db, frozen) or any(
                bundle[key] != frozen[key]
                for key in ("verifier_identity", "verifier_revision")
            ):
                raise FrontierLimit("verifier_binding_changed")
    if owner_kind == "attempt":
        packet = db.execute(
            """SELECT p.request,p.manifest FROM composed_checks c
            JOIN attempts t ON t.id=c.owner_id
            JOIN assignments a ON a.id=t.assignment_id
            JOIN context_packets p ON p.job_id=a.job_id WHERE c.id=?""",
            (check_id,),
        ).fetchone()
        if packet and json.loads(packet["request"]).get("strategy", "").startswith(
            "target-correction|"
        ):
            from .proof_context import binding_matches

            frozen = json.loads(packet["manifest"])
            if not binding_matches(db, frozen) or any(
                bundle[key] != frozen[key]
                for key in ("verifier_identity", "verifier_revision")
            ):
                raise FrontierLimit("verifier_binding_changed")
    reason = lean_budget_reason(db, loop)
    limits = json.loads(loop["limits"])
    if reason is None and limits.get("completion_reserve", 0):
        tail = completion_tail(db, loop)
        cost = lean_cost(db, loop["group_id"])
        remaining_checks = max(0, tail["checks"] - (owner_kind == "attempt"))
        remaining_ms = remaining_checks * limits["completion_check_ms"]
        if (
            cost["operations"] + 1 + remaining_checks
            > limits["verification_operations"]
        ):
            reason = "completion_verification_reserve"
        elif (
            cost["elapsed_ms"] + deadline_ms + remaining_ms > limits["lean_elapsed_ms"]
        ):
            reason = "completion_lean_time_reserve"
    if reason is None and store.clock() >= loop["deadline"] and owner_kind != "attempt":
        reason = "deadline"
    # Only a target dispatched before deadline may drain, with the same ceilings.
    if reason:
        raise FrontierLimit(reason)
    db.execute(
        "INSERT INTO frontier_lean_reservations(check_id,group_id,deadline_ms) VALUES (?,?,?)",
        (check_id, bundle["group_id"], deadline_ms),
    )


def lean_budget_reason(db, loop):
    limits = json.loads(loop["limits"])
    cost = lean_cost(db, loop["group_id"])
    if cost["operations"] >= limits["verification_operations"]:
        return "verification_budget"
    if cost["elapsed_ms"] >= limits["lean_elapsed_ms"]:
        return "lean_time_budget"
    return None


def finish_check(db, check_id, result, usage):
    if check_id is not None:
        db.execute(
            "UPDATE frontier_lean_reservations SET elapsed_ms=?,subprocesses=?,finished=1 WHERE check_id=? AND finished=0",
            (
                result.elapsed_ms if not usage.get("elapsed_unknown") else None,
                usage.get("subprocesses"),
                check_id,
            ),
        )


def _correction_source(db, group_id, history, limits):
    if not limits.get("target_corrections", 0) or any(
        r["strategy"].startswith("target-correction|") for r in history
    ):
        return None
    return db.execute(
        """SELECT t.id,d.task_id FROM attempts t
        JOIN verifications v ON v.attempt_id=t.id
        JOIN assignments a ON a.id=t.assignment_id
        JOIN frontier_decisions d ON d.job_id=a.job_id
        WHERE d.group_id=? AND d.action='synthesize' AND v.status='rejected'
        ORDER BY t.rowid DESC LIMIT 1""",
        (group_id,),
    ).fetchone()


def _direct_auxiliary_candidate(
    db, cid, rejected, history, limits, candidate, candidates, proposal
):
    """One durable correction per claim, independent of artifact/manifest churn."""
    if proposal and proposal["action"] == "critique":
        candidate(
            cid,
            "critique",
            1,
            "attributed strategy help request",
            proposal["id"],
            rejected_artifact_ids=[rejected["id"]],
        )
        return
    corrections = [
        r
        for r in history
        if r["claim_id"] == cid and r["strategy"].startswith("auxiliary-correction|")
    ]
    if not corrections and limits["retries"] and rejected["status"] == "rejected":
        source = db.execute(
            """SELECT j.model,t.owner_id FROM jobs j
            JOIN group_tasks t ON t.id=? WHERE j.id=?""",
            (rejected["task_id"], rejected["job_id"]),
        ).fetchone()
        if source:
            candidate(
                cid,
                "investigate",
                -1,
                "bounded direct auxiliary correction from exact Lean rejection and frozen context",
                "auxiliary-correction",
                rejected_artifact_ids=[rejected["id"]],
            )
            candidates[-1].update(
                parent=rejected["task_id"],
                owner_id=source["owner_id"],
                owner_model=source["model"],
            )
            return
    candidate(
        cid,
        "critique",
        1,
        "repeated formal failure or unavailable direct correction redirects branch",
        "rejected",
        rejected_artifact_ids=[rejected["id"]],
    )


def _target_correction_candidate(
    db, group_id, root, history, limits, candidate, candidates
):
    failed = _correction_source(db, group_id, history, limits)
    if failed:
        candidate(
            root,
            "synthesize",
            -1,
            "bounded target correction from exact Lean rejection and frozen context",
            "target-correction",
            target_attempt_ids=[failed["id"]],
        )
        candidates[-1]["parent"] = failed["task_id"]


def _frontier_candidates(db, group_id, root, history, planning, limits, binding):
    """Return ranked candidates and deferrals from bounded graph and observed evidence."""
    # Indexed bounded traversal. Cycles and abandoned suggestions do not gate the root.
    edges = db.execute(
        """WITH RECURSIVE reach(id,depth) AS (
        SELECT ?,0 UNION SELECT e.to_id,reach.depth+1 FROM claim_relationships e
        JOIN reach ON e.from_id=reach.id WHERE e.group_id=? AND e.kind='suggests_using' AND reach.depth<3)
        SELECT e.*,v.status AS opinion,v.id AS review_id FROM claim_relationships e
        LEFT JOIN claim_relationship_reviews v ON v.rowid=(SELECT max(v2.rowid)
            FROM claim_relationship_reviews v2 WHERE v2.group_id=e.group_id AND v2.relationship_id=e.id)
        WHERE e.group_id=? AND e.from_id IN (SELECT id FROM reach LIMIT 16)
        ORDER BY e.rowid LIMIT 32""",
        (root, group_id, group_id),
    ).fetchall()
    nodes = {root: {"opinion": "target", "review_id": ""}}
    for e in edges:
        if e["to_id"] != root:
            current = nodes.get(e["to_id"])
            if current is None or e["opinion"] in ("challenged", "abandoned"):
                nodes[e["to_id"]] = {
                    "opinion": e["opinion"] or "suggested",
                    "review_id": e["review_id"] or "",
                    "relationship_id": e["id"],
                }
        if len(nodes) >= 16:
            break
    artifacts = db.execute(
        """SELECT a.*,ca.claim_id FROM group_artifacts a JOIN claim_artifacts ca
        ON ca.artifact_id=a.id WHERE a.group_id=? ORDER BY a.rowid LIMIT 64""",
        (group_id,),
    ).fetchall()
    checked = {
        a["claim_id"]
        for a in artifacts
        if a["status"] == "verified" and a["verifier_identity"] == binding["identity"]
    }
    proposals = {
        r["claim_id"]: dict(r)
        for r in db.execute(
            "SELECT * FROM graph_action_proposals WHERE group_id=? ORDER BY rowid LIMIT 128",
            (group_id,),
        )
    }
    candidates, deferred = [], []

    def candidate(cid, action, rank, reason, strategy="default", **evidence):
        if action in ("plan", "critique") and planning >= limits["planning_calls"]:
            deferred.append(
                {"claim_id": cid, "action": action, "reason": "planning_critique_cap"}
            )
            return
        priority = proposals.get(cid, {}).get("priority", 0)
        candidates.append(
            {
                "claim_id": cid,
                "action": action,
                "rank": rank,
                "priority": priority,
                "reason": reason,
                "strategy": strategy,
                "avoid": None,
                "parent": None,
                "evidence": evidence,
            }
        )

    if not history:
        candidate(root, "plan", -1, "initial bounded decomposition", "initial")
    else:
        for cid, node in nodes.items():
            if cid == root:
                continue
            if node["opinion"] == "abandoned":
                deferred.append(
                    {
                        "claim_id": cid,
                        "action": "investigate",
                        "reason": "abandoned_advisory_suggestion",
                    }
                )
                continue
            if node["opinion"] == "challenged":
                candidate(
                    cid,
                    "critique",
                    1,
                    "independent reconsideration of reviewed challenge",
                    node["review_id"],
                    relationship_ids=[node["relationship_id"]],
                    review_ids=[node["review_id"]],
                )
            elif cid in checked:
                deferred.append(
                    {"claim_id": cid, "action": "prove", "reason": "currently_checked"}
                )
                continue
            else:
                attempted = any(
                    r["claim_id"] == cid
                    and r["action"] == "investigate"
                    and r["status"] == "done"
                    for r in history
                )
                rejected = next(
                    (
                        a
                        for a in reversed(artifacts)
                        if a["claim_id"] == cid
                        and a["status"] in ("rejected", "timeout", "verifier_error")
                    ),
                    None,
                )
                if rejected:
                    if limits.get("direct_auxiliary_correction", 0):
                        _direct_auxiliary_candidate(
                            db,
                            cid,
                            rejected,
                            history,
                            limits,
                            candidate,
                            candidates,
                            proposals.get(cid),
                        )
                        continue
                    candidate(
                        cid,
                        "critique",
                        1,
                        "negative formal evidence redirects branch",
                        "rejected",
                        rejected_artifact_ids=[rejected["id"]],
                    )
                    critiques = [
                        r
                        for r in history
                        if r["claim_id"] == cid
                        and r["action"] == "critique"
                        and r["status"] == "done"
                        and r["strategy"].startswith("rejected|context:")
                    ]
                    findings = [
                        r
                        for r in history
                        if r["claim_id"] == cid and r["action"] == "investigate"
                    ]
                    if critiques and findings and len(findings) <= limits["retries"]:
                        messages = [
                            r[0]
                            for r in db.execute(
                                """SELECT m.id,p.request FROM group_messages m JOIN claim_messages cm
                            ON cm.message_id=m.id JOIN context_packets p ON p.job_id=m.job_id
                            WHERE m.group_id=? AND cm.claim_id=?
                            AND m.job_id=? AND m.kind='critique' ORDER BY m.rowid LIMIT 1""",
                                (group_id, cid, critiques[-1]["job_id"]),
                            )
                            if rejected["id"]
                            in json.loads(r["request"]).get("rejected_artifact_ids", [])
                        ]
                        if messages:
                            candidate(
                                cid,
                                "investigate",
                                2,
                                "bounded auxiliary repair from formal rejection and critique",
                                "auxiliary-repair",
                                rejected_artifact_ids=[rejected["id"]],
                                critique_ids=messages,
                            )
                            candidates[-1]["parent"] = findings[-1]["task_id"]
                            candidates[-1]["avoid"] = findings[-1]["model"]
                elif (
                    node["opinion"] == "promising"
                    or attempted
                    or proposals.get(cid, {}).get("action") == "prove"
                ):
                    candidate(
                        cid,
                        "prove",
                        2,
                        "promising or investigated target-relevant claim",
                    )
                else:
                    candidate(
                        cid,
                        "investigate",
                        3,
                        "bounded unresolved target suggestion",
                        relationship_ids=[node["relationship_id"]],
                    )
            help_request = proposals.get(cid)
            if help_request and help_request["action"] == "critique":
                action = help_request["action"]
                candidate(
                    cid,
                    action,
                    {"critique": 1, "prove": 2, "investigate": 3}[action],
                    "attributed priority or help request",
                    help_request["id"],
                )
        # Always admit a direct target attempt, regardless of planning cycles.
        context = sorted(
            a["id"]
            for a in artifacts
            if a["claim_id"] in nodes
            and a["claim_id"] != root
            and a["status"] == "verified"
            and a["verifier_identity"] == binding["identity"]
            and nodes[a["claim_id"]]["opinion"] not in ("abandoned", "challenged")
        )
        candidate(
            root,
            "synthesize",
            0 if context else 4,
            "try target with newly checked relevant context"
            if context
            else "direct target fallback; suggestions are advisory",
        )
        # Trigger planning from actual observed evidence, never the revision alone.
        events = [
            str(r[0])
            for r in db.execute(
                """SELECT rowid FROM group_artifact_outcomes
            WHERE group_id=? AND status IN ('verified','rejected','timeout','verifier_error') ORDER BY rowid LIMIT 24""",
                (group_id,),
            )
        ]
        events += [
            r["review_id"] for r in nodes.values() if r["opinion"] == "challenged"
        ]
        events += [r["job_id"] for r in history if r["status"] == "failed"]
        verdicts = db.execute(
            """SELECT v.attempt_id FROM verifications v
            JOIN attempts t ON t.id=v.attempt_id JOIN assignments a ON a.id=t.assignment_id
            JOIN group_jobs gj ON gj.job_id=a.job_id WHERE gj.group_id=?
            AND v.status IN ('rejected','timeout') ORDER BY t.rowid DESC LIMIT 1""",
            (group_id,),
        ).fetchall()
        events += ["target:" + r["attempt_id"] for r in verdicts]
        _target_correction_candidate(
            db, group_id, root, history, limits, candidate, candidates
        )
        if events and sum(r["action"] == "plan" for r in history) < 3:
            candidate(
                root,
                "plan",
                5,
                "bounded evidence-triggered replanning",
                hashlib.sha256(encode(events).encode()).hexdigest(),
                target_attempt_ids=[r["attempt_id"] for r in verdicts],
            )
    candidates.sort(
        key=lambda c: (c["rank"], -c["priority"], c["claim_id"], c["action"])
    )
    return candidates, deferred


def _admit_candidate(
    store, db, group_id, group, loop, limits, history, candidates, deferred
):
    """Return the first packet/model admission; annotate retries and deferrals in place."""
    from .store import Conflict

    models = json.loads(loop["models"])
    models = {r: [m] if isinstance(m, str) else m for r, m in models.items()}
    capabilities = json.loads(loop["capabilities"])
    selected = None
    tail = completion_tail(db, loop)
    lean = lean_cost(db, group_id)
    for c in candidates:
        action = c["action"]
        completion = action == "synthesize"
        held_jobs = max(0, tail["jobs"] - 1) if completion else tail["jobs"]
        if tail["jobs"] and len(history) + 1 + held_jobs > group["max_tasks"]:
            deferred.append(c | {"reason": "completion_job_reserve"})
            continue
        if action in ("investigate", "prove") and tail["checks"]:
            if lean["operations"] >= limits["verification_operations"] - tail["checks"]:
                deferred.append(c | {"reason": "completion_verification_reserve"})
                continue
            if (
                lean["elapsed_ms"] + limits["completion_check_ms"]
                > limits["lean_elapsed_ms"] - tail["lean_ms"]
            ):
                deferred.append(c | {"reason": "completion_lean_time_reserve"})
                continue
        role = (
            "planner"
            if action == "plan"
            else "critic"
            if action == "critique"
            else "synthesizer"
            if action == "synthesize"
            else "investigator"
        )
        task_type = (
            "plan"
            if action == "plan"
            else "critique"
            if action == "critique"
            else "proof"
            if action == "synthesize"
            else "finding"
        )
        output_tokens = (
            2048
            if action == "synthesize"
            else limits.get("response_output_tokens", 512)
        )
        messages = [
            {
                "role": "user",
                "content": (
                    f"Frontier action: {action}. Focus claim: {c['claim_id']}. "
                    "For synthesis return a proof body of the original target. "
                    "For other actions follow GRAPH_RESPONSE."
                    + (
                        " Correct the quoted failed candidate using its exact Lean diagnostics. "
                        "The checked lemma context is frozen from that attempt. Return only a new proof body."
                        if c["strategy"] == "target-correction"
                        else ""
                    )
                ),
            }
        ]
        try:
            context_limit = min(
                8192,
                max(
                    capabilities.get(m, {}).get("context_tokens", 8192)
                    for m in models[role]
                ),
            )
            correction = c["strategy"] in ("target-correction", "auxiliary-correction")
            built = (
                build_auxiliary_correction(
                    db,
                    group_id,
                    c["evidence"]["rejected_artifact_ids"][0],
                    messages,
                    max_bytes=limits["packet_bytes"],
                    context_limit=context_limit,
                )
                if c["strategy"] == "auxiliary-correction"
                else build_target_correction(
                    db,
                    group_id,
                    c["evidence"]["target_attempt_ids"][0],
                    messages,
                    max_bytes=limits["packet_bytes"],
                    context_limit=context_limit,
                )
                if correction
                else build_packet(
                    store,
                    db,
                    group_id,
                    c["claim_id"],
                    messages,
                    task_type=task_type,
                    max_output_tokens=output_tokens,
                    max_bytes=limits["packet_bytes"],
                    context_limit=min(
                        8192,
                        max(
                            capabilities.get(m, {}).get("context_tokens", 8192)
                            for m in models[role]
                        ),
                    ),
                    **c["evidence"],
                )
            )
            manifest = built["manifest"]
            if (
                len(manifest["declarations"]) > limits["included_lemmas"]
                or len(encode(manifest).encode()) > limits["source_bytes"]
            ):
                if correction:
                    raise ValueError("Frozen target correction manifest exceeds limits")
                built = build_packet(
                    store,
                    db,
                    group_id,
                    c["claim_id"],
                    messages,
                    proof_ids=[],
                    task_type=task_type,
                    max_output_tokens=output_tokens,
                    max_bytes=limits["packet_bytes"],
                    **c["evidence"],
                )
            # Packet revisions include work/evidence writes. Only actual selected
            # declarations and explicit strategy changes create a new attempt key.
            context_key = hashlib.sha256(
                encode(built["manifest"]["declarations"]).encode()
            ).hexdigest()
            strategy = c["strategy"] + "|context:" + context_key
            prior = [
                r
                for r in history
                if r["claim_id"] == c["claim_id"]
                and r["action"] == action
                and r["strategy"].split("|retry:")[0] == strategy
            ]
            if prior:
                retries = (
                    0
                    if action == "synthesize" and limits.get("target_corrections", 0)
                    else limits["retries"]
                )
                if len(prior) > retries or action in ("plan", "critique"):
                    deferred.append(c | {"reason": "equivalent_strategy_exhausted"})
                    continue
                c["reason"] += "; bounded independent retry after completed attempt"
                c["avoid"], c["parent"] = prior[-1]["model"], prior[-1]["task_id"]
                strategy += "|retry:" + str(len(prior))
            c["strategy"] = strategy
            model, cost, routing = choose(
                db,
                group_id,
                models | {role: [c["owner_model"]]} if c.get("owner_model") else models,
                capabilities,
                role,
                task_type,
                built["budget"]["input_byte_upper_bound"],
                group["remaining_work"]
                - (
                    tail["work"] * max(0, tail["jobs"] - 1) // tail["jobs"]
                    if completion and tail["jobs"]
                    else 0
                    if completion
                    else tail["work"]
                ),
                avoid=c["avoid"],
                now=store.clock(),
                lease_seconds=store.lease_seconds,
                context_token_bound=built["budget"]["admission_upper_bound"],
            )
        except (ValueError, Conflict) as error:
            deferred.append(c | {"reason": "context_unfit: " + str(error)})
            continue
        if model is None:
            deferred.append(
                c
                | {
                    "reason": "completion_work_reserve"
                    if not completion
                    and tail["work"]
                    and any(
                        "budget_exceeded" in r["reasons"]
                        for r in json.loads(routing)["candidates"]
                    )
                    else "model_unavailable_or_budget",
                    "routing": json.loads(routing),
                }
            )
            continue
        selected = {
            "candidate": c,
            "role": role,
            "task_type": task_type,
            "packet": built,
            "model": model,
            "cost": cost,
            "routing": routing,
        }
        break
    return selected


def _dispatch_frontier_task(
    store,
    db,
    group_id,
    group,
    run,
    limits,
    history,
    planning,
    selected,
    candidates,
    deferred,
):
    """Persist the admitted task, reservation, frozen packet and decisions in the caller transaction."""
    from .claim_graph import attach_task
    from .store import identifier

    c = selected["candidate"]
    role, task_type = selected["role"], selected["task_type"]
    built, model, cost, routing = (
        selected["packet"],
        selected["model"],
        selected["cost"],
        selected["routing"],
    )
    agents = {
        r["request_key"]: r["id"]
        for r in db.execute("SELECT * FROM group_agents WHERE group_id=?", (group_id,))
    }
    if len(history) >= group["max_tasks"]:
        return stop(db, store, group_id, "task_limit")
    owner = (
        role
        if role != "investigator"
        else ("investigator-2" if c["parent"] else "investigator-1")
    )
    owner_id = c.get("owner_id", agents[owner])
    task_id, job_id = identifier(), identifier()
    key = "frontier:" + str(len(history))
    parent = (
        db.execute(
            "SELECT depth FROM group_tasks WHERE id=?", (c["parent"],)
        ).fetchone()
        if c["parent"]
        else None
    )
    # Bound work lineage independently of mathematical suggestion cycles.
    if parent and parent["depth"] >= 4:
        return stop(db, store, group_id, "task_depth")
    db.execute(
        """INSERT INTO group_tasks
        (id,group_id,request_key,parent_id,creator_id,owner_id,description,budget,remaining,depth)
        VALUES (?,?,?,?,?,?,?,?,0,?)""",
        (
            task_id,
            group_id,
            key,
            c["parent"],
            agents["planner"],
            owner_id,
            c["reason"],
            cost,
            parent["depth"] + 1 if parent else 0,
        ),
    )
    attach_task(
        db,
        group_id,
        task_id,
        c["claim_id"],
        c["action"] if c["action"] != "plan" else "investigate",
    )
    db.execute(
        "UPDATE agent_groups SET remaining_work=remaining_work-? WHERE id=?",
        (cost, group_id),
    )
    if not run:
        run_id = create_group_run(db, group, store.clock())
    else:
        run_id = run["id"]
        db.execute(
            "UPDATE runs SET status='running' WHERE id=? AND status='exhausted'",
            (run_id,),
        )
    kind = "model.generate" if c["action"] == "synthesize" else "model.respond"
    insert_group_job(
        db,
        job_id,
        run_id,
        group_id,
        key,
        task_id,
        owner_id,
        group["environment"],
        cost,
        model=model,
        max_output_tokens=built["budget"]["output_token_allowance"],
        max_assignments=min(2, limits["planning_calls"] - planning)
        if c["action"] in ("plan", "critique")
        else 2,
        kind=kind,
        task_type=None if kind == "model.generate" else task_type,
        messages=json.dumps(built["messages"]),
    )
    freeze_packet(
        db,
        job_id,
        group_id,
        task_id,
        built,
        dict(action=c["action"], strategy=c["strategy"], **c["evidence"]),
    )
    deferred += [
        a | {"reason": "lower_rank_than_selected"}
        for a in candidates
        if a is not c
        and not any(
            d.get("claim_id") == a["claim_id"] and d.get("action") == a["action"]
            for d in deferred
        )
    ]
    db.execute(
        """INSERT INTO frontier_decisions
        (group_id,claim_id,action,strategy,graph_revision,reason,deferred,task_id,job_id,reservation)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            group_id,
            c["claim_id"],
            c["action"],
            c["strategy"],
            built["graph_revision"],
            c["reason"],
            encode(deferred),
            task_id,
            job_id,
            cost,
        ),
    )
    db.execute(
        "INSERT INTO group_route_decisions VALUES (?,?,?,?)",
        (group_id, key, job_id, routing),
    )
    return True


class Frontier:
    def stop_frontier(self, group_id, reason):
        with self.transaction() as db:
            return stop(db, self, group_id, reason)

    def stop_unchecked_target(self, attempt_id, group_id, reason):
        """No Lean verdict for an input that could not reserve verification capacity."""
        with self.transaction() as db:
            if (
                db.execute(
                    "SELECT 1 FROM verification_ownership WHERE owner_kind='attempt' AND owner_id=?",
                    (attempt_id,),
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM verifications WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
            ):
                return False
            db.execute(
                """UPDATE jobs SET status='cancelled' WHERE status='verifying' AND id IN
                (SELECT a.job_id FROM assignments a JOIN attempts t ON t.assignment_id=a.id WHERE t.id=?)""",
                (attempt_id,),
            )
            return stop(db, self, group_id, reason)

    def frontier_trace(self, group_id):
        with self.connect() as db:
            decisions = read_decisions(db, group_id)
            return {
                "decisions": decisions,
                "lean": lean_cost(db, group_id),
                "model": model_cost(db, group_id),
                "completion_tail": completion_tail(
                    db,
                    db.execute(
                        "SELECT * FROM group_loops WHERE group_id=?", (group_id,)
                    ).fetchone(),
                ),
            }

    def advance_frontier(self, group_id):
        with self.transaction() as db:
            loop = db.execute(
                "SELECT * FROM group_loops WHERE group_id=?", (group_id,)
            ).fetchone()
            if loop["phase"] == "stopped":
                return False
            group = _require_group(db, group_id)
            limits = json.loads(loop["limits"])
            graph = db.execute(
                "SELECT * FROM group_graphs WHERE group_id=?", (group_id,)
            ).fetchone()
            root = graph["root_id"]
            run = db.execute(
                "SELECT r.* FROM runs r JOIN group_runs gr ON gr.run_id=r.id WHERE gr.group_id=?",
                (group_id,),
            ).fetchone()
            if run and run["status"] == "solved":
                return stop(db, self, group_id, "verified_target")
            if run and run["status"] == "error":
                return stop(db, self, group_id, "verifier_error")
            if self.clock() >= loop["deadline"]:
                return stop(db, self, group_id, "deadline")
            # Process persisted completions once, including rejected JSON and worker failures.
            for decision in db.execute(
                """SELECT d.*,j.status,j.kind FROM frontier_decisions d
                JOIN jobs j ON j.id=d.job_id WHERE d.group_id=? AND d.processed=0 ORDER BY d.id""",
                (group_id,),
            ).fetchall():
                if decision["status"] in ("queued", "assigned", "verifying"):
                    return False
                if decision["status"] == "done" and decision["kind"] == "model.respond":
                    self._ingest_group_graph_response(db, group_id, decision["job_id"])
                db.execute(
                    "UPDATE frontier_decisions SET processed=1 WHERE id=?",
                    (decision["id"],),
                )
                db.execute(
                    "UPDATE group_tasks SET status=? WHERE id=?",
                    (
                        "done" if decision["status"] == "done" else "blocked",
                        decision["task_id"],
                    ),
                )
            if db.execute(
                """SELECT 1 FROM group_jobs gj JOIN jobs j ON j.id=gj.job_id
                WHERE gj.group_id=? AND j.status IN ('queued','assigned','verifying') LIMIT 1""",
                (group_id,),
            ).fetchone():
                return False
            if db.execute(
                "SELECT 1 FROM group_artifacts WHERE group_id=? AND status='pending'",
                (group_id,),
            ).fetchone():
                return False
            # Existing dispatched work drains above. Do not buy another proof-producing
            # model assignment when no subsequent Lean operation can be admitted.
            reason = lean_budget_reason(db, loop)
            if reason:
                return stop(db, self, group_id, reason)
            binding = db.execute(
                "SELECT * FROM artifact_verifier_binding WHERE id=1"
            ).fetchone()
            if not binding["identity"] or self.artifact_verifier_binding != (
                binding["identity"],
                binding["revision"],
            ):
                return False
            history = db.execute(
                "SELECT d.*,j.model,j.status,j.max_assignments FROM frontier_decisions d JOIN jobs j ON j.id=d.job_id "
                "WHERE d.group_id=? ORDER BY d.id",
                (group_id,),
            ).fetchall()
            planning = sum(
                r["max_assignments"]
                for r in history
                if r["action"] in ("plan", "critique")
            )
            candidates, deferred = _frontier_candidates(
                db, group_id, root, history, planning, limits, binding
            )
            selected = _admit_candidate(
                self, db, group_id, group, loop, limits, history, candidates, deferred
            )
            if selected is None:
                reason = (
                    "no_useful_frontier"
                    if not candidates
                    or all(
                        d["reason"]
                        in (
                            "equivalent_strategy_exhausted",
                            "currently_checked",
                            "abandoned_advisory_suggestion",
                            "planning_critique_cap",
                        )
                        for d in deferred
                    )
                    else "capacity_or_model_budget"
                )
                reserve_reasons = [
                    d["reason"]
                    for d in deferred
                    if d["reason"].startswith("completion_")
                ]
                if reserve_reasons and reason == "capacity_or_model_budget":
                    reason = reserve_reasons[0]
                db.execute(
                    """INSERT OR IGNORE INTO frontier_decisions
                    (group_id,claim_id,action,strategy,graph_revision,reason,deferred,reservation,processed)
                    VALUES (?,?,?,?,?,?,?,0,1)""",
                    (
                        group_id,
                        root,
                        "stop",
                        reason,
                        graph["revision"],
                        reason,
                        encode(deferred),
                    ),
                )
                return stop(db, self, group_id, reason)
            return _dispatch_frontier_task(
                self,
                db,
                group_id,
                group,
                run,
                limits,
                history,
                planning,
                selected,
                candidates,
                deferred,
            )


def model_cost(db, group_id):
    rows = db.execute(
        """SELECT j.max_assignments,j.task_type,gj.cost FROM group_jobs gj
        JOIN jobs j ON j.id=gj.job_id WHERE gj.group_id=?""",
        (group_id,),
    ).fetchall()
    return {
        "reserved_work": sum(r["cost"] for r in rows),
        "reserved_assignments": sum(r["max_assignments"] for r in rows),
        "reserved_planning_critique_assignments": sum(
            r["max_assignments"] for r in rows if r["task_type"] in ("plan", "critique")
        ),
    }


def read_decisions(db, group_id):
    rows = [
        dict(r)
        for r in db.execute(
            """SELECT d.*,p.sha256 AS packet_sha256,
        length(p.packet) AS packet_bytes,p.manifest AS selected_manifest,p.budget AS packet_budget
        FROM frontier_decisions d LEFT JOIN context_packets p ON p.job_id=d.job_id
        WHERE d.group_id=? ORDER BY d.id""",
            (group_id,),
        )
    ]
    for row in rows:
        for key in ("deferred", "selected_manifest", "packet_budget"):
            row[key] = json.loads(row[key]) if row[key] is not None else None
    return rows
