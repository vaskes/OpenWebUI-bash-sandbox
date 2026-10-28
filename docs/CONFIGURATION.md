# Configuration Reference

Every knob you can turn on this stack, organised by service. All env vars
are set in `docker-compose.yml` unless noted otherwise.

## Sandbox agent (`sandbox-agent` service)

| Env var                | Default                                  | Description                                                    |
|------------------------|------------------------------------------|----------------------------------------------------------------|
| `AGENT_WORKSPACE`      | `/workspace`                              | Path the agent considers its writable surface. Bind-mount this. |
| `AGENT_SESSIONS_FILE`  | `/workspace/.agent-sessions.json`         | Where the per-session registry is persisted.                   |
| `AGENT_TOKEN`          | (empty → 503)                             | Bearer token. Mirrored from `OPENAPI_PROXY_TOKEN`. Required.   |
| `AGENT_LOG_LEVEL`      | `INFO`                                    | uvicorn log level (`debug`, `info`, `warning`, `error`).      |
| `AGENT_PORT`           | `8082`                                    | uvicorn listen port.                                           |
| `AGENT_STDOUT_LIMIT`   | `512k`                                    | How much text one command may return in one exec. 32k..1024k.  |
| `AGENT_BINARY_LIMIT`   | `2048k`                                   | How large a file `/files` will serve. `0` = unlimited.         |
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

The writable layer is the container's own: it is discarded when the
container is recreated, and it does not move the boundary. The
container stays the boundary either way.

The agent logs a warning at startup when `sudo` is present — either
that the mode is on, which of the two runtime settings is still in
the way, or that the root filesystem is read-only. Full description,
including what the mode does *not* give: [`docs/SECURITY.md`](SECURITY.md),
section 9.

### Size values

`AGENT_STDOUT_LIMIT` and `AGENT_BINARY_LIMIT` accept a plain byte count or
a suffix: `b`, `k`/`kb`, `m`/`mb`, `g`/`gb` (`512k`, `2m`, `1g`, `1048576`).
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

### Filesystem layout inside the container

| Path                | Source                                  | Writable? |
|---------------------|-----------------------------------------|-----------|
| `/workspace`        | bind-mount `/opt/agent-workspace`       | yes       |
| `/home/agent`       | the image itself (the code lives there)  | no — the root filesystem is read-only; **never** mount a volume here either |
| `/var/cache/apt`    | named volume `sandbox-agent-apt`        | yes (the download cache only) |
| `/home/agent/.cache/pip`  | named volume `sandbox-agent-pip` | yes       |
| `/opt/sandbox/`     | baked into image                        | no        |
| `/home/agent/.ssh`  | bind-mount `/opt/agent-workspace/.ssh` (read-only) | no |
| `/tmp`              | container tmpfs                         | yes (ephemeral) |
| `/usr`, `/etc`, `/var/lib/apt`, `/var/lib/dpkg` | the image itself | no — the read-only root filesystem is what makes `apt-get` fail in a privileged deployment (`AGENT_READ_ONLY=false` turns the root filesystem writable) |

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
| `sandbox-agent-apt`   | `/var/cache/apt`             | apt download cache                   |
| `sandbox-agent-pip`   | `/home/agent/.cache/pip`     | pip download cache                   |
| `open-webui-data`     | `/app/backend/data`          | Open WebUI's SQLite DB (chats, users, settings) |

The Open WebUI DB **must not** be bind-mounted into `/workspace` — that
would let the LLM do `rm -rf /workspace/.open-webui-data` and wipe the
entire chat history. Wave 3 fix #C1.

**Nothing is mounted at `/home/agent` itself, and nothing may be.** The
agent's code lives there (`WORKDIR` is `/home/agent` so the import path
resolves), and Docker seeds a named volume from the image on first use
and never re-syncs it. A volume at that path therefore shadows the code
with the copy from the first run: every later image rebuild becomes a
no-op, the repository moves and the container does not. That is exactly
what the production stack did until v2.5.0. Mount a sub-path if you need
to persist something — `/home/agent/.cache/pip` is the one that matters.

Delete these with `docker volume rm <name>` to start fresh.

## Bind-mounts (host)

| Host path                              | Container path        | Mode  | Purpose                        |
|----------------------------------------|-----------------------|-------|--------------------------------|
| `/opt/agent-workspace`                 | `/workspace`          | rw    | Main working dir               |
| `/opt/agent-workspace/.ssh`            | `/home/agent/.ssh`    | ro    | SSH keys for outbound ssh      |

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
