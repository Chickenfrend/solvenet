# G6: graph-driven composed proof and local observation

Run from the repository root with Go and the pinned Lean 4.19.0 toolchain:

```sh
PATH="$HOME/.elan/bin:$PATH" PYTHONPATH=coordinator/src:homelab/src \
  python3 -m unittest discover -s integration -p test_graph_collaboration.py -v
```

## Deterministic process demonstration

`integration/fixtures/graph-nat-reorder.json` pins an `Init` natural-number
reorder problem. The planner publishes three branches and a B → A suggestion.
A proves reassociation independently. B uses A's exact generated declaration to
swap the inner sum; the target uses B to complete the outer swap. The generated
names/proof IDs come from the frozen **eligible checked context**, and the fake
provider refuses to construct B or the target without that exact predecessor.
Real Lean's elaborated-expression receipt confirms direct A-use by B and direct
B-use/transitive A-use by the target. These lemmas are useful to this candidate,
not mathematically necessary for every proof of the theorem.

The investigator challenges its own incoming target → A suggestion using the
relationship ID received in its packet. The independently dispatched critic
requires that exact relationship and the challenge's reason in its frozen packet
before marking the suggestion promising. Neither review target is taken from
the test harness's graph receipts. Two deliberately failed bounded target attempts
then open B's frontier. In the paired negative database, Lean rejects A instead
of receiving a planning challenge: the next critique is selected for negative
formal evidence. Selection reasons and deferred alternatives are asserted,
rather than assigning tasks directly in the test.

The coordinator is real localhost HTTP; the Go worker is compiled once. Each
queued job is handled by a separate `-once` worker process with one of two stable
worker identities; A's worker and B's later worker are distinct. The test manually
ticks the real coordinator between processes, removing scheduling races without
bypassing job/lease/provider/result HTTP. It asserts publication, artifact and
checked-packet source/agent/task/job/assignment provenance, exact packet bytes,
hashes, complete messages and worker context admission. A coordinator Store
restart before B dispatch and after target completion preserves frozen packets.
No reference target proof is submitted or included in the model input.

Success-path cost: seven requests/leases, zero worker retries/failures,
161 input/77 output tokens and 7,000,000 provider nanoseconds **supplied by the
fake API**, five logical composed Lean checks (two auxiliary, two failed targets,
one successful target), ten Lean subprocesses, known elapsed time and explicit
zero unknown counts. Exported JSON replays after restart without reading graph
state; removing its declarations rejects the exact target candidate. Those two
external validation operations add four Lean subprocesses, with separately
measured elapsed time, to the group's five operations/ten subprocesses. The
paired negative path adds one separately accounted auxiliary rejection. Each
worker has a 15-second process bound and kill/drain cleanup, sockets close within
five seconds, and the graph retains its model, packet, source, verification and
180-second Lean ceilings. Allow roughly one minute including the paired case.

## Compact operator trace and immutable export

The existing operator tool prints graph selections/deferred counts, packet
hashes/sizes, supplied names, planning relationships, actual proof-use receipts,
worker IDs, calls and separate model/Lean costs:

```sh
python3 integration/group_trace.py <group_id> --coordinator http://127.0.0.1:8080
curl http://127.0.0.1:8080/v1/proofs/<proof_id>/bundle > replay.json
```

`GET /v1/proofs/<proof_id>/bundle` exports immutable composed replay inputs;
`/evidence` exposes verification attempts and actual-use receipts. Both accept
only actual artifact or group target-attempt IDs with a committed verification
receipt (including a rejected proof). Queued planner/target job IDs, unknown IDs
and fresh pending proofs return 404. A pending recheck retains inspection of its
previous committed receipt. Internal frozen job-context export remains separate.
`GET /v1/groups/<group_id>/graph` returns the existing bounded planning
neighborhood (16 nodes, 32 items per category, 64 KiB encoded data), publication
sources, reviews and omission counts. This inspection does not grant formal
reuse eligibility; the frozen checked packet and proof receipts carry that.

Replay with the pinned verifier, recording its extra operation separately from
coordinator reservations:

```python
import json
from pathlib import Path
from solvenet.verifier import LeanVerifier
bundle = json.loads(Path("replay.json").read_text())
result, usage = LeanVerifier(Path("lean")).verify_composed(bundle)
print(result.status, result.elapsed_ms, usage)
```

## Explicit capped local-model protocol

`integration/live_graph_trial.py` is opt-in and uses only `127.0.0.1:11434`.
It first reads `/api/tags` and refuses an uninstalled model; it neither downloads
weights nor reads hosted credentials. Build a worker, then select an **already
installed** tag from the health/list response:

```sh
curl --max-time 3 http://127.0.0.1:11434/api/tags
(cd worker && go build -o /tmp/opencode/solvenet-g6-worker ./cmd/solvenet-worker)
PATH="$HOME/.elan/bin:$PATH" PYTHONPATH=coordinator/src:homelab/src \
  python3 integration/live_graph_trial.py --model qwen2.5-coder:7b \
  --worker /tmp/opencode/solvenet-g6-worker --output /tmp/opencode/g6-observation-new.json
```

The trial uses a new temporary SQLite database and the tiny conjunction target
`: True ∧ (True ∧ True)` to assess protocol compliance. All roles share the chosen
local model, context 8192, planning output allowance 512 and target allowance 2048.
Caps: 12 weighted work units at configured reservation cost 3, at most two jobs
and three assignment reservations, one planning assignment, zero independent
retries, two verification operations, 20,000 cumulative Lean milliseconds and
10 seconds per verifier call. Group deadline is 75 seconds; operator cutoff is
80 seconds, followed by worker terminate/kill waits of at most five seconds each
and scheduler drain of at most 15 seconds. These are observation caps, not a
model-strength ranking. The output is a new local JSON file with model digest,
snapshot, calls, costs and target diagnostics. Unknown token/time counts and an
operator cutoff remain explicit. Cleanup removes only the temporary trial DB.
The output also includes `execution_metadata`: collection timestamp, CPU model
and logical CPU count, total RAM, NVIDIA GPU names/total VRAM/driver when
`nvidia-smi` is available, Ollama version from read-only `/api/version`, and
applicable generation settings. Inventory probes have bounded reads and
three-second command/HTTP deadlines. Missing metadata has explicit null values
and unavailability reasons. Hardware inventory does not imply per-call CPU/GPU
placement. Temperature, seed, top-p/top-k, GPU offload and thread count are not
sent by this trial; effective model/server defaults remain explicitly unavailable.

## Actual local observation, 2026-10-02

Read-only Ollama listing found `qwen2.5-coder:7b` (7.6B Q4_K_M) and
`goedel-prover-v2-8b:q4_k_m` (8.2B Q4_K_M). One capped trial with the former ran
through the real HTTP coordinator, compiled Go worker and pinned Lean, using the
protocol above. Observed digest:
`dae161e27b0e90dd1856c8bb3209201fd6736d8eb66298e75ed87571486f4364`.

| Observation | Result |
| --- | --- |
| Path | initial plan → direct target fallback → `no_useful_frontier` |
| Plan | `(True ∧ True) ∧ True`; no structured graph claims published |
| Target | invalid `funext` / `and.intro True` term; Lean rejected |
| Model calls | 2 completed, 0 provider failures/retries |
| Tokens | 1537 input, 55 output, unknown counts 0 |
| Provider time | 11,548,365,809 ns (includes reported load time) |
| Model reservations | 12 work units, 3 assignments, 1 planning assignment |
| Lean | 1 logical operation, 2 subprocesses, 1477 ms; unknown counts 0 |
| Packet sizes | 1005-byte plan, 1364-byte synthesis |
| Checked lemmas / actual graph reuse | none |

Read-only metadata collected later during G6 review on the same date (without
rerunning generation) found:

| Metadata | Read-only observation |
| --- | --- |
| CPU | AMD Ryzen 5 2600 Six-Core Processor; 12 logical CPUs |
| Total RAM | 33,566,236,672 bytes from `/proc/meminfo` |
| GPU / total VRAM | unavailable: `nvidia-smi` not found on PATH; inventory not collected |
| Ollama version | `/api/version` reports `0.34.2` at review time |
| Platform | Linux 7.2.6-arch2-1, x86_64, glibc 2.44 |
| Generation options sent in the trial | `num_ctx=8192`; `num_predict=512` plan / `2048` proof |
| Temperature, seed, top-p/top-k, offload, threads | not explicitly sent; effective defaults unavailable |
| Actual trial CPU/GPU placement / peak RAM or VRAM | unavailable; not recorded during the original trial |

These later inventory readings do not establish historical execution placement,
resource utilization or the exact Ollama version at the earlier trial's execution.
Future script outputs capture the inventory timestamp before generation; original
call timings, tokens and outcomes above are unchanged.

This is a protocol-compliance failure on a trivial target, not an efficiency
comparison or a frontier result. The initial planner's unstructured output was
retained as informal evidence and the bounded direct fallback remained available.
A useful next policy/prompt experiment is a compact schema example plus an
explicit “no decomposition” response, measuring ingestion acceptance separately
from theorem success before increasing planning budgets. No G5 scheduling change
is justified by this single observation. The scripted arithmetic fixture validates
composition and coordination mechanics; it does not establish real-model gains.
