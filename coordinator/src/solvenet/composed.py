"""Bounded immutable replay inputs and elaborated-expression use receipts (G3)."""

import hashlib
import json
import tempfile
import time
from pathlib import Path

from .verifier import VerificationStatus

MAX_DECLARATIONS = 8
MAX_DEPTH = 8
MAX_SOURCE_BYTES = 32 * 1024
SCHEMA = "solvenet.composed.v1"


def declaration_name(proof_id):
    return "SolveNetLemma_" + hashlib.sha256(proof_id.encode()).hexdigest()[:24]


def encode(bundle):
    return json.dumps(bundle, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def validate_bundle(bundle):
    if bundle["schema"] != SCHEMA or not bundle["verifier_identity"]:
        raise ValueError("Invalid composed verifier binding")
    declarations = bundle["declarations"]
    if len(declarations) > MAX_DECLARATIONS:
        raise ValueError("Composed declaration limit exceeded")
    seen, depths, names = set(), {}, set()
    for item in declarations:
        if item["name"] != declaration_name(item["proof_id"]):
            raise ValueError("Renamed composed declaration")
        if item["proof_id"] in seen or item["name"] in names:
            raise ValueError("Duplicate composed declaration")
        dependencies = item["prerequisite_ids"]
        if len(set(dependencies)) != len(dependencies) or not set(dependencies) <= seen:
            raise ValueError("Cyclic or unordered composed prerequisites")
        depth = 1 + max((depths[p] for p in dependencies), default=0)
        if depth > MAX_DEPTH:
            raise ValueError("Composed depth limit exceeded")
        seen.add(item["proof_id"])
        names.add(item["name"])
        depths[item["proof_id"]] = depth
    selection = bundle["selected_proof_ids"]
    if len(set(selection)) != len(selection) or not set(selection) <= seen:
        raise ValueError("Invalid selected proof IDs")
    if len(encode(bundle).encode()) > min(
        MAX_SOURCE_BYTES, bundle.get("source_byte_limit", MAX_SOURCE_BYTES)
    ):
        raise ValueError("Composed aggregate source limit exceeded")


def build_source(verifier, bundle, receipt):
    """Reuse the standalone parser/exact-type/universe/axiom checks for every theorem."""
    items = bundle["declarations"] + [
        {
            "name": "SolveNetCandidate",
            "statement": bundle["statement"],
            "proof": bundle["proof"],
        }
    ]
    source = ""
    for index, item in enumerate(items):
        problem = verifier._validate_input(
            item["statement"], item["proof"], bundle["imports"]
        )
        if problem:
            raise ValueError(problem)
        fragment = verifier._build_source(
            item["statement"],
            item["proof"],
            bundle["imports"],
            expected_name=f"SolveNetExpected_{index}",
            candidate_name=item["name"],
            include_prelude=index == 0,
        )
        source += fragment + "\n"
    names = ", ".join("`" + item["name"] for item in bundle["declarations"])
    source += f"""
open Lean Elab Command in
run_cmd do
  let actual ← getConstInfo `SolveNetCandidate
  let supplied : Array Name := #[{names}]
  let direct := actual.value!.getUsedConstants
  let types := actual.type.getUsedConstants
  let rec visit (fuel : Nat) (todo : List Name) (seen : NameSet) : CoreM NameSet := do
    match todo with
    | [] => return seen
    | name :: rest =>
      if seen.contains name then visit fuel rest seen
      else
        match fuel with
        | 0 => throwError "Use inspection exceeded bound"
        | fuel + 1 =>
          let info ← getConstInfo name
          let refs := info.getUsedConstantsAsSet.toList
          visit fuel (refs ++ rest) (seen.insert name)
  let closure ← liftCoreM <| visit 100000 (direct.toList ++ types.toList) {{}}
  let strings := fun (ns : Array Name) => Json.arr (ns.map fun n => Json.str n.toString)
  let result := Json.mkObj [
    ("status", Json.str "known"),
    ("direct", strings (supplied.filter direct.contains)),
    ("type", strings (supplied.filter types.contains)),
    ("transitive", strings (supplied.filter closure.contains))]
  liftIO <| IO.FS.writeFile {json.dumps(str(receipt))} result.compress
"""
    return source


def verify_composed(verifier, bundle):
    started = time.monotonic()
    usage = {"status": "usage_unknown", "subprocesses": 0}
    try:
        validate_bundle(bundle)
        with tempfile.TemporaryDirectory(prefix="solvenet-composed-") as directory:
            receipt = Path(directory) / "receipt.json"
            source = Path(directory) / "Candidate.lean"
            text = build_source(verifier, bundle, receipt)
            if len(text.encode()) > min(
                MAX_SOURCE_BYTES, bundle.get("source_byte_limit", MAX_SOURCE_BYTES)
            ):
                raise ValueError("Generated composed source limit exceeded")
            source.write_text(text, encoding="utf-8")
            # Diagnose missing toolchains/imports as infrastructure failures.
            # Statement checks themselves run in order with the supplied declarations,
            # because their types may refer to earlier generated Lean names.
            preflight = Path(directory) / "Preflight.lean"
            preflight.write_text(
                verifier._build_source(": True", None, bundle["imports"]),
                encoding="utf-8",
            )
            status, diagnostics = verifier._run(preflight, started)
            usage["subprocesses"] += 1
            if status is not VerificationStatus.VERIFIED:
                return verifier._result(
                    VerificationStatus.TIMEOUT
                    if status is VerificationStatus.TIMEOUT
                    else VerificationStatus.VERIFIER_ERROR,
                    "Composed environment preflight failed: " + diagnostics,
                    started,
                ), usage
            status, diagnostics = verifier._run(source, started)
            usage["subprocesses"] += 1
            if status == VerificationStatus.VERIFIED:
                if not receipt.is_file() or receipt.stat().st_size > 8192:
                    status = VerificationStatus.VERIFIER_ERROR
                    diagnostics = (
                        "Composed checks did not produce a bounded completion receipt"
                    )
                else:
                    usage = json.loads(receipt.read_text()) | {
                        "subprocesses": usage["subprocesses"]
                    }
                    names = {item["name"] for item in bundle["declarations"]}
                    if usage.get("status") != "known" or any(
                        not isinstance(usage.get(k), list) or not set(usage[k]) <= names
                        for k in ("direct", "type", "transitive")
                    ):
                        raise ValueError("Invalid composed use receipt")
    except (OSError, ValueError, KeyError, TypeError) as error:
        status, diagnostics = VerificationStatus.REJECTED, str(error)
    return verifier._result(status, diagnostics, started), usage
