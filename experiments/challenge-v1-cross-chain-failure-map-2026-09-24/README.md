# Local runner implementation

The frozen design is in `PLAN.md`. This runner calls the pinned Go Ollama provider
directly (`worker/experiments/crosschain`), so its system prompt, streaming JSON
schema, and proof extraction come from the provider rather than a reimplementation.
The Python bridge invokes the coordinator's `ContainerVerifier` with the current
`solvenet-verifier:homelab` image, restricted Docker configuration and 10-second
Lean timeout. The fixture is decoded
into an explicit public-field-only Go struct; `reference_proof` is not loaded.

From the repository root, implementation checks (fake Ollama, fake verifier; no
live model requests):

```sh
(cd worker && go test ./...)
python3 -m unittest discover -s experiments/challenge-v1-cross-chain-failure-map-2026-09-24 -p 'test_*.py'
```

The tests cover the five prescribed approach examples, outcome priority,
allowlisted message template, shared prefix and conditional stopping, settings
and request budgets, alternating third-arm order, malformed JSON/HTTP failures,
no raw predecessor or reference-proof message, and retained partial state on
readiness failure. Both check commands passed with fake services on 2026-09-24.

Before any live experiment, publish these frozen source hashes along with the
test results (the runner stores and rechecks them during execution):

| File | SHA-256 |
| --- | --- |
| `problems/challenge-v1.json` | `cd199d3ccd299533a38b14ead2d768b7edf47565f7c6b2d14f63822030d85337` |
| `worker/internal/provider/ollama.go` | `8893049b0c2dccb797178c828fc12e40dc1bde9e0c8ab8ddad401184e7fcdcb5` |
| `worker/experiments/crosschain/main.go` | `d8dbf88a7a4318755d838097b9292141e420ba49e5fe0f3a36456405934312ad` |
| `worker/experiments/crosschain/main_test.go` | `3f787a4467b0385ef0e46cc8a073baabd2d57d443a09328244cee65dd68e4722` |
| `verifier_bridge.py` | `55193942360106da720b8d7a93d5843d1b09d0a8aa7e56bdd70cb72f22df0ac6` |
| `report.py` | `8ba7a3afdeda710c10dcd188d336c5a8051172a50322ab5220cd752e860957ff` |
| `test_report.py` | `d8c1240238884caf010ae5d937eba84338bf3c414c6f25c54ecac7d9e85289d9` |

Live execution command **after** freezing/publishing these checks, starting a
ready local Ollama service with the expected model and a ready local verifier
image (execute once; the output path must not already exist):

```sh
cd worker
go run ./experiments/crosschain -root .. -ollama-url http://127.0.0.1:11434 -out ../experiments/challenge-v1-cross-chain-failure-map-2026-09-24/local-run.private.json
```

At startup and around each generation the runner checks the fixture and source
hashes, restricted verifier readiness, Docker image ID, Lean 4.19.0 version,
verifier configuration/source hashes, Ollama availability and the exact
`sha256:98d095df8e1d58c088000c8a97fe5d4bc28b97fad4ef77d7a794be30544299d1`
model digest. The provider independently records that digest with every call;
the runner requires it to match. It saves each attempt and block to the private
JSON atomically. A mismatch stops the run with partial data; never resume or
compare partial results. The verifier bridge also records the Docker image ID,
resource limits, toolchain version and relevant source hashes in the private
configuration.

After a completed run, generate the redacted report without publishing the
private response/candidate/diagnostic archive:

```sh
python3 experiments/challenge-v1-cross-chain-failure-map-2026-09-24/report.py experiments/challenge-v1-cross-chain-failure-map-2026-09-24/local-run.private.json > /tmp/opencode/crosschain-redacted-report.json
```

## Archived result (2026-09-24)

The standalone architecture decision and next local-test boundary are in
[`DECISION.md`](DECISION.md).

The run completed all **60 paired blocks** (12 fixtures × five base seeds), with
no abort. The full **redacted aggregate** is in [`RESULTS.json`](RESULTS.json),
including per-fixture paired outcomes and the per-block cumulative solved/cost
series. The original private archive remains local and untracked: SHA-256
`61cf98915ed26608b42e559360252041c95c55fdaa4a986c037cd94e8090ee42`.
No candidate proofs, raw provider responses, or Lean diagnostics are published.

| Base seed | Baseline solved / 12 | Map solved / 12 | Baseline-only | Map-only |
| --- | ---: | ---: | ---: | ---: |
| 121 | 6 | 7 | 0 | 1 |
| 132 | 7 | 6 | 1 | 0 |
| 143 | 5 | 5 | 0 | 0 |
| 154 | 6 | 6 | 0 | 0 |
| 165 | 6 | 6 | 0 | 0 |
| **Total paired blocks** | **30 / 60** | **30 / 60** | **1** | **1** |

The shared first two chains solved 28 blocks for both arms. The remaining 32
required a third request in each arm: one block was solved by both third
chains, one only by the baseline, one only by the map, and 29 by neither.
Overall paired outcomes were 29 both, 29 neither, and one exclusive to each
arm. These 60 observations repeat the same 12 theorems, so they are not 60
independent fixtures.

| Charged cost (shared prefix counted in each arm) | Baseline | Map |
| --- | ---: | ---: |
| Provider requests | 152 | 152 |
| Input tokens | 19,727 | 19,844 |
| Output tokens | 8,004 | 8,606 |
| Provider duration (ns) | 170,598,845,564 | 181,319,782,837 |
| Extracted proofs / Lean checks | 141 / 141 | 139 / 139 |
| Lean elapsed (ms) | 168,775 | 166,370 |
| Format failures | 11 | 13 |

The physical common prefix cost 120 requests, 112 Lean checks, 14,360 input
tokens, 5,910 output tokens and 134,948 Lean ms. Each incremental third arm
cost 32 requests: baseline 5,367 input / 2,094 output tokens, 29 Lean checks
and 33,827 Lean ms; map 5,484 input / 2,696 output tokens, 27 Lean checks and
31,422 Lean ms. Neither arm had provider failures, Lean timeouts, verifier
errors, or unknown input/output usage. Absolute relative differences (divided
by the larger total) for input/output/Lean time were **0.59% / 7.00% / 1.42%**
over all charged work and **2.13% / 22.33% / 7.11%** on incremental third
work. The predeclared comparison is **not cost-comparable**: third-request
output tokens exceed the 15% limit, and the arms differ by two Lean checks
(limit one). Solved counts and resource differences are descriptive, not
evidence of an efficiency advantage.

The predeclared condition for a next bounded cross-chain-findings field in a
local job protocol was not met (net map-only advantage zero, rather than at
least four; advantage in one seed, rather than three; only one theorem helped,
rather than two). **Retain independent jobs and refine/replicate this coarse
failure-map hypothesis before adding protocol state.** This result does not
justify a first-class agent abstraction or remote allocation. It concerns one
local model, five seeds and 12 repeated fixtures; seeded outputs may correlate,
the tactic labels are coarse, and the map adds prompt tokens. It does not rule
out other forms of collaboration.

Integrity: the fixture, pinned provider, runner (`main.go`, `main_test.go`) and
verifier bridge SHA-256 hashes in the run configuration match the hashes frozen
above; the run records Lean 4.19.0 and the expected model digest. **After the
run**, two report-only defects were fixed: the ≤15% cost gate now uses
unrounded percentages (rounding is only for display), and the per-fixture
table now includes fixtures never solved by either arm. The configuration in
`RESULTS.json` retains the *execution-time* reporter/test hashes from the
frozen table; the corrected `report.py` SHA-256 is
`b80dc4b1c68ebc275f075659ce60918c60bf947f20c75fd2977555608ef71752`
and corrected `test_report.py` SHA-256 is
`3feedd6e5899ea1d6782597a3165f8dd5a6dbaa986199837976895b376aec713`.
The frozen plan, runner, and private run archive were not changed. The
corrected report tests pass, and regeneration from the private archive matches
`RESULTS.json` byte-for-byte.
