# IT1: site → coordinator → worker → Lean

From the repository root, with Python 3.11+, Go, and the pinned Lean 4.19.0
toolchain installed through elan (including `lake` on `PATH`):

```sh
PATH="$HOME/.elan/bin:$PATH" PYTHONPATH=coordinator/src:homelab/src python3 -m unittest discover -s integration -v
```

The explicit test fails during setup if Go or local Lean/Lake is unavailable.
It builds one temporary Go worker, uses distinct temporary site/coordinator
SQLite files and a fake localhost Ollama API, then submits two fixture runs
through the real site's CSRF-protected form. Lean checks a valid candidate and
rejects an invalid one. No Docker, Ollama installation, GPU, API key or existing
SolveNet database is needed. Allow roughly 10 seconds on a warmed machine,
plus initial Go build/toolchain setup time. A failure reports the run ID,
bounded run state and worker log.
