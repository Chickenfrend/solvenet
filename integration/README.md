# Process-level integration: IT1, A6 and G6

## Opt-in OpenAI graph observation (O4)

`live_openai_graph_trial.py` defaults to a **no-call preflight**. Supply an exact
model ID, the tested `responses-reasoning` profile, a compiled worker, a
worker-local key-file path, current operator-confirmed rates, a total allowance
and any prior-call reservation. Only adding `--execute-paid` starts requests.
The Python runner never opens the key file; it passes its path only in the
worker environment. It performs no compatibility probe.

Example preflight (prices are illustrative; confirm the selected model's current
prices at <https://developers.openai.com/api/docs/pricing> before execution):

```sh
PATH="$HOME/.elan/bin:$PATH" PYTHONPATH=coordinator/src:homelab/src \
  python3 integration/live_openai_graph_trial.py \
  --model gpt-6.1-sol --profile responses-reasoning \
  --worker /tmp/opencode/solvenet-openai-trial-worker \
  --key-file "$HOME/.config/solvenet/openai_api_key" \
  --output /tmp/opencode/solvenet-o4-openai-observation.json \
  --total-budget-usd 1 --prior-reserved-usd 0.08 \
  --input-usd-per-million 2.50 --output-usd-per-million 10
```

With these inputs the conservative total projection is **$0.89920**, including
the $0.08 reservation for an earlier probe whose usage is unknown. The graph
reserves 20 work units at model cost 2: at most five jobs, each with at most two
assignments, hence at most ten attempted calls including retries. Every attempt
reserves 24,576 input tokens (full request bytes plus framing bounded by the
worker) and 2,048 output tokens including reasoning. Existing graph jobs request
512 tokens for plan/finding/critique and 2,048 for target proofs; the projection
uses 2,048 for every attempt. It refuses a projection above the total allowance.

The claim gate independently caps assignments and pauses on expired assignments
or missing input/output usage, including failures. A structured planning reply
is required before further graph expansion. It uses the multi-step natural-number
reordering target from `fixtures/graph-nat-reorder.json`, supplying only its
statement/imports/environment, never fixture proofs. All roles use one model
but retain distinct agent IDs. No hierarchy comparison is claimed.

Ceilings: two planning/critique assignments, no independent frontier retries,
four real Lean operations / 40 seconds Lean allowance, 10 seconds per Lean
check, 45 seconds per provider call, 285-second group deadline and 300-second
worker cutoff. At cutoff and cleanup, termination escalates to a kill after five
seconds if needed; the scheduler
is operator-driven and has no background thread. A running Lean check may take
up to its ten-second bound to return after the process cutoff.

The fresh JSON output and adjacent `.json.data` directory persist the coordinator
SQLite database, frozen prompts/packet hashes, graph acceptance/rejection receipts,
checked artifacts and use evidence, target outcomes, assignment retries/failures,
worker log, and actual/unknown usage. Observed token-based dollar values are
estimates, not provider bills; unknown usage stays unknown. Both paths must be
new. A formatting failure, truncation or negative mathematical result stops this
observation without increasing its allowance. Inspect structured collaboration,
checked lemma handoff/use, and target verification separately.

Offline cap/gate/cleanup regressions (no keys/API):

```sh
PYTHONPATH=coordinator/src:homelab/src python3 -m unittest discover \
  -s integration -p test_live_openai_graph_trial.py -v
```

O1 also runs the G6 graph scenario through fake loopback OpenAI `chat-json` and
`responses-reasoning` profiles using an arbitrary configured model ID. Both
profiles carry nested graph JSON and target proofs through compiled Go workers,
the real coordinator HTTP protocol and pinned Lean, including a rejected branch.
See [OpenAI profile contracts](../docs/openai-profiles.md). No paid calls or
existing key files are used.

The OpenAI recovery regressions keep one compiled daemon alive across a 429
retry or malformed/local-admission failure and a later unrelated compatible
run. Exact provider-call counts exclude startup and background paid probes.
Both profiles also carry Lean literal `"\n"` statements and proof bodies through
graph publication, artifact binding and pinned Lean, with nested Unicode-escaped
synthetic secrets redacted independently of formal content.

O3 adds coordinator-owned, provider-neutral `GRAPH_RESPONSE` instructions to
frozen graph tasks. Plan/finding/critique examples show the accepted graph JSON
serialized inside the outer `text` string, plus the valid empty graph batch.
Examples copy exact task contexts and select review IDs only from the final
received packet. Plan examples republish focus to demonstrate local `$key`
references; finding examples contain an explicit proof-body placeholder, never
a reference proof. Checked declarations must use their manifest names and
proof IDs exactly. Suggestions and review opinions confer no proof authority.
The complete instructions, examples and packet are included in conservative
G4 byte/token admission and routing. Packet-based graph reviews referring to
unreceived relationships receive a formatting/ID rejection receipt.

The G6 fake providers derive claim/artifact/review shapes from the actual shown
examples, supplying fixture mathematics only as model responses. The same
compiled workers and mocked real adapters still establish checked A → B → target
use with Lean. Coordinator parser tests submit the exact examples and empty
response through persisted completions, including malformed arrays/local IDs
and an edge published after dispatch. Worker contract tests reject an object in
the outer string slot. These are automated formatting and handoff checks, not
real-model effectiveness observations; no paid evaluation was performed.

The [G6 graph demonstration](../docs/graph-demonstration.md) adds a deterministic
graph-mode A → B → target composed proof through compiled independent Go workers,
paired evidence-driven assignments, a challenged/redirected branch, immutable
replay and compact operator trace. It also records an actual capped local Ollama
observation and provides an explicit opt-in reproduction command.

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

## A6: collaborating agents → two workers → pinned Lean

Run only the collaboration case with:

```sh
PATH="$HOME/.elan/bin:$PATH" PYTHONPATH=coordinator/src:homelab/src \
  python3 -m unittest discover -s integration -p test_group_collaboration.py -v
```

`fixtures/collaboration-nat-reorder.json` pins the target under `Init` in
`lean/lean-toolchain` (Lean 4.19.0): for natural numbers `a b c`, prove
`(a + b) + c = (c + b) + a`. The first investigator proposes reassociation as
an auxiliary formal lemma; a second explores swapping terms but misses the
parentheses. A critic accepts the first and redirects the second. The scripted
synthesizer returns a proof using reassociation and two commutations **only
after** the verified auxiliary statement appears in the verified-context block;
this checks delivery and gating, not causal discovery or an efficiency gain.
The lemma alone does not prove the target. Lean checks both claims independently.
The fixture's proof is used solely by the fake model server as a deterministic
response, never submitted as a reference proof in the target problem request.

The test creates one temporary coordinator SQLite database (no site is needed
for group inspection), an in-process localhost coordinator HTTP server and
scheduler, two fake localhost Ollama APIs, and **two compiled Go worker
processes**. Models `ollama/fixture-routine:latest` and
`ollama/fixture-specialist:latest` are scripted response identities, **not
downloaded weights**; both advertise availability. Routing config restricts
the routine model to findings at cost 1, and the specialist to planning,
findings, critique and proof at cost 2. The coordinator reserves 24 work units
and uses 20 across six jobs; the redirected branch is reassigned to the first
investigator, who switches from routine to specialist while retaining that
agent ID. The run waits up
to 45 seconds for terminal verification and cleans up subprocesses, threads,
sockets and its temporary DB. No Docker, Ollama daemon, GPU, hosted credentials,
site database or existing coordinator database is touched.

The assertions require five persistent agent IDs, six distinct linked jobs,
parentage and reviewer dispositions, the cross-model handoff, a verified
provenance-bound artifact, a verified target attempt and a `verified_target`
group reason. On the scripted success path the coordinator's `cost` reports
six requests/leases, zero retries/failures, 138 known input tokens, 66 known
output tokens, 6,000,000 known provider-duration nanoseconds, two real Lean
checks and their measured milliseconds (machine-dependent, unknown count 0).
The fake API *supplies* token/duration numbers to test accounting; they are
not real model consumption or a model-strength benchmark. Missing usage in
other runs remains explicitly unknown in `cost`; a queued request has no
reported provider usage. This case does not demonstrate success on a frontier
problem, autonomous decomposition or accuracy of local models.

The collaboration test module also runs a G2 variant through the unchanged Go
wire protocol: the planner publishes a claim/suggestion, the finding proposes an
exact-bound artifact on the same claim, and the critic challenges the suggestion.
Assertions require actual completed agent/task/assignment provenance, rejection
of forged authority, independent review/Lean status, and the same six-call
redirect/synthesis behavior and costs. It does not exercise G3 proof composition
or graph-driven scheduling.

For an operator-run group, `GET /v1/groups/<id>` contains agents, task parents,
job links, route explanations, bounded messages, artifact outcomes, calls and
cost. `GET /v1/runs/<run_id>` contains the final Lean diagnostics and candidate.
To print a compact graph and cost from **those existing endpoints**:

```sh
python3 integration/group_trace.py <group_id> --coordinator http://127.0.0.1:8080
```

For an optional exploratory run with actual local models, start a coordinator
on a **new temporary SQLite path** using `--verifier local --project lean` and
two Go workers using `-provider ollama -model <installed-name> -ollama-url
http://127.0.0.1:11434` (different `-id` values). Check installed names with
`ollama list`, and check both IDs with `/v1/model-activity?model=ollama%2F...`.
Submit `POST /v1/groups` with `request_key`, the fixture's `statement`,
`imports`, `environment`, `max_work: 24`, role `models` and
`model_capabilities` as in the test, replacing model IDs and capability/cost
estimates with the actual installed models. Keep `context_bytes` large enough
for the bounded group prompts and worker `-ollama-context` greater than 2048
tokens for synthesis. Inspect with `group_trace.py` and the run endpoint;
record configured IDs, Ollama `generation.model` / `model_digest` if reported,
route decisions, all calls and known/unknown usage, Lean checks, failures,
elapsed time and terminal reason. This is an operator procedure, not a claim
that a live-model trial was performed or that the scripted identities imply a
capability hierarchy for real weights.
