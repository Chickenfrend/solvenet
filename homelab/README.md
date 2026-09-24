# Private homelab service (H2–H4)

The homelab site is a separate Python process and keeps only local operator
settings in its own SQLite database. Models and Problems read fixture sets,
recent runs and model activity via the coordinator HTTP API.

From the repository root, with Python 3.11+:

```sh
python3 -m pip install -e ./homelab
# Start the coordinator separately (see coordinator/README.md), with its own DB.
solvenet-homelab --db homelab.db set-coordinator http://127.0.0.1:8080
solvenet-homelab --db homelab.db add-model ollama/qwen2.5-coder:7b "Local Qwen" Ollama "On this device"
solvenet-homelab --db homelab.db serve
```

Open http://127.0.0.1:8081/. `--db` must point to a separate file from the
coordinator's `--db`; use an absolute path if starting from another directory.
`serve` defaults to the loopback interface and port 8081. An unconfigured site
defaults to the loopback coordinator at port 8080. `set-coordinator` can select
one HTTP(S) origin at a time. Model entries (display name, coordinator model ID,
provider and execution location) are scoped to that origin and can be updated
with another `add-model` invocation. Configure actual worker/model connections
and any provider credentials at the worker, not here. Use `Local network` for
Ollama served by a LAN endpoint even though its provider remains Ollama.
Configured models alone are **not** evidence of worker availability. Recent
worker claims show idle presence; live leases plus worker contact show a job
as working. Expired worker contact shows Offline, and models never seen by the
coordinator show Unknown. A missing/incompatible activity API also shows
Unknown rather than inventing activity. Refresh the page to update the status.
The Problems link opens a vertical fixture list. On a problem detail page you can
choose a configured model with a recent worker signal (Idle or Working), an
independent or repair strategy, initial chains, output tokens and generation
timeout. A successful form redirects to a run confirmation link. The coordinator
`POST /v1/fixture-runs` records the checked-in fixture identity for exactly one
problem; it rejects changed fixture hashes. The form and coordinator never send
the fixture's validation-only reference proof to workers. Form submissions use
a server-validated CSRF token and strict same-site cookie.
Full candidate/verification inspection follows in H5.
Coordinator history stays on the
coordinator; switching origins immediately changes API-backed data and never
copies it into the site database.

Use only on localhost or a trusted LAN/private VPN. Neither the site nor the
current coordinator has public-deployment authentication. In particular, do
not connect to an internet-facing unauthenticated coordinator or expose either
service publicly. Coordinator URLs with embedded credentials, paths or query
parameters are rejected; no credentials belong in site SQLite, model names,
templates or coordinator requests. The site makes bounded HTTP requests with
a 2-second timeout and shows an offline/invalid-response message when the
coordinator cannot supply the overview. It never calls `/ready`.

Run the site tests with:

```sh
PYTHONPATH=homelab/src python3 -m unittest discover -s homelab/tests -v
```
