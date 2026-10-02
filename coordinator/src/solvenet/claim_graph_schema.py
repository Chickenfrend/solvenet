"""Additive claim graph migration. Work parentage and proof use are not edges."""

MIGRATION_19 = """
CREATE TABLE group_graphs (
 group_id TEXT PRIMARY KEY REFERENCES agent_groups(id), root_id TEXT,
 revision INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(group_id, root_id) REFERENCES group_claims(group_id, id));
CREATE TABLE group_claims (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES agent_groups(id),
 statement TEXT NOT NULL, imports TEXT NOT NULL, environment TEXT NOT NULL,
 UNIQUE(group_id,id), UNIQUE(group_id,statement,imports,environment));
CREATE TABLE claim_publications (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL, request_key TEXT NOT NULL,
 claim_id TEXT NOT NULL, source TEXT NOT NULL CHECK(source IN ('coordinator','job')),
 agent_id TEXT, task_id TEXT, job_id TEXT, assignment_id TEXT,
 reason TEXT NOT NULL, UNIQUE(group_id,request_key),
 FOREIGN KEY(group_id,claim_id) REFERENCES group_claims(group_id,id),
 FOREIGN KEY(group_id,agent_id) REFERENCES group_agents(group_id,id),
 FOREIGN KEY(group_id,task_id) REFERENCES group_tasks(group_id,id),
 FOREIGN KEY(job_id,group_id,task_id,agent_id) REFERENCES group_jobs(job_id,group_id,task_id,agent_id),
 FOREIGN KEY(assignment_id) REFERENCES assignments(id));
CREATE TABLE claim_relationships (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL, request_key TEXT NOT NULL,
 from_id TEXT NOT NULL, to_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('suggests_using','alternative_to','supersedes')),
 source TEXT NOT NULL CHECK(source IN ('coordinator','job')),
 agent_id TEXT, task_id TEXT, job_id TEXT, assignment_id TEXT, reason TEXT NOT NULL,
 CHECK(from_id != to_id), UNIQUE(group_id,request_key), UNIQUE(group_id,id),
 FOREIGN KEY(group_id,from_id) REFERENCES group_claims(group_id,id),
 FOREIGN KEY(group_id,to_id) REFERENCES group_claims(group_id,id),
 FOREIGN KEY(group_id,agent_id) REFERENCES group_agents(group_id,id),
 FOREIGN KEY(group_id,task_id) REFERENCES group_tasks(group_id,id),
 FOREIGN KEY(job_id,group_id,task_id,agent_id) REFERENCES group_jobs(job_id,group_id,task_id,agent_id),
 FOREIGN KEY(assignment_id) REFERENCES assignments(id));
CREATE TABLE claim_relationship_reviews (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL, request_key TEXT NOT NULL,
 relationship_id TEXT NOT NULL, reviewer_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('promising','challenged','abandoned')), reason TEXT NOT NULL,
 UNIQUE(group_id,request_key),
 FOREIGN KEY(group_id,relationship_id) REFERENCES claim_relationships(group_id,id),
 FOREIGN KEY(group_id,reviewer_id) REFERENCES group_agents(group_id,id));
CREATE TABLE claim_tasks (
 group_id TEXT NOT NULL, task_id TEXT NOT NULL, claim_id TEXT NOT NULL,
 action TEXT NOT NULL CHECK(action IN ('investigate','critique','prove','synthesize')),
 PRIMARY KEY(group_id,task_id),
 FOREIGN KEY(group_id,task_id) REFERENCES group_tasks(group_id,id),
 FOREIGN KEY(group_id,claim_id) REFERENCES group_claims(group_id,id));
CREATE TABLE claim_messages (
 group_id TEXT NOT NULL, message_id TEXT NOT NULL, claim_id TEXT NOT NULL,
 PRIMARY KEY(group_id,message_id,claim_id),
 FOREIGN KEY(group_id,message_id) REFERENCES group_messages(group_id,id),
 FOREIGN KEY(group_id,claim_id) REFERENCES group_claims(group_id,id));
CREATE TABLE claim_artifacts (
 group_id TEXT NOT NULL, artifact_id TEXT NOT NULL, claim_id TEXT NOT NULL,
 PRIMARY KEY(group_id,artifact_id),
 FOREIGN KEY(group_id,artifact_id) REFERENCES group_artifacts(group_id,id),
 FOREIGN KEY(group_id,claim_id) REFERENCES group_claims(group_id,id));
INSERT INTO group_graphs(group_id) SELECT id FROM agent_groups;
CREATE INDEX publications_claim ON claim_publications(group_id,claim_id);
CREATE INDEX relationships_from ON claim_relationships(group_id,from_id);
CREATE INDEX relationships_to ON claim_relationships(group_id,to_id);
CREATE INDEX reviews_relationship ON claim_relationship_reviews(group_id,relationship_id);
CREATE INDEX tasks_claim ON claim_tasks(group_id,claim_id);
CREATE INDEX messages_claim ON claim_messages(group_id,claim_id);
CREATE INDEX artifacts_claim ON claim_artifacts(group_id,claim_id);
CREATE TABLE group_artifact_outcomes (
 group_id TEXT NOT NULL REFERENCES agent_groups(id),
 artifact_id TEXT NOT NULL REFERENCES group_artifacts(id),
 status TEXT NOT NULL, diagnostics TEXT NOT NULL, verifier_identity TEXT,
 verifier_revision INTEGER);
CREATE INDEX outcomes_artifact ON group_artifact_outcomes(artifact_id);
CREATE INDEX outcomes_group ON group_artifact_outcomes(group_id);
CREATE TRIGGER outcomes_limit BEFORE INSERT ON group_artifact_outcomes
 WHEN (SELECT count(*) FROM group_artifact_outcomes WHERE group_id=NEW.group_id) >= 256
 BEGIN SELECT RAISE(ABORT,'Artifact outcome history limit reached'); END;
-- v18 stored terminal results but not the binding revision used for each check.
-- Preserve what is known before a later verifier rebind resets verified rows.
INSERT INTO group_artifact_outcomes
 SELECT group_id,id,status,diagnostics,verifier_identity,NULL
 FROM group_artifacts WHERE status != 'pending';
CREATE TRIGGER artifact_outcome AFTER UPDATE OF status ON group_artifacts
 WHEN NEW.status != 'pending' AND OLD.status != NEW.status
 BEGIN INSERT INTO group_artifact_outcomes
 SELECT NEW.group_id,NEW.id,NEW.status,NEW.diagnostics,NEW.verifier_identity,revision
 FROM artifact_verifier_binding WHERE id=1; END;
CREATE TRIGGER claims_immutable BEFORE UPDATE ON group_claims
 BEGIN SELECT RAISE(ABORT,'Claims are immutable'); END;
PRAGMA user_version = 19;
"""

# Triggers also cover writes performed inside the existing fixed loop.
for table in ('group_claims', 'claim_publications', 'claim_relationships',
              'claim_relationship_reviews', 'claim_tasks', 'claim_messages', 'claim_artifacts'):
    MIGRATION_19 += f"""
CREATE TRIGGER {table}_revision AFTER INSERT ON {table}
 BEGIN UPDATE group_graphs SET revision=revision+1 WHERE group_id=NEW.group_id; END;
"""

MIGRATION_19 += """
CREATE TRIGGER graph_message_review AFTER UPDATE OF review_status ON group_messages
 WHEN OLD.review_status != NEW.review_status
 BEGIN UPDATE group_graphs SET revision=revision+1 WHERE group_id=NEW.group_id
 AND EXISTS (SELECT 1 FROM claim_messages WHERE group_id=NEW.group_id AND message_id=NEW.id); END;
CREATE TRIGGER graph_artifact_status AFTER UPDATE OF status,verifier_identity ON group_artifacts
 WHEN OLD.status != NEW.status OR OLD.verifier_identity IS NOT NEW.verifier_identity
 BEGIN UPDATE group_graphs SET revision=revision+1 WHERE group_id=NEW.group_id
 AND EXISTS (SELECT 1 FROM claim_artifacts WHERE group_id=NEW.group_id AND artifact_id=NEW.id); END;
CREATE TRIGGER graph_task_status AFTER UPDATE OF status,remaining,budget,owner_id ON group_tasks
 WHEN OLD.status != NEW.status OR OLD.remaining != NEW.remaining
 OR OLD.budget != NEW.budget OR OLD.owner_id != NEW.owner_id
 BEGIN UPDATE group_graphs SET revision=revision+1 WHERE group_id=NEW.group_id
 AND EXISTS (SELECT 1 FROM claim_tasks WHERE group_id=NEW.group_id AND task_id=NEW.id); END;
CREATE TRIGGER graph_work_availability AFTER UPDATE OF remaining_work ON agent_groups
 WHEN OLD.remaining_work != NEW.remaining_work
 BEGIN UPDATE group_graphs SET revision=revision+1 WHERE group_id=NEW.id
 AND EXISTS (SELECT 1 FROM claim_tasks WHERE group_id=NEW.id); END;
CREATE TRIGGER graph_job_created AFTER INSERT ON group_jobs
 BEGIN UPDATE group_graphs SET revision=revision+1 WHERE group_id=NEW.group_id
 AND EXISTS (SELECT 1 FROM claim_tasks WHERE group_id=NEW.group_id AND task_id=NEW.task_id); END;
CREATE TRIGGER graph_job_status AFTER UPDATE OF status ON jobs
 WHEN OLD.status != NEW.status
 BEGIN UPDATE group_graphs SET revision=revision+1 WHERE group_id IN (
 SELECT gj.group_id FROM group_jobs gj JOIN claim_tasks ct
 ON ct.group_id=gj.group_id AND ct.task_id=gj.task_id WHERE gj.job_id=NEW.id); END;
CREATE TRIGGER graph_artifact_outcome AFTER INSERT ON group_artifact_outcomes
 BEGIN UPDATE group_graphs SET revision=revision+1 WHERE group_id=NEW.group_id
 AND EXISTS (SELECT 1 FROM claim_artifacts WHERE group_id=NEW.group_id AND artifact_id=NEW.artifact_id); END;
"""
