# Configuration Reference

Every knob you can turn on this stack, organised by service. All env vars
are set in `docker-compose.yml` unless noted otherwise.

## Sandbox agent (`sandbox-agent` service)

| Env var                | Default                                  | Description                                                    |
|------------------------|------------------------------------------|----------------------------------------------------------------|
| `AGENT_WORKSPACE`      | `/workspace`                              | Path the agent considers its writable surface. It is a volume over a filesystem with a ceiling — see `AGENT_WORKSPACE_SIZE`. |
| `AGENT_SESSIONS_FILE`  | `/workspace/.agent-sessions.json`         | Where the per-session registry is persisted.                   |
| `AGENT_TOKEN`          | (empty → 503)                             | Bearer token. Mirrored from `OPENAPI_PROXY_TOKEN`. Required.   |
| `AGENT_INBOUND_TOKEN`  | = the proxy's token                       | Bearer token the **agent** accepts on its own doors. Set it to a different value to keep the operator's stack token out of the container the model runs in — see [docs/SECURITY.md](SECURITY.md), "The sandbox is not a boundary". |
| `AGENT_LOG_LEVEL`      | `INFO`                                    | uvicorn log level (`debug`, `info`, `warning`, `error`).      |
| `AGENT_PORT`           | `8082`                                    | uvicorn listen port.                                           |
| `AGENT_STDOUT_LIMIT`   | `512k`                                    | How much text one command may return in one exec. 32k..1024k.  |
| `AGENT_BINARY_LIMIT`   | `2048k`                                   | How large a file `/files` will serve. `0` = unlimited.         |
| `AGENT_UPLOAD_MAX_BYTES` | `64m`                                   | The **transport** ceiling of the upload door (`POST /files`): how many bytes are read before the request is refused. `0` = unlimited. It is not the file policy — that is `AGENT_BINARY_LIMIT` — and it has to be **at least** as large, or the transport refuses an upload the door itself would have accepted. Every other door keeps the 2 MB body guard. |
| `AGENT_MCP_MAX_READ_BYTES` | `1m`                                  | How many bytes one MCP `read_file` may put into a model's context. `0` = no budget of its own. 0..64m. A file over the budget is refused, and the refusal names the file's size and the limit. The HTTP `/files` door keeps `AGENT_BINARY_LIMIT` — there the bytes land in a file, not in a prompt. |
| `AGENT_ALLOW_SUDO`     | `0`                                       | Build the image with `sudo`, so a command can become root. **Private, secured deployments only.** |
| `AGENT_NO_NEW_PRIVILEGES` | `true`                                  | The container's `no-new-privileges` flag. `false` is required by the privilege mode. |
| `AGENT_CAP_DROP`       | `ALL`                                     | Capabilities dropped from the container. `SYS_ADMIN` in the privilege mode. |
| `AGENT_READ_ONLY`      | `true`                                    | The container's root filesystem is read-only. `false` gives the privilege mode's root a filesystem it can write. |
| `AGENT_PIDS_LIMIT`     | `256`                                     | How many processes the agent's shell may spawn. Raise it for builds that fan out. |
| `AGENT_MCP_ALLOWED_HOSTS` | (empty → the check is off)          | Comma-separated `Host` header values the MCP endpoint answers for. **Empty = no Host check** — the default, and the SDK's own default for a service that is not bound to the loopback. Set it to turn the protection against DNS rebinding on: a request with any other `Host` is then refused with a 421. |

If `AGENT_TOKEN` is empty, the agent refuses to serve any request
except `/healthz` (returns 503). This is fail-closed by design.

### The privilege mode

`AGENT_ALLOW_SUDO=1` builds the agent image with `sudo` and a
`NOPASSWD` rule for the agent's user, so a command in the sandbox can
run `sudo su` and become root **inside the container**.

> **Private deployments in secured environments only.** The sandbox runs
> code whose input may be hostile; a prompt injection that reaches the
> shell gets root. The container stays the boundary — the mode gives
> nothing on the host — but inside the container the model is root.

Two more settings are needed, and they are compose-level: the setuid
bit is inert while the container's `no-new-privileges` flag is on, and
`su` cannot change the user while the capability bounding set is empty.
Set all three for the escalation:

```
AGENT_ALLOW_SUDO=1
AGENT_NO_NEW_PRIVILEGES=false
AGENT_CAP_DROP=SYS_ADMIN
```

**A fourth setting decides whether the root is useful.**
`AGENT_READ_ONLY` defaults to `true`: the container's root filesystem
is read-only, so the root shell owns the filesystem and cannot change
a byte of it. `apt-get` needs three writable paths
(`/var/lib/apt/lists`, `/var/lib/dpkg`, `/usr`) and gets *"Read-only
file system"* for all three — a model that tries to install a package
concludes the sandbox is broken, while the deployment is merely a
half-root. A deployment whose privileged root has to install packages
sets:

```
AGENT_READ_ONLY=false
```

**The rule: where sudo is on, the root has to be writable.** A root
that cannot write is a half-root, and a half-root is a deployment that
passes every check and fails at the first `apt-get`. So the two knobs
are not free: `AGENT_ALLOW_SUDO=1` with a read-only root filesystem is
a contradiction, and `tools/check-posture.sh` refuses it —

```bash
bash tools/check-posture.sh   # AGENT_ALLOW_SUDO and AGENT_READ_ONLY from the environment
```

The stock deployment (no sudo, read-only root) passes it, and so does
the privilege mode (sudo, writable root). The check is a refusal, not
a fixer: it says what is wrong and stops, so the decision stays the
operator's.

The writable layer is the container's own: it is discarded when the
container is recreated, and it does not move the boundary. The
container stays the boundary either way.

The agent logs a warning at startup when `sudo` is present — either
that the mode is on, which of the two runtime settings is still in
the way, or that the root filesystem is read-only. Full description,
including what the mode does *not* give: [`docs/SECURITY.md`](SECURITY.md),
section 9.

### Size values

`AGENT_STDOUT_LIMIT`, `AGENT_BINARY_LIMIT` and `AGENT_UPLOAD_MAX_BYTES`
accept a plain byte count or a suffix: `b`, `k`/`kb`, `m`/`mb`, `g`/`gb`
(`512k`, `2m`, `1g`, `1048576`).
A value that cannot be parsed, or that is outside the documented range, is a
**startup failure** — the container refuses to boot rather than silently
clamping to something the operator did not ask for.

### `AGENT_STDOUT_LIMIT`

The budget is the number of bytes one command may return in one exec.
When the output is longer, the tail is dropped and the payload says so:

```
[output truncated at 524288 bytes]
```

The tmux scrollback is sized from the budget, so the limit is honest in
both directions: an output that fits comes back **whole, on every run**,
and an output that does not fit is reported rather than handed back as a
plausible fragment. Two things make that true, and both are covered by
tests:

* the completion marker is matched in its *printed* form, so the poll
  cannot stop on the echo of the command that prints it — that echo is
  visible in the pane long before the command has produced anything;
* a begin marker printed before the command proves the capture still holds
  the start of the output. If it is gone, the pane overflowed and the
  result says so in the first line instead of pretending to be complete.

The scrollback is sized for lines of 64 bytes, not for full-width ones: a
200-column pane would only be able to deliver the budget for wide output,
and a plain `cat` of an ordinary text file would lose its beginning.

The budget is **not** a context-overflow guard. How much of a tool result
fits into the model's context is the client's business — the bot that talks
to this agent already caps tool results far lower than 512k.

### `AGENT_WORKSPACE_SIZE`

The ceiling on the sandbox's own storage. It is the size of the
filesystem that carries `/workspace`, and it is the whole bound: the
sandbox cannot write more than this, and when it tries, the write
fails with `ENOSPC` *inside the workspace*.

Default: `1g` (`2g` in the production stack).

**The workspace is on disk, in a filesystem of exactly that size.** The
ceiling cannot come from Docker: compose has no per-volume storage
option, and `--storage-opt size` only ever worked with the devicemapper
storage driver, which Docker no longer ships (this host runs
overlayfs). A named volume is a directory on the host's disk by
construction. So the ceiling comes from the filesystem instead:

* `tools/setup-workspace.sh` makes an ext4 filesystem in a **sparse
  file** of `AGENT_WORKSPACE_SIZE` and mounts it. The file cannot grow:
  the filesystem inside it runs out of space, and the write fails.

  The file is sparse when it is made, so an empty workspace costs a few
  megabytes. **After the workspace has held a lot, the host's disk pays
  the high-water mark:** the filesystem frees its blocks when the files
  are deleted, but the file does not give them back — ext4 does not
  punch holes in the file it lives in. Measured on the production host
  after a verification that filled the workspace and cleaned it up: the
  filesystem was empty (544 K used) and the image file still took 1.9 G.
  The ceiling is therefore the most the host can ever lose to the
  workspace — and the most it can lose, because the file cannot grow.
* The stack's `owbs-workspace` volume binds that **mount point**, so
  the container sees the filesystem and not a directory.

Two rules come with the number:

* **Keep it at or above `AGENT_BINARY_LIMIT`.** The file door accepts a
  file up to the binary limit and writes it into the workspace; a
  workspace smaller than the policy would refuse a file the door had
  already promised to accept. `tools/setup-workspace.sh` compares the
  two and refuses to make a workspace that cannot hold it.
* **Changing the ceiling wipes the workspace.** The ceiling is the size
  of the filesystem, and a different size is a different filesystem. The
  script says so out loud before it does it.

The price of the bound is honest and worth naming. The workspace is
**on disk and durable**: it survives a container recreate, and so does
the session registry (`/workspace/.agent-sessions.json`). What is *not*
bounded is the container's own writable layer — it is a directory on
the host's disk with no quota on it, so a deployment with a writable
root (`AGENT_READ_ONLY=false`) cannot bound its own storage. That is
the price of a root that can really write, and it is the operator's
call.

### `AGENT_WORKSPACE_PATH`

Where the workspace lives on the host. It is the **mount point** of
the filesystem `tools/setup-workspace.sh` makes, and the volume binds
it.

Default: `/opt/owbs-workspace` (the default stack),
`/opt/telegrambot/sandbox-workspace` (production),
`/tmp/owbs-workspace` (CI).

The path has to be **absolute**: Docker resolves a relative `device`
against the daemon's working directory, not against the compose file.

**The volume has to be the workspace.** Docker does not change a
volume's options once the volume exists: a volume left over from an
earlier stack keeps the driver it was made with, and a plain local
volume is a directory in Docker's own directory -- on the host's
partition, with nothing to stop it growing. So the volume is checked:

```bash
bash tools/check-workspace-volume.sh "$AGENT_WORKSPACE_PATH"
```

and a deployment that finds a volume which is not the workspace takes
it away and lets compose make it again with the driver the compose file
asks for. The check needs no root and changes nothing.

Make the workspace once per host, and check it without root:

```bash
sudo bash tools/setup-workspace.sh "$AGENT_WORKSPACE_PATH" "$AGENT_WORKSPACE_SIZE"
bash tools/setup-workspace.sh --check "$AGENT_WORKSPACE_PATH" "$AGENT_WORKSPACE_SIZE"
```

`make workspace` does the first one for the default stack, and
`make up` runs the second one before it starts anything: a workspace
that was never set up is an unbounded directory on the host's disk,
which is exactly the hole the ceiling closes.

### Filesystem layout inside the container

| Path                | Source                                  | Writable? |
|---------------------|-----------------------------------------|-----------|
| `/workspace`        | volume `owbs-workspace` over a filesystem of `AGENT_WORKSPACE_SIZE` | yes (and capped) |
| `/home/agent`       | the image itself (the code lives there)  | no — the root filesystem is read-only; **never** mount a volume here either |
| `/var/cache/apt`    | tmpfs (`128m` / `256m`)                 | yes (the download cache only) |
| `/home/agent/.cache/pip`  | tmpfs (`64m` / `128m`)           | yes       |
| `/opt/sandbox/`     | baked into image                        | no        |
| `/home/agent/.ssh`  | bind-mount `/opt/agent-workspace/.ssh` (read-only) | no |
| `/tmp`              | container tmpfs                         | yes (ephemeral) |
| `/usr`, `/etc`, `/var/lib/apt`, `/var/lib/dpkg` | the image itself | no — the read-only root filesystem is what makes `apt-get` fail in a privileged deployment (`AGENT_READ_ONLY=false` turns the root filesystem writable) |

### The python environment in the image

The sandbox is a python image, and the model installs python packages in
it. Three things make that cheap instead of painful:

| Path              | What it is                                                        | Writable? |
|-------------------|-------------------------------------------------------------------|-----------|
| `/opt/pylibs`     | the preinstalled packages (numpy, matplotlib, pandas, …)          | no — a seed, read-only |
| `/opt/wheelhouse` | the wheels those packages were built from                          | no |
| `pip`             | `python3-pip` + `python3-venv` from apt (so `ensurepip` is there) | — |

`PIP_FIND_LINKS=/opt/wheelhouse` is set in the image, so pip looks at the
local wheels **first**: installing one of the preinstalled packages is an
offline, instant operation. The network stays available for everything
else — pip falls back to it.

The list of preinstalled packages is the build argument
`AGENT_PYTHON_PACKAGES` (default: `numpy matplotlib pandas sqlalchemy
openai requests httpx pillow sympy pytest tqdm`). A smaller image is one
build flag away:

```bash
docker compose build --build-arg AGENT_PYTHON_PACKAGES="numpy matplotlib"
```

Two rules the image keeps:

* **a plain `pip install` is refused** — the interpreter is externally
  managed (PEP 668), and that refusal is correct: the image is not a
  package dump. The way to add a package is a virtual environment inside
  the workspace (`python3 -m venv /workspace/.venv`), which is writable,
  executable, and survives a container recreate;
* **`/opt/pylibs` is on the model's `PYTHONPATH`, not on the service's.**
  The tree carries its own `pydantic` and `httpx`; letting those shadow
  the agent service's pinned dependencies would break the service for a
  reason that has nothing to do with the model.

One consequence of the second rule is worth knowing, because it is
measured and not obvious: `PYTHONPATH` comes **before** a virtual
environment's own `site-packages`. So a version the model installs into
its own environment does **not** override a preinstalled one — pip reports
the install as successful, and the preinstalled version is what runs. The
way out is one environment variable:

```bash
env -u PYTHONPATH /workspace/.venv/bin/pip install "pandas==2.2.0"
env -u PYTHONPATH /workspace/.venv/bin/python script.py
```

### Healthcheck

```
curl -fsS http://127.0.0.1:8082/healthz
```

Runs every 10 s; container is marked unhealthy after 3 consecutive failures.

## OpenAPI proxy (`openapi-proxy` service)

| Env var                                | Default                          | Description                                                            |
|----------------------------------------|----------------------------------|------------------------------------------------------------------------|
| `OPENAPI_PROXY_TOKEN`                  | (empty → 503)                    | **Required.** Bearer token for `/bash`, `/shell_exec`, `/ws`, `/healthz`, `/api/config`, `/openapi.json`. |
| `OPENAPI_PROXY_UPSTREAM_TOKEN`          | = `OPENAPI_PROXY_TOKEN`          | Bearer token the proxy uses to call sandbox-agent. Set only if the agent has a different token. |
| `OPENAPI_PROXY_CORS_ORIGINS`           | (empty)                          | Comma-separated allowlist. Empty = no browser cross-origin access.     |
| `OPENAPI_PROXY_SSH_ALLOWLIST`          | (empty → ssh disabled)           | Comma-separated hostnames / CIDRs the `ssh` shortcut may target.       |
| `OPENAPI_PROXY_RATE_LIMIT_RPM`          | `120`                            | Per-token requests per minute. `0` = disabled.                         |
| `OPENAPI_PROXY_UPLOAD_MAX_BYTES`        | `67108864` (64 MiB)              | The proxy's **transport** ceiling for an upload (`POST /files`): how many bytes are read before the proxy refuses the request. A plain byte count, like the proxy's other numbers. The *file policy* belongs to the agent (`AGENT_BINARY_LIMIT`), and this number has to be at least as large — otherwise the proxy refuses an upload the agent would have taken. |
| `OPENAPI_PROXY_RATE_LIMIT_MAX_KEYS`     | `10000`                          | LRU cap on the in-memory rate-limit dict. Past this, oldest key evicted. |
| `OPENAPI_PROXY_WEBSOCKET_IDLE_TIMEOUT` | `300`                            | Close `/ws` connections idle this long (sec). `0` = disabled. New in v2.4.0. |
| `OPENAPI_PROXY_LOG_LEVEL`              | `INFO`                           | uvicorn log level.                                                      |
| `AGENT_EXEC_TIMEOUT`                   | `60`                             | Longest a single command may run, in seconds (5..3600). The proxy asks the agent for exactly this limit and then waits longer. New in v2.10.2; the range's ceiling raised to an hour in v2.14.0. The **shipped default is still 60** — the number is the deployment's to raise (see below). |
| `OPENAPI_PROXY_PORT`                   | `8090`                           | uvicorn listen port.                                                    |
| `SANDBOX_URL`                          | `http://127.0.0.1:8082`          | Upstream sandbox-agent URL.                                             |

`SANDBOX_URL` is **critical**: if it's empty or wrong, every `bash` call
returns `{"error": "sandbox-agent request failed: ..."}`. After changing it
in `docker-compose.yml` you must run `docker compose up -d` (not
`docker compose restart` — that doesn't re-read env).

### `AGENT_EXEC_TIMEOUT`

How long a single command may run before the agent gives up on it and
answers `timed_out: true` with the output it managed to produce. The
default is 60 seconds; the accepted range is 5..3600. A value outside the
range is a **startup failure**, not a clamp — a limit nobody can trust is
worse than a container that refuses to boot.

The proxy waits `AGENT_EXEC_TIMEOUT + 15` seconds for the agent, and that
gap is load-bearing. The agent answers *after* the limit expires — that is
what "timed out" means — so with the same number on both sides the answer
arrives at the very moment the proxy stops listening, and the caller gets
a transport error instead of the timeout it asked for.

The limit is a property of the command, not of the transport: a longer
command is not a faster answer, it is the same answer with more of it
already produced when the limit hits.

**The limit is the deployment's, and every path that starts the stack has
to pass the deployment's env file.** The compose interpolates
`${AGENT_EXEC_TIMEOUT:-60}`, so a proxy started without that file falls
back to a minute — silently, and with the caller still believing it may
run for an hour. That is not hypothetical: in production the side-car ran
with a 60-second limit for a whole release, because the deploy path did
not pass `/opt/telegrambot/.env.sandbox`, where the operator's
`AGENT_EXEC_TIMEOUT=3600` lives.

Two things follow, and the second one is the expensive half.

* **The caller and the side-car must agree.** The bot's contract says a
  command may run for 3600 s and waits 3630 s for the answer; a proxy
  that asks the agent for 60 s answers in a minute, and the caller is
  left waiting for a limit that no longer exists.
* **A command that is cut keeps the session.** The agent answers
  "timed out" and the caller moves on, but the command is still running
  in the session's shell — so the session stays occupied, and every later
  command in it is refused as "the session is busy". A limit that is
  shorter than the command therefore does not just shorten the command:
  it wedges the session it ran in.

The `agent-stack.service.example` template names the env file
(`COMPOSE_ENV_FILE`) for exactly this reason.

### An hour-long command is an operational event

The hard cap on one command is 3600 seconds (`MAX_TIMEOUT` in
`agent_api.py`): a command may hold a worker for an hour. That is the
operator's ruling — a build that takes an hour is a build that takes an
hour — and it has three consequences a deployment has to plan for:

* **A restart during a long command cuts it off.** The containers stop
  gracefully for 25 seconds (`timeout_graceful_shutdown`). A command
  that is still running after that is killed, and the caller sees a
  dropped connection rather than a timeout. Restart during quiet hours,
  or ask for a shorter limit.
* **The command holds its resources for the whole run.** `mem_limit`,
  `cpus` and `pids_limit` apply for as long as the command lives: a
  command that runs for an hour holds a core and its memory for an
  hour.
* **The caller has to wait longer than the command may run.** The proxy
  waits `AGENT_EXEC_TIMEOUT + 15` seconds for the agent, and the MCP
  relay waits 3660 seconds — an hour and a minute. The chain has to be
  longer at every step, or the answer is lost on the way back: the
  relay's horizon is the agent's cap plus its grace, and a client that
  gives up earlier than the relay never sees the answer at all.
* **The proxy's number is the number that counts.** The agent's cap is
  3600 s, but the proxy asks the agent for `AGENT_EXEC_TIMEOUT` and
  ignores the timeout the caller sent. A deployment that leaves the
  proxy at 60 s has an agent that can run for an hour and a proxy that
  can never use it. The knob is the deployment's; the shipped default
  is 60 s, and a deployment that wants the hour raises it in its env
  file and passes that file to every path that starts the stack.

### Healthcheck

```
curl -fsS http://127.0.0.1:8090/openapi.json
```

## Open WebUI (`open-webui` service)

| Env var                       | Default              | Description                                       |
|-------------------------------|----------------------|---------------------------------------------------|
| `WEBUI_AUTH`                  | `true`               | Require login                                     |
| `WEBUI_SECRET_KEY`            | (placeholder)        | **Change me** in production                       |
| `ENABLE_SIGNUP`               | `false`              | Allow new accounts                                |
| `DEFAULT_USER_ROLE`           | `user`               | Role for new signups                              |
| `ENABLE_AUDIO_STT`            | `true`               | Allow speech-to-text                              |
| `AUDIO_STT_ENGINE`            | `openai`             | Use OpenAI-compatible API                         |
| `AUDIO_STT_OPENAI_API_BASE_URL` | `http://host.docker.internal:8000/v1` | whisper-api URL — `host.docker.internal` is the Docker host as seen from the container |
| `AUDIO_STT_OPENAI_API_KEY`    | `sk-not-needed`      | whisper-api doesn't validate it                   |
| `SANDBOX_URL`                 | `http://sandbox-agent:8082` | (optional, used by some integrations)        |
| `TOOL_SERVER_CONNECTIONS`     | (JSON list)          | Pre-wires the OpenAPI tool connection             |

`WEBUI_SECRET_KEY` is **required** — the compose file refuses to start
without it (`${WEBUI_SECRET_KEY:?WEBUI_SECRET_KEY must be set}`), and
the template ships a `CHANGE_ME` placeholder for it. It is the key
Open WebUI signs its sessions with, so a deployment that leaves the
placeholder has an admin session anyone can forge. Generate one with
`openssl rand -base64 48`.

### `TOOL_SERVER_CONNECTIONS` format

```json
[{
  "type": "openapi",
  "url": "http://openapi-proxy:8090",
  "path": "openapi.json",
  "info": {
    "id": "sandbox-tools",
    "name": "SandboxTools",
    "description": "Persistent shell"
  },
  "config": { "enable": true },
  "auth_type": "bearer",
  "key": "${OPENAPI_PROXY_TOKEN}",
  "spec_type": "url"
}]
```

The `key` here is the same bearer token the proxy validates against
`OPENAPI_PROXY_TOKEN`. If they don't match, every call returns 401.

The token is *interpolated into this JSON by the compose file*, so it
has to survive that: no quote, no backslash, no whitespace, no
non-ASCII characters. A token that breaks the JSON does not fail loudly
— Open WebUI comes up without the sandbox tool and says nothing about
why. `openssl rand -hex 32` produces a token that always works, and
`make check-env` (which `make build`, `make up` and `make restart` all
run first) refuses a token that would break the document. The proxy
also logs the problem at start-up, so a stack started with
`docker compose up` directly is not silent about it.

## Metrics and monitoring

Both services expose a Prometheus exposition at `/metrics`. The endpoint
is authenticated like every other one — the numbers are harmless, but
an unauthenticated endpoint on a published port is a habit worth not
having.

| Metric                                | Service | What it counts                      |
|---------------------------------------|---------|-------------------------------------|
| `sandbox_proxy_requests_total`        | proxy   | requests handled                    |
| `sandbox_proxy_rate_limited_total`    | proxy   | requests refused with 429           |
| `sandbox_proxy_exec_timeouts_total`   | proxy   | upstream calls that ran out of time |
| `sandbox_proxy_upstream_errors_total` | proxy   | upstream calls that failed otherwise |
| `sandbox_agent_exec_total`            | agent   | commands executed                   |
| `sandbox_agent_exec_timeouts_total`   | agent   | commands that ran out of time       |
| `sandbox_agent_exec_refused_total`    | agent   | commands refused before they were typed, because they would stick the shell |
| `sandbox_agent_exec_stuck_resets_total` | agent | shells freed after they waited for the rest of an unfinished command |
| `sandbox_agent_active_sessions`       | agent   | sessions held right now             |

The counters live in the process, so they reset when the container
restarts — that is what a counter is for. The rates Prometheus computes
from them are what a dashboard shows.

A ready-made stack (Prometheus + Grafana + cAdvisor) is in
`docker-compose.monitoring.yml`. It is a separate compose project, so
the sandbox starts and works without it. The steps — creating the token
file, pointing the stack at the network of the sandbox, and the URLs it
lands on — are in the README, under "Monitoring".

## Volume mounts (named)

| Name                  | Mounted at                   | Purpose                              |
|-----------------------|------------------------------|--------------------------------------|
| `owbs-workspace`      | `/workspace`                 | The agent's workspace: a volume whose driver binds the mount point of a filesystem with a ceiling (`AGENT_WORKSPACE_SIZE`). Made by `tools/setup-workspace.sh`. |
| `open-webui-data`     | `/app/backend/data`          | Open WebUI's SQLite DB (chats, users, settings) |

**The agent has exactly one named volume, and it is the workspace.** The
name is a container for the filesystem, not a directory of Docker's own:
its driver binds a host path (`AGENT_WORKSPACE_PATH`), and that path is
the mount point of the ext4 filesystem that `tools/setup-workspace.sh`
makes out of a file of `AGENT_WORKSPACE_SIZE`. That is how a ceiling
that Docker cannot provide comes from the filesystem instead.

Every *other* path the sandbox may write is a tmpfs mount with a
ceiling. The apt and pip caches were volumes until v2.16.0; they are
tmpfs now.

The Open WebUI DB **must not** be bind-mounted into `/workspace` — that
would let the LLM do `rm -rf /workspace/.open-webui-data` and wipe the
entire chat history. Wave 3 fix #C1.

**Nothing is mounted at `/home/agent` itself, and nothing may be.** The
agent's code lives there (`WORKDIR` is `/home/agent` so the import path
resolves), and Docker seeds a named volume from the image on first use
and never re-syncs it. A volume at that path therefore shadows the code
with the copy from the first run: every later image rebuild becomes a
no-op, the repository moves and the container does not. That is exactly
what the production stack did until v2.5.0.

Delete these with `docker volume rm <name>` to start fresh.

## Bind-mounts (host)

| Host path                              | Container path        | Mode  | Purpose                        |
|----------------------------------------|-----------------------|-------|--------------------------------|
| `/opt/agent-workspace/.ssh`            | `/home/agent/.ssh`    | ro    | SSH keys for outbound ssh      |

The workspace is **not** in this table: it is a volume (`owbs-workspace`)
over a filesystem with a ceiling — see `AGENT_WORKSPACE_SIZE` and
`AGENT_WORKSPACE_PATH`. The only host path the agent sees is the
read-only SSH key directory. A read-only bind stays read-only even for
a root command inside the container — remounting it needs
`CAP_SYS_ADMIN`, which the container does not have.

## Ports (host → container)

| Host port           | Service        | Notes                                 |
|---------------------|----------------|---------------------------------------|
| `127.0.0.1:3001`   | open-webui     | Web UI                                |
| `127.0.0.1:8090`   | openapi-proxy  | OpenAPI + WS                           |

The address the stack is published on is the operator's choice, and the
compose file spells it out as a parameter whose default is the loopback:

| Variable            | Default        | Notes                                 |
|---------------------|----------------|---------------------------------------|
| `SANDBOX_BIND_HOST` | `127.0.0.1`    | The host address the published ports bind to. `0.0.0.0` publishes on every interface — that is the choice of a deployment that wants the port reachable from the network, and it leaves the bearer token as the only thing between that network and a shell. |

`sandbox-agent` has **no host port mapping** — only `expose:` in compose
so the openapi-proxy can reach it via the Docker network. The bearer
token is still required for defence in depth.

The internal container ports (3001, 8082, 8090) stay the same — they
are referenced by inter-service URLs.

## systemd unit

The repository ships `agent-stack.service.example` — a **template**: it
names one machine (a user, a group, a directory), and those three lines
are the only ones a copy has to change.

```bash
cp agent-stack.service.example /etc/systemd/system/agent-stack.service
$EDITOR /etc/systemd/system/agent-stack.service   # User, Group, WorkingDirectory
systemctl daemon-reload && systemctl enable --now agent-stack
```

`User=git` and `Group=docker` in the template — adjust if your Docker
socket is owned by a different group. The `WorkingDirectory` must point
at the directory containing `docker-compose.yml`.

Both `ExecStart` and `ExecReload` pass `--build`: neither service pins
an image, so `up -d` reuses whatever tag is present and serves the
previous code. A unit that starts at boot would then serve yesterday's
build forever.

## The production stack (`docker-compose.production.yml`)

The production compose file is the two-service stack the telegram bot
talks to: `sandbox-agent` and `openapi-proxy`, no Open WebUI. It is a
different file with a different set of variables, and it is the one the
operator's own deployment path uses.

| Variable | Required | What it does |
|----------|----------|--------------|
| `AGENT_STACK_TOKEN` | **yes** | The bearer token, shared by both services. The compose file refuses to start without it (`${AGENT_STACK_TOKEN:?...}`). |

`AGENT_STACK_TOKEN` is the production stack's name for the token the
development stack calls `OPENAPI_PROXY_TOKEN`: the proxy validates
incoming callers against it and forwards it to the agent as
`AGENT_TOKEN`. One value, two names, one file.

```bash
export AGENT_STACK_TOKEN="$(openssl rand -hex 32)"
docker compose -f docker-compose.production.yml up -d --build
```

The production file hardcodes the loopback for the published proxy port
(`127.0.0.1:8090`) and uses its own network (`tb-net`) and project name
(`telegrambot-sandbox`) — it is not the development stack with different
values. `--build` is not optional here either: neither service pins an
image, so a plain `up -d` serves the previous build without a word.
