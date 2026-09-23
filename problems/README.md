# Versioned fixture sets

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
strings but do not elaborate in the pinned project. For ad hoc submission,
`python3 -m solvenet.problem_set --help` lists options and its `--output`
manifest includes a SHA-256 of the exact fixture JSON bytes. For durable,
idempotent full-set submissions, use the experiment API described in
`coordinator/README.md`.

`challenge-v1.json` is a separate, 12-problem set: ID `challenge`, version `1`,
in the same pinned Lean environment. It includes propositional and quantified
equivalences, natural-number arithmetic, list induction, and Boolean algebra.
It is intended for a more discriminating independent-vs-repair comparison than
the introductory `core` fixtures. Its IDs and exact JSON bytes are versioned
independently of `core`; make a new version rather than editing a published set.
The real-Lean reference-proof test requires Lake on PATH and checks both sets:

```sh
PATH="$HOME/.elan/bin:$PATH" PYTHONPATH=coordinator/src python3 -m unittest discover \
  -s coordinator/tests -p test_problem_set.py -v
```

The ad hoc submission CLI can also select `--set problems/challenge-v1.json`.
For persisted experiments select `challenge`/`1` and the SHA-256 of that file
as described in `coordinator/README.md`; the server accepts only its explicit
checked-in fixture allowlist, never a request-supplied file path.
