"""Internal typed boundaries; external JSON still requires runtime validation.

These describe in-memory values after parsing. They do not validate worker
output, authorize proof reuse, or replace the existing SQLite/Lean checks.
"""

from typing import Literal, NotRequired, TypedDict

JobKind = Literal["model.generate", "model.respond"]
TaskType = Literal["plan", "question", "finding", "critique", "task_proposal"]


class ClaimFields(TypedDict):
    statement: str
    imports: list[str]
    environment: str


class PromptFocus(ClaimFields):
    id: str


class RelationshipReference(TypedDict):
    id: str


class PromptUntrusted(TypedDict):
    relationships: list[RelationshipReference]


class GraphPromptPacket(TypedDict):
    """The portion of a final packet read by graph instruction generation."""

    focus: PromptFocus
    untrusted: PromptUntrusted


class TextEnvelope(TypedDict):
    text: str


class ProofDeclaration(TypedDict):
    claim_id: str
    proof_id: str
    name: str
    statement: str
    proof: str
    prerequisite_ids: list[str]


class ComposedBundle(ClaimFields):
    schema: str
    group_id: str
    claim_id: str
    proof: str
    declarations: list[ProofDeclaration]
    selected_proof_ids: list[str]
    verifier_identity: str
    verifier_revision: int
    source_byte_limit: NotRequired[int]


class ProofUsage(TypedDict):
    """Unknown usage is explicit; absent dependency lists do not mean no use."""

    status: Literal["known", "usage_unknown"]
    subprocesses: int
    direct: NotRequired[list[str]]
    type: NotRequired[list[str]]
    transitive: NotRequired[list[str]]
    elapsed_unknown: NotRequired[bool]


class GroupRunFields(TypedDict):
    """Database-facing fields, where imports are stored as serialized JSON."""

    id: str
    statement: str
    imports: str
    environment: str
