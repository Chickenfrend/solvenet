"""Coordinator-owned examples; no provider-specific prompt or reference proofs."""

from .composed import encode
from .contracts import ClaimFields, GraphPromptPacket, TextEnvelope
from .graph_response import SCHEMA


def graph_example(packet: GraphPromptPacket, task_type: str) -> TextEnvelope:
    """An accepted proposal shape instantiated solely from the received packet.

    The proof slot is intentionally a placeholder, not a suggested proof. A
    parser-accepted artifact is still pending and must pass the Lean checker.
    """
    focus = packet["focus"]
    context: ClaimFields = {
        "statement": focus["statement"],
        "imports": focus["imports"],
        "environment": focus["environment"],
    }
    batch: dict[str, object] = {"graph_schema": SCHEMA}
    if task_type == "plan":
        batch["claims"] = [
            dict(
                key="c",
                **context,
            )
        ]
        batch["findings"] = [
            {"key": "f", "claim": "$c", "text": "No useful decomposition yet."}
        ]
    elif task_type == "finding":
        batch["artifacts"] = [
            dict(
                key="p",
                claim=focus["id"],
                **context,
                proof="YOUR_LEAN_PROOF_BODY",
                prerequisite_proof_ids=[],
            )
        ]
    elif task_type == "critique":
        edges = packet["untrusted"]["relationships"]
        if edges:
            batch["reviews"] = [
                {
                    "key": "r",
                    "relationship": edges[0]["id"],
                    "status": "promising",
                    "reason": "Explain usefulness or objection.",
                }
            ]
        else:
            batch["findings"] = [
                {
                    "key": "f",
                    "claim": focus["id"],
                    "text": "No received relationship to review.",
                }
            ]
    else:
        raise ValueError("Invalid graph instruction task type")
    return {"text": encode(batch)}


def graph_instructions(packet: GraphPromptPacket, task_type: str) -> str:
    return (
        f'GRAPH_RESPONSE ({task_type}): Return outer JSON {{"text":string}}; text is serialized '
        "graph JSON, not an object, prose or Markdown. Shape example (not a mathematical solution):\n"
        + encode(graph_example(packet, task_type))
        + "\n"
        "No useful decomposition is valid: "
        + encode({"text": encode({"graph_schema": SCHEMA})})
        + "\n"
        "Omit unused arrays. Each item needs a unique key (1-32 letters/digits/_/-). "
        "A $key references a claim/relationship declared in this response only. "
        "Claims use exact Lean statements, imports and environment; these are immutable. "
        "The plan example republishes focus, not a new lemma; propose useful auxiliary claims yourself. "
        'Relationships: {key,from,to,kind:"suggests_using",reason}; distinct claim endpoints, '
        "received IDs or local $keys. Suggestions are not checked dependencies. "
        "Reviews use relationship IDs ONLY from this received packet; status is promising/challenged/abandoned. "
        "Findings: {key,claim,text}. Artifacts only in finding jobs: replace YOUR_LEAN_PROOF_BODY, "
        "copy the claim context exactly; prerequisite_proof_ids lists used checked_lemmas proof_id values. "
        "Use their exact name declarations in Lean, never invented names. "
        "Each artifact proof must be a Lean 4 tactic body, inserted verbatim (with indentation) "
        "beneath the verifier's `theorem <name> <statement> := by` wrapper. "
        "Do not include a leading by, a theorem/example/def declaration, Markdown fences, or explanation. "
        "Do not use sorry or admit. Candidate strings are preserved exactly; malformed wrappers "
        "are not stripped or automatically repaired. "
        'Format-only example for an unrelated claim: {"statement":": True","proof":"exact True.intro"} '
        "produces `theorem <name> : True := by\\n  exact True.intro`. "
        "This illustrates syntax only, not a proof of the received claim. "
        "Do not assert verified status: only the coordinator Lean check establishes proof facts."
    )
