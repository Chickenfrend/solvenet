# Core fixture set

`core-v1.json` is the immutable first set: ID `core`, version `1`, Lean
`leanprover/lean4:v4.19.0` with the `Init` import. Changing its contents should
create a new version, preserving IDs for unchanged theorems. Problems include
stable IDs, human titles, Lean statement/imports, category, and a validation-only
reference proof. The proof field is never included in run submission or worker
prompts. The JSON file and `lean/lean-toolchain` identify the pinned environment.

From the repository root, with Lean's `lake` on PATH, verify every reference
proof using the same verifier used by the coordinator:

```sh
PYTHONPATH=coordinator/src python3 -m unittest discover \
  -s coordinator/tests -p test_problem_set.py -v
```

The structural loader rejects duplicate IDs, empty or multi-line statements,
invalid import module names, and missing fields. Lean preflight and reference
proof verification catch statements and imports that are syntactically valid
strings but do not elaborate in the pinned project. Submission requires a running
coordinator; `python3 -m solvenet.problem_set --help` lists options. Save its
JSON manifest (`--output`) with the subsequent run results: the current API does
not persist fixture/version metadata in the run record itself. The manifest
includes a SHA-256 of the exact fixture JSON bytes for later comparison.
