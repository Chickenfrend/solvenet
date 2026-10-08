# Incremental Python type checking

Run `.venv/bin/mypy` from the repository root after installing
`requirements-dev.txt`. CI runs the same pinned checker and configuration,
targeting the project's Python 3.11 minimum.

## Initial strict scope

The explicit `files` list in `mypy.ini` covers:

- `contracts.py`: composed replay declarations, usage evidence, the portion of
  a final context packet read by prompt generation, and group/job field types.
- `composed.py`: proof source construction and composed verification results.
- `graph_instructions.py`: prompts built from typed final-packet inputs.
- `group_operations.py`: transaction-local persistence operations with explicit
  SQLite connections, job kinds, task types, serialized inputs and return types.
- `protocol_limits.py`: existing shared limit constants.

`strict = True` applies throughout this checked set. `follow_imports = silent`
lets mypy infer imported APIs without requiring an immediate whole-repository
annotation migration. Other modules, including most Store/API/model-result
ingestion and frontier policy, are **not yet covered by strict diagnostics**.
Untyped callers and SQLite's dynamic row contents still limit end-to-end
guarantees; annotations here are a foundation, not a claim that the complete
collaboration pipeline is statically verified.

## Expanding coverage

1. Choose one concrete boundary or module, such as validated graph proposals,
   frontier decisions or worker result parsing.
2. Add explicit records and parameter/return annotations, preserving the wire
   protocol and current lifecycle behavior. Prefer `TypedDict`, dataclasses,
   unions and literals over blanket `Any` or casts.
3. Add the module to `files`, resolve its strict diagnostics, and run relevant
   behavioral tests as well as Ruff and mypy. Do not weaken the checked set to
   make new code pass; any exceptional suppression needs a specific explanation.

`TypedDict` represents internal structure **after parsing**. It does not validate
external JSON or establish provenance. Existing runtime checks, budgets, verifier
identity binding and Lean checks remain authoritative. Composed usage receipts
are checked at runtime before being represented as typed dependency lists;
unknown usage remains explicit rather than being inferred as zero or no use.
These annotations do not change the documented local verifier trust boundary.
