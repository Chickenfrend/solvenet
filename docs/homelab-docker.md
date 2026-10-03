# Docker homelab (Linux Docker Engine + Compose)

This example runs one private site, one local SQLite coordinator and one Go
Ollama worker. Use a trusted LAN or private VPN; the site has no user login.
The unauthenticated coordinator has **no published port**. Do not publish it or
route it from the internet. The worker and coordinator API have no internet
authentication. Docker Engine on Linux with Compose v2, a working cgroup
resource limiter, and enough disk for Lean and an already-installed Ollama model
are required (Linux/amd64 for the checked-in Lean download). The
verifier download/build uses the Lean version in
`lean/lean-toolchain` (currently 4.19.0); the Python and Go base images and Go
toolchain are versioned in their Dockerfiles. Retain the image digests from
`docker image inspect` if you need bit-for-bit repeatable rebuilds after registry
tags change. Build from a checked-out revision; don't upgrade source and images
independently.

## First start

From the repository root:

```sh
# A Docker bind workspace must have the SAME absolute path on host and in
# coordinator. Each request creates a private temporary subdirectory here.
sudo install -d -m 0700 -o 10001 -g 10001 /var/lib/solvenet/verify
cp .env.homelab.example .env
# Edit .env: OLLAMA_MODEL, SITE_BIND_IP (trusted LAN interface), DOCKER_GID,
# SOLVENET_PROGRESS_TOKEN (generate with openssl rand -hex 32).
docker compose build verifier coordinator site worker
docker compose up -d site worker
docker compose ps
```

The Docker socket group ID can be obtained with
`stat -c %g /var/run/docker.sock`; set `DOCKER_GID` to this numeric ID in `.env`.
The coordinator is the **only** Compose service with socket access, through
that group. Docker socket access is effectively root-equivalent on the host:
run this stack only on a trusted Docker host and restrict access to the
coordinator, site and Compose project. The socket is never passed to the
internet-facing site, the worker, or a container holding provider API keys.
The host Docker daemon must be able to see both the verifier image and
`/var/lib/solvenet/verify` at that exact path (a remote Docker context or
Docker Desktop VM with a different filesystem does not satisfy this example).
Avoid rootless Docker without adapting socket permissions and workspace paths.

Initialize the site settings once, after the services start (these commands
run inside the site container; no coordinator DB mount is involved):

```sh
docker compose exec site python -m solvenet_homelab.server --db /data/homelab.db set-coordinator http://coordinator:8080
docker compose exec site python -m solvenet_homelab.server --db /data/homelab.db add-model 'ollama/qwen2.5-coder:7b' 'Local Qwen' Ollama 'On this device'
```

Replace `qwen2.5-coder:7b` in the second command with your exact
`OLLAMA_MODEL`. The worker advertises `ollama/` followed by that model name;
the site model ID must match exactly. One worker process offers one model. A
different LAN Ollama server should be labeled `Local network` instead of
`On this device` when adding its card. Configured cards become available for
new runs only when the worker has recently checked in. Open
`http://<SITE_BIND_IP>:<SITE_PORT>/` on a phone on your trusted network.
The default bind IP is `127.0.0.1` for local-only setup; explicitly choose
your trusted LAN interface or VPN IP, **not** `0.0.0.0`, and firewall that port
to trusted devices. The backend network is internal and the coordinator has no
host port. Never route the site to the public internet without adding access
control first.

The Ollama worker posts short, bearer-authenticated progress previews directly
to the site over the private Compose network. Generate one random
`SOLVENET_PROGRESS_TOKEN` in `.env` for both containers; it is never sent to the
coordinator. The site holds at most 32 previews of 8 KiB each in memory for up
to a minute. The run page polls a small fragment while active, labels it
partial/unverified, and offers a manual refresh; after a stalled worker or site
restart the preview may disappear. The final candidate, Lean diagnostics, raw
model response and usage come from the coordinator on a full run refresh.
Outside Compose, use the same token in both processes and pass the worker
`-progress-url=http://<private-site-origin>`; omit the flag to disable previews.

### Optional OpenAI hosted worker

The `hosted` Compose profile starts a second worker for OpenAI API access.
It has outbound HTTPS via the frontend network, no Docker socket and no
published port. Create a key file outside the repository, readable by container
UID 65532 (Compose mounts it read-only), and set `OPENAI_KEY_FILE` to its
absolute path in `.env` (the path only, never the key). For example, create the
external directory and enter the key with hidden terminal input (from the
repository root):

```sh
sudo install -d -m 0700 /var/lib/solvenet/secrets
sudo python3 worker/setup_openai_key.py /var/lib/solvenet/secrets/openai_api_key
sudo chown 65532:65532 /var/lib/solvenet/secrets/openai_api_key
sudo chmod 0400 /var/lib/solvenet/secrets/openai_api_key
```

Compose's file-backed secret uses a read-only bind mount, so the host ownership
and mode must allow UID 65532 to read it; secret UID/mode declarations do not
change that host file. The worker sees `/run/secrets/openai_api_key`. No other
service mounts it. The helper prompts without terminal echo, refuses an echoing
fallback, creates an exclusive mode-0600 file and removes a partial file on
write failure or Ctrl-C/SIGTERM. It never overwrites an existing file. Keep the
key out of shell arguments/history; the only argument is the external path.
For rotation, stop the worker and remove the old external file before rerunning
the helper, then reapply the Compose ownership/mode commands above. Configure
these **non-secret** settings in `.env`:

```sh
OPENAI_KEY_FILE=/var/lib/solvenet/secrets/openai_api_key
OPENAI_MODEL=gpt-4o-mini
OPENAI_PROFILE=chat-json
OPENAI_CONTEXT=32768
OPENAI_CONTEXT_BYTES=32768
OPENAI_MAX_OUTPUT=16384
# For responses-reasoning only: omitted, low, medium or high.
OPENAI_REASONING_EFFORT=
```

Model IDs are configurable: select the API contract explicitly for unfamiliar
models. `chat-json` uses Chat Completions JSON mode and supports temperature/seed;
`responses-reasoning` uses Responses strict JSON schema and **does not** support
those sampling settings. Reasoning effort is separate from sampling. Capacities
are operator-supplied limits, not claims about model quality or pricing. See
[worker API profiles](openai-profiles.md). Then run:

```sh
docker compose --profile hosted up -d site worker hosted-worker
docker compose exec site python -m solvenet_homelab.server --db /data/homelab.db add-model 'openai/gpt-4o-mini' 'Hosted GPT' OpenAI 'Cloud API'
```

Match the card ID to `openai/$OPENAI_MODEL` exactly. It appears beside Ollama
cards; a LAN Ollama endpoint remains `Local network` regardless of its HTTP
transport. Cards are selectable only after a worker checks in. Stop hosted
usage with `docker compose stop hosted-worker`. The coordinator and site receive
the model ID and ordinary job/result fields, never the key or account details.
Keep the key out of `.env`, CLI arguments, site settings and logs. For a local
worker outside Compose, set `OPENAI_API_KEY` or `OPENAI_API_KEY_FILE` (not both)
and run `solvenet-worker -provider openai -model gpt-4o-mini` with the usual
coordinator URL. A missing key prevents worker startup; a revoked key produces
a permanent worker provider failure (`OpenAI HTTP 401`) without forwarding the
provider error body. On HTTP 401/403 it stops advertising the model until the
key is corrected and the worker is restarted; the card becomes Offline after
its last worker signal ages out. Compose deliberately does not restart the
hosted worker automatically. For rotation, stop it first, replace the external
file and restore its ownership/mode, then recreate to refresh the mounted file
and current model/profile settings:

```sh
docker compose stop hosted-worker
# Rotate the external key file, or change the non-secret settings in .env.
docker compose --profile hosted up -d --force-recreate hosted-worker
```

A live worker initially reports **unobserved**, not ready. Startup and health
claims never call OpenAI. The existing routing policy permits an unobserved
worker to accept explicitly scheduled jobs; successful structured generation
changes health to ready on the next claim. Failures mark it unavailable (with a
fixed public reason); restart after correcting credentials/configuration or
temporary network/rate/service problems. 401/403 publishes unavailable and
stops immediately, with no repeated paid calls. Stopped/stale cards become
Offline; changing a card does not change worker credentials or configuration.

#### Explicit worker-local checks

For native setup, build from the worker directory and create the external file
as the worker user (directory 0700, file 0600):

```sh
# Run from the repository root; ensure $HOME/.local/bin is on PATH.
mkdir -p "$HOME/.local/bin"
(cd worker && go build -o "$HOME/.local/bin/solvenet-worker" ./cmd/solvenet-worker)
mkdir -p "$HOME/.config/solvenet"
chmod 0700 "$HOME/.config/solvenet"
python3 worker/setup_openai_key.py "$HOME/.config/solvenet/openai_api_key"
```

Export only its path:

```sh
export OPENAI_API_KEY_FILE="$HOME/.config/solvenet/openai_api_key"
solvenet-worker -provider openai -model gpt-4o-mini -openai-profile chat-json -openai-check
solvenet-worker -provider openai -model gpt-4o-mini -openai-profile chat-json -coordinator http://127.0.0.1:8080
```

The first command exits after validating local configuration: `unobserved`
means configured, with API access and generation compatibility **not checked**.
Missing/invalid credentials or configuration (including invalid CLI capacities)
return fixed unavailable health JSON and exit 1. Check-mode parse errors never
echo argument values into stderr.
For manual runs, `OPENAI_API_KEY` is also supported: obtain it with an interactive
hidden read (`read -rs OPENAI_API_KEY; export OPENAI_API_KEY`), unset
`OPENAI_API_KEY_FILE`, and unset the key after use. Never put its value in command
arguments, a `.env` file, URLs, site/database settings or logs. Native rotation
requires stopping the process and starting it again; keys are read once.

Only the following explicit flag authorizes a **paid** check (one call, no
retries; no coordinator jobs or storage):

```sh
solvenet-worker -provider openai -model gpt-4o-mini -openai-profile chat-json -openai-check -openai-check-paid
# Compose: use the same model/profile/capacity flags as hosted-worker.
docker compose --profile hosted run --rm --no-deps hosted-worker -provider=openai -model=gpt-4o-mini -openai-profile=chat-json -openai-check -openai-check-paid
```

The generation check has a 60-second deadline and output cap of 256 tokens for
Chat or 1024 for Responses, further limited by configured output capacity.
Responses' cap includes hidden reasoning; truncation/incomplete/refusal/malformed
output is **inconclusive**, never ready. A successful check says only that one
structured `model.respond` call worked, not that proofs or collaboration work.
No metadata-only probe is used: metadata access would not establish generation
compatibility. Check results are printed as fixed public health JSON; standalone
checks do not persist readiness or transfer credentials to the coordinator/site.
Actual worker job outcomes supply subsequent public health. API-key access is
separate from ChatGPT subscriptions and SolveNet user login.

The worker sends theorem/imports and strategy/repair messages with JSON proof
instructions. `chat-json` maps the job output budget to Chat Completions
`max_tokens` and passes optional temperature/seed. `responses-reasoning` uses
Responses `max_output_tokens`, strict `text.format`, `store:false`, and optional
worker-local reasoning effort; it does not accept temperature/seed. Both report
provider input/output tokens when returned (otherwise Unknown), model, finish
reason/status, requested settings and bounded sanitized raw diagnostics.
Rate limits, network failures and server errors are transient: health reports
unobserved/recovering and normal scheduled jobs with bounded assignment retries
can restore readiness, without any paid background probe. Authentication/access
rejection and recognized model/profile incompatibility latch unavailable until
restart. Job-local admission/context failures, invalid requests, incomplete
generations, refusals and malformed outputs fail that job while retaining any
previously established compatibility. Formal result strings use separate
credential redaction that preserves unrelated Lean literal escapes; malformed
structured content fails explicitly. Job deadlines cancel HTTP requests; Lean
verification remains the coordinator's source of truth.

### Ollama connectivity

Ollama is not started by Compose. Download the model first (`ollama pull
qwen2.5-coder:7b`). `OLLAMA_URL` defaults to
`http://host.docker.internal:11434` using Docker's Linux `host-gateway` mapping.
Ollama must listen on an interface reachable from containers (for example,
configure `OLLAMA_HOST=0.0.0.0:11434` on the host) and your firewall must
restrict port 11434 to the Docker host/trusted LAN. A host-bound
`127.0.0.1:11434` alone is **not** reachable from the worker. Alternatively set
`OLLAMA_URL=http://<trusted-LAN-Ollama-IP>:11434` and restrict that endpoint to
trusted clients. Test from the worker's network if jobs are stuck; Ollama
must already contain the named model. If using a LAN Ollama endpoint, register
its site card with `Local network` as above. Do not expose Ollama publicly.

## Health and proof smoke test

`docker compose ps` shows cheap HTTP health checks (`/` for the site and
`/health` for the coordinator). Worker presence appears on the Models page
after the worker checks in. To explicitly exercise the verifier, run:

```sh
docker compose exec coordinator python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=40).read().decode())"
```

`/ready` actually starts a pinned Lean verifier container; it is intentionally
**not** a periodic health check. Look for `{"status":"ready"}`; 503 includes
bounded diagnostics for missing Docker permissions/images/toolchain. On the
Problems page select a simple core fixture (e.g. `and-swap`), select the
worker model, request one initial chain and follow the new run link. A
model-generated candidate is `verified` only after Lean checks it; model
quality is not guaranteed. For a deterministic end-to-end verification check
without relying on model output, see the optional scripted-worker test below:

```sh
# Run against the internal API without publishing it. Return value contains run_id.
docker compose exec coordinator python -c 'import json,urllib.request; request=urllib.request.Request("http://127.0.0.1:8080/v1/runs",data=json.dumps({"statement":"(n : Nat) : n + 0 = n","attempts":1,"model":"scripted"}).encode(),headers={"Content-Type":"application/json"}); print(urllib.request.urlopen(request).read().decode())'
docker compose run --rm --no-deps worker -coordinator=http://coordinator:8080 -provider=scripted -proof=rfl -once
# Open /runs/<run_id> in the site, or inspect via docker compose exec coordinator.
```

The worker may need a few seconds for verification. The scripted check proves
the verifier path; the Ollama worker is exercised by the site form above.
If the scripted worker runs before the job is queued, repeat the one-shot
worker command. Consult `docker compose logs coordinator worker site` for
connectivity errors. The Docker verifier runs one container per candidate with
Lean 4.19.0, no network, read-only rootfs, no capabilities, no new privileges,
an unprivileged UID, a bounded writable `/tmp`, CPU/memory/process/file-size
limits and Lean/outer deadlines. The verifier only mounts its one temporary
request/result workspace, not the SQLite files or socket. Container isolation
depends on the host Docker daemon/kernel; it is not a hardened multi-tenant
security boundary.

## Data, upgrades and other coordinator origins

`site-data` contains only the site's SQLite settings, and `coordinator-data`
contains only the coordinator's SQLite jobs/results. They survive `docker
compose down` and `up`; **do not** use `down -v` unless deleting both databases
is intentional. For a consistent backup, stop the writers then archive volumes:

```sh
docker compose stop worker site coordinator
umask 077
mkdir -p "$HOME/solvenet-backups"
docker run --rm -v solvenet-homelab_site-data:/data:ro -v "$HOME/solvenet-backups":/backup busybox:1.36 tar -C /data -cf /backup/site-data.tar .
docker run --rm -v solvenet-homelab_coordinator-data:/data:ro -v "$HOME/solvenet-backups":/backup busybox:1.36 tar -C /data -cf /backup/coordinator-data.tar .
docker compose up -d site worker
```

Store archives outside this checkout with restricted access: coordinator
results and local model configuration may be sensitive. Restore each tar to
its matching **empty** named volume while services are stopped, preserving
numeric owners, then restart. To upgrade, back up both volumes, check out a
known revision, rebuild **verifier first** followed by coordinator/site/worker
(`docker compose build verifier coordinator site worker`), then `docker compose
up -d site worker`. The verifier image must be present in the *host* daemon
before coordinator `/ready` or candidate verification. Recheck `/ready`,
site browsing and a run. SQLite schema migration runs on startup.

The site is an HTTP API client; it never mounts the coordinator volume.
`set-coordinator` changes its selected API origin, and model cards are scoped
to that origin rather than copying history. This package configures only the
internal `http://coordinator:8080` origin. An authenticated external coordinator
would need an authenticated API transport/client and corresponding worker
configuration before it could be used outside a private tunnel; the current
site client has no credentials support. Do **not** point it at an unauthenticated
internet coordinator or assume switching the site URL redirects the bundled
local worker. Never put API keys in site settings, Compose model labels, or
coordinator job payloads.
