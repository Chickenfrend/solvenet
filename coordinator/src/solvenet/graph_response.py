"""G2: bounded untrusted JSON proposals, authorized only by persisted completion."""

import json
import re

from .claim_graph import _source, attach_evidence, insert_claim
from .group_artifacts import insert_artifact, validate_artifact
from .group_state import _conflict, _require, _require_group, _text

SCHEMA = "solvenet.graph.v1"
MAX_BATCH_BYTES = 8192
MAX_BATCH_ITEMS = 16
ARRAY_LIMITS = {
    "claims": 8,
    "relationships": 8,
    "reviews": 8,
    "findings": 8,
    "artifacts": 4,
    "priorities": 4,
    "help_requests": 4,
}

MIGRATION_20 = """
ALTER TABLE claim_relationship_reviews ADD COLUMN source TEXT NOT NULL DEFAULT 'coordinator';
ALTER TABLE claim_relationship_reviews ADD COLUMN task_id TEXT REFERENCES group_tasks(id);
ALTER TABLE claim_relationship_reviews ADD COLUMN job_id TEXT REFERENCES jobs(id);
ALTER TABLE claim_relationship_reviews ADD COLUMN assignment_id TEXT REFERENCES assignments(id);
ALTER TABLE group_artifacts ADD COLUMN assignment_id TEXT REFERENCES assignments(id);
ALTER TABLE group_messages ADD COLUMN assignment_id TEXT REFERENCES assignments(id);
CREATE TABLE graph_response_receipts (
 job_id TEXT PRIMARY KEY REFERENCES jobs(id), group_id TEXT NOT NULL REFERENCES agent_groups(id),
 assignment_id TEXT NOT NULL REFERENCES assignments(id),
 status TEXT NOT NULL CHECK(status IN ('accepted','rejected')), result TEXT NOT NULL);
CREATE INDEX graph_receipts_group ON graph_response_receipts(group_id);
PRAGMA user_version = 20;
"""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def graph_batch(text):
    """No fence extraction or heuristic promotion of ordinary model prose."""
    try:
        value = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(value, dict) or "graph_schema" not in value:
        return None
    if len(text.encode()) > MAX_BATCH_BYTES:
        raise ValueError("Graph batch exceeds encoded byte limit")
    value = json.loads(text, object_pairs_hook=_unique_object)
    if value["graph_schema"] != SCHEMA:
        raise ValueError("Unsupported graph schema")
    count = 0
    keys = set()
    for name, maximum in ARRAY_LIMITS.items():
        items = value.get(name, [])
        if not isinstance(items, list) or len(items) > maximum:
            raise ValueError("Invalid graph array: " + name)
        for item in items:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("key"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", item["key"])
            ):
                raise ValueError("Invalid job-local proposal key")
            if item["key"] in keys:
                raise ValueError("Duplicate proposal key")
            keys.add(item["key"])
        count += len(items)
    if count > MAX_BATCH_ITEMS:
        raise ValueError("Too many graph proposals")
    return value


def completed_source(db, group_id, job_id):
    row = db.execute(
        """SELECT gj.*, j.kind,j.task_type,j.status,a.id AS assignment_id,a.result
        FROM group_jobs gj JOIN jobs j ON j.id=gj.job_id
        JOIN assignments a ON a.job_id=j.id AND a.status='completed'
        WHERE gj.group_id=? AND gj.job_id=? ORDER BY a.rowid DESC LIMIT 1""",
        (group_id, job_id),
    ).fetchone()
    if (
        row is None
        or row["status"] != "done"
        or row["kind"] != "model.respond"
        or row["task_type"] not in ("plan", "finding", "critique")
    ):
        _conflict("Graph response needs a completed plan, finding or critique")
    result = json.loads(row["result"])
    if result.get("status") != "completed":
        _conflict("Failed assignment is not a graph source")
    _source(db, group_id, row["agent_id"], row["task_id"], job_id)
    return row, result["output"]["text"]


class GraphResponses:
    def ingest_group_graph_response(self, group_id, job_id):
        """Read the actual completed response; return a durable acceptance/rejection receipt.

        None means ordinary text. Never accepts caller-supplied text or identity.
        Safe to call after restart or repeat delivery.
        """
        with self.transaction() as db:
            return self._ingest_group_graph_response(db, group_id, job_id)

    def _ingest_group_graph_response(self, db, group_id, job_id):
        from .store import Conflict

        row, text = completed_source(db, group_id, job_id)
        old = db.execute(
            "SELECT result FROM graph_response_receipts WHERE job_id=?", (job_id,)
        ).fetchone()
        if old:
            return json.loads(old["result"])
        db.execute("SAVEPOINT graph_batch")
        try:
            batch = graph_batch(text)
            if batch is None:
                db.execute("RELEASE graph_batch")
                return None
            result = self._apply_graph_batch(db, group_id, row, batch)
        except (ValueError, Conflict, KeyError, TypeError, RecursionError) as error:
            db.execute("ROLLBACK TO graph_batch")
            result = {"status": "rejected", "error": str(error)[:512]}
        db.execute("RELEASE graph_batch")
        db.execute(
            "INSERT INTO graph_response_receipts VALUES (?,?,?,?,?)",
            (
                job_id,
                group_id,
                row["assignment_id"],
                result["status"],
                json.dumps(result),
            ),
        )
        return result

    def _apply_graph_batch(self, db, group_id, source, batch):  # noqa: C901 -- atomic typed proposal ingestion
        from .store import identifier

        group = _require_group(db, group_id)
        job_id, agent_id, task_id = (
            source["job_id"],
            source["agent_id"],
            source["task_id"],
        )
        provenance = {"agent_id": agent_id, "task_id": task_id, "job_id": job_id}
        local_claims, local_edges = {}, {}
        result = {
            "status": "accepted",
            "claims": {},
            "publications": {},
            "relationships": {},
            "reviews": {},
            "findings": {},
            "artifacts": {},
        }

        def key(item):
            return "graph:" + job_id + ":" + item["key"]

        def ref(value, table, local):
            if not isinstance(value, str):
                raise ValueError("Graph reference must be a string")
            if value.startswith("$"):
                if value[1:] not in local:
                    raise ValueError("Unknown job-local reference")
                return local[value[1:]]
            _require(db, table, group_id, value)
            return value

        for item in batch.get("claims", []):
            claim, publication = insert_claim(
                db,
                group_id,
                key(item),
                item["statement"],
                item["imports"],
                item["environment"],
                reason=item.get("reason", ""),
                **provenance,
            )
            local_claims[item["key"]] = claim
            result["claims"][item["key"]] = claim
            result["publications"][item["key"]] = publication
        for item in batch.get("relationships", []):
            edge = self.propose_group_relationship(
                group_id,
                key(item),
                ref(item["from"], "group_claims", local_claims),
                ref(item["to"], "group_claims", local_claims),
                item["kind"],
                reason=item.get("reason", ""),
                _db=db,
                **provenance,
            )
            local_edges[item["key"]] = edge
            result["relationships"][item["key"]] = edge
        for item in batch.get("reviews", []):
            packet = db.execute(
                "SELECT packet FROM context_packets WHERE job_id=?", (job_id,)
            ).fetchone()
            if packet is not None and item["relationship"] not in {
                edge["id"]
                for edge in json.loads(packet["packet"])["untrusted"]["relationships"]
            }:
                raise ValueError(
                    "Review relationship must be from the received context packet"
                )
            result["reviews"][item["key"]] = self.review_group_relationship(
                group_id,
                key(item),
                ref(item["relationship"], "claim_relationships", local_edges),
                agent_id,
                item["status"],
                item.get("reason", ""),
                task_id=task_id,
                job_id=job_id,
                _db=db,
            )
        for item in batch.get("findings", []):
            claim = ref(item["claim"], "group_claims", local_claims)
            _text(item["text"], "graph finding", 2048)
            if (
                db.execute(
                    "SELECT count(*) FROM group_messages WHERE group_id=?", (group_id,)
                ).fetchone()[0]
                >= group["max_messages"]
            ):
                _conflict("Message limit reached")
            message = identifier()
            db.execute(
                """INSERT INTO group_messages
                (id,group_id,request_key,agent_id,task_id,job_id,kind,text,assignment_id)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    message,
                    group_id,
                    key(item),
                    agent_id,
                    task_id,
                    job_id,
                    "critique" if source["task_type"] == "critique" else "finding",
                    item["text"],
                    source["assignment_id"],
                ),
            )
            attach_evidence(db, group_id, claim, "message", message)
            result["findings"][item["key"]] = message
        for item in batch.get("artifacts", []):
            if source["task_type"] != "finding":
                raise ValueError("Formal proposals require a finding job")
            claim = ref(item["claim"], "group_claims", local_claims)
            validate_artifact(
                *(item[k] for k in ("statement", "imports", "environment", "proof"))
            )
            target = _require(db, "group_claims", group_id, claim)
            if (
                item["statement"],
                json.dumps(item["imports"]),
                item["environment"],
            ) != (target["statement"], target["imports"], target["environment"]):
                _conflict("Artifact does not prove the exact claim context")
            artifact = insert_artifact(
                db,
                group_id,
                key(item),
                agent_id,
                task_id,
                item["statement"],
                item["imports"],
                item["environment"],
                item["proof"],
                job_id,
                graph_proposal_key=item["key"],
            )
            attach_evidence(db, group_id, claim, "artifact", artifact)
            if "prerequisite_proof_ids" in item:
                from .proof_context import freeze_context, selected_bundle

                freeze_context(
                    db,
                    artifact,
                    "artifact",
                    selected_bundle(
                        db,
                        group_id,
                        claim,
                        item["proof"],
                        item["prerequisite_proof_ids"],
                    ),
                )
            result["artifacts"][item["key"]] = artifact
        for name in ("priorities", "help_requests"):
            result[name] = {}
            for item in batch.get(name, []):
                claim = ref(item["claim"], "group_claims", local_claims)
                priority = item.get("priority", 0)
                if type(priority) is not int or not 0 <= priority <= 3:
                    raise ValueError("Priority must be between 0 and 3")
                action = item.get(
                    "action", "investigate" if name == "priorities" else "critique"
                )
                if action not in ("investigate", "critique", "prove"):
                    raise ValueError("Invalid proposed frontier action")
                reason = item.get("reason", "")
                if not isinstance(reason, str) or len(reason.encode()) > 512:
                    raise ValueError("Invalid frontier proposal reason")
                proposal_id = key(item)
                db.execute(
                    "INSERT INTO graph_action_proposals VALUES (?,?,?,?,?,?,?)",
                    (proposal_id, group_id, job_id, claim, action, priority, reason),
                )
                db.execute(
                    "UPDATE group_graphs SET revision=revision+1 WHERE group_id=?",
                    (group_id,),
                )
                result[name][item["key"]] = proposal_id
        return result
