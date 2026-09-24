# Local runner implementation

The frozen design is in `PLAN.md`. This runner calls the pinned Go Ollama provider
directly (`worker/experiments/crosschain`), so its system prompt, streaming JSON
schema, and proof extraction come from the provider rather than a reimplementation.
The Python bridge invokes the coordinator's `ContainerVerifier` with its default
restricted Docker configuration and 10-second Lean timeout. The fixture is decoded
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
| `verifier_bridge.py` | `ec01b4605c4770346ced476e5648a60e1028a70ca060db4ccec4da9c9395b23c` |
| `report.py` | `8ba7a3afdeda710c10dcd188d336c5a8051172a50322ab5220cd752e860957ff` |
| `test_report.py` | `42755c7584b25e716884c066e89e63d43a4e2236baed392c1d038f62d70c320a` |

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
