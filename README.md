# OpenWebUI-bash-sandbox

> Persistent bash sandbox for [Open WebUI](https://github.com/open-webui/open-webui) via
> built-in External Tool Servers — and the same sandbox over the
> [Model Context Protocol](https://modelcontextprotocol.io/) for every other
> client. One `bash` tool, persistent state, no middleware patching, no
> model reconfiguration.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Open WebUI: compatible](https://img.shields.io/badge/Open_WebUI-compatible-blue)](https://github.com/open-webui/open-webui)
[![Docker Compose: v2](https://img.shields.io/badge/Docker_Compose-v2-2496ED)](https://docs.docker.com/compose/)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)](https://www.python.org/)
[![Tests: 719](https://img.shields.io/badge/tests-719_total-brightgreen)](tests/)

> ## ⚠️ NOT MULTI-TENANT — single operator only
>
> This stack assumes **one operator controls the Open WebUI instance and
> the agents behind it**. One bearer token, one tmux server, one
> workspace: anyone who can talk to the UI can read another chat's
> session and run commands with the same privileges.
>
> **This is the original design, not a gap left open.** The alternative —
> bolting per-user authentication onto a shared tmux server — would be
> the wrong abstraction, because the isolation has to happen at the
> container boundary, not at the token boundary.
>
> Do not expose this to the public internet, and do not hand one
> instance to users who are supposed to be isolated from each other.
> For a multi-tenant deployment, run a **separate `sandbox-agent` per
> user**, each with its own `AGENT_TOKEN`, its own `/workspace` and its
> own volumes.
>
> What this means in practice is spelled out under
> [Limitations → Single trust boundary](#single-trust-boundary-by-design-not-by-accident).

---

## Why this exists

Open WebUI has first-class support for **External Tool Servers** that speak
OpenAPI 3.0. Instead of patching Open WebUI or writing a custom
integration, you drop in a single HTTP service that exposes a `bash` tool.
The LLM calls it, gets back a shell transcript, and continues — with
**persistent state across calls** in the same chat (cwd, env vars, history,
running processes).

This repo is the minimal, production-shaped packaging of that pattern:
**two services** in this repository — `openapi-proxy` and `sandbox-agent` —
plus an optional upstream `open-webui` container that is not built from here.
One bearer token, two doors (OpenAPI and MCP), zero patches.

```
HTTP:  User ──▶ Open WebUI ──▶ openapi-proxy (OpenAPI 3.0, bearer auth) ──▶ sandbox-agent (UID 1000, r/o fs) ──▶ tmux ──▶ bash
MCP:   User ──▶ any MCP client ──▶ openapi-proxy (POST /mcp, bearer auth) ──▶ sandbox-agent ──▶ tmux ──▶ bash
```

## Features

- ✅ **One `bash` tool** — runs any shell command in a persistent sandbox
- ✅ **MCP endpoint** — the same sandbox over the Model Context Protocol
  (`POST /mcp`): `bash`, `read_file`, `file_info`, `delete_file`, for any
  client that is not Open WebUI
- ✅ **Persistent state** — cwd, env vars, history survive across LLM calls
- ✅ **Auto-wire** — `TOOL_SERVER_CONNECTIONS` env var pre-loads the tool
- ✅ **Zero patches** — uses Open WebUI's built-in External Tool Servers
- ✅ **Bearer-token auth** on the proxy (required, constant-time compare)
- ✅ **CORS allowlist** (opt-in, off by default)
- ✅ **Rate limiting** (token bucket, configurable rpm)
- ✅ **SSH host allowlist** (CIDR + hostname, empty = disabled)
- ✅ **Per-WebSocket session** — no cross-user output leak
- ✅ **Singleton HTTP client** — no per-request TCP handshake
- ✅ **systemd unit** for autostart on boot
- ✅ **Multi-stage Dockerfiles** — small images
- ✅ **719 unit + integration tests** (stdlib `unittest`, no pytest required)
- ✅ **CI: ruff + ruff B-rules (SAST) + gitleaks + coverage threshold**

## Quick start (60 seconds)

```bash
git clone https://github.com/vaskes/OpenWebUI-bash-sandbox.git
cd OpenWebUI-bash-sandbox
cp .env.example .env
# Edit .env — at minimum set OPENAPI_PROXY_TOKEN and WEBUI_SECRET_KEY.
# Suggested: openssl rand -hex 32 and openssl rand -base64 48
docker compose up -d --build
```

Wait for `docker compose ps` to show all three containers `(healthy)`. Then
open <http://localhost:3001>, start a new chat, click the **🔧 Tools**
button, enable **SandboxTools**, and type:

```
pwd && ls /workspace
```

> The published ports bind to the **loopback** by default — the address is
> the operator's choice and is not written into the compose file. A
> deployment that needs another machine to reach the stack sets
> `SANDBOX_BIND_HOST` (`0.0.0.0` publishes on every interface; then the
> bearer token is the only thing between that network and a shell).

The model should call `bash(command="pwd && ls /workspace")` and you should
see `/workspace` and the contents of the workspace.

> **Heads up:** the tool toggle is **per-chat** in Open WebUI. You have to
> enable it once per new chat. This is Open WebUI's design, not a bug.

## Production deployment

See [docs/SECURITY.md](docs/SECURITY.md) for the full hardening checklist.
The instance you are about to expose is a **single-operator** one — see
the warning at the top of this file; every user of it shares one trust
boundary.
The minimum required before exposing this on a network:

1. Fill in `.env` from `.env.example`. **Never commit `.env`.**
2. Set `OPENAPI_PROXY_TOKEN` to a 32+ char random string.
3. Set `WEBUI_SECRET_KEY` to a 64+ char random string.
4. Set `ENABLE_SIGNUP=false` and create Open WebUI users manually.
5. Set `OPENAPI_PROXY_CORS_ORIGINS` if you serve a browser UI.
6. Set `OPENAPI_PROXY_SSH_ALLOWLIST` to your actual fleet.
7. Put `/opt/agent-workspace` on a filesystem with a quota.
8. Pipe the JSON logs to your log aggregator.

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the long version.
Short version:

```
┌────────────────────────┐        ┌───────────────────────────────┐
│  Open WebUI (optional) │        │  any MCP client               │
│  :3001, tool server    │        │  llama.cpp-style, bot harness │
└───────────┬────────────┘        └──────────────┬────────────────┘
            │  HTTP  /bash, /files, …             │  MCP  POST /mcp
            │                                     │
            └────────────────┬────────────────────┘
                             ▼
        ┌────────────────────────────────────────────────────────────┐
        │  openapi-proxy   :8090       the only published surface   │
        └────────────────────────────┬───────────────────────────────┘
                                     │  HTTP, internal Docker network
                                     ▼
        ┌────────────────────────────────────────────────────────────┐
        │  sandbox-agent   :8082       internal only, bearer token   │
        │  HTTP door:  /sessions · /files · /metrics · /healthz     │
        │  MCP door:   POST /mcp → bash · read_file · file_info ·   │
        │                                    delete_file             │
        └────────────────────────────┬───────────────────────────────┘
                                     ▼
                 tmux session `agent_<id>`  ·  /workspace files
```

The agent has **no host port mapping**: everything that reaches the outside
world goes through the proxy, where the token, the CORS policy, the rate
limit and the body limit live.

## Configuration

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for the full reference.
The most important knobs:

- **`OPENAPI_PROXY_TOKEN`** (env var on `openapi-proxy` and
  `open-webui` via `TOOL_SERVER_CONNECTIONS`) — shared bearer token.
  The proxy returns 503 if this is empty — fail-closed by design.
- **`OPENAPI_PROXY_CORS_ORIGINS`** — comma-separated list of allowed
  origins for browser access. Empty = no browser cross-origin calls.
- **`OPENAPI_PROXY_SSH_ALLOWLIST`** — comma-separated hostnames or CIDRs
  the proxy will forward `ssh <host>` to. Empty = ssh shortcut disabled.
- **`OPENAPI_PROXY_RATE_LIMIT_RPM`** — per-token requests per minute.
  0 = disabled.
- **`OPENAPI_PROXY_WEBSOCKET_IDLE_TIMEOUT`** — close `/ws` connections
  idle for this many seconds (default 300). 0 = disabled. Prevents a
  forgotten browser tab from pinning a tmux session forever.
- **`AGENT_MCP_ALLOWED_HOSTS`** — comma-separated `Host` header values
  the MCP endpoint answers for. **Empty = the check is off** (the
  default, and the SDK's own default for a service that is not bound to
  the loopback: a containerised service is reached by its service name,
  and a list that does not contain that name refuses every legitimate
  caller). Set it to turn the protection against DNS rebinding on and
  to name the hosts the endpoint answers for — a request with any other
  `Host` is refused with a 421. The bearer token is the guard either
  way; this is one more layer.
- **`/workspace` mount** — bind-mount `/opt/agent-workspace` to put files
  on the host that survive container restarts.
- **`AGENT_ALLOW_SUDO`** — `0` by default. `1` builds the image with
  `sudo`, so a command in the sandbox can run `sudo su` and become root
  **inside the container**. **Private deployments in secured environments
  only** — the sandbox runs code whose input may be hostile, and this
  hands that code root. Two runtime settings have to give way as well
  (`AGENT_NO_NEW_PRIVILEGES=false`, `AGENT_CAP_DROP=SYS_ADMIN`); see
  [docs/SECURITY.md](docs/SECURITY.md#9-the-privilege-mode-sudo-su-inside-the-sandbox).
- **`AGENT_READ_ONLY`** — `true` by default: the container's root
  filesystem is read-only. A root shell in a read-only container is a
  half-root — it cannot install a package (`apt-get` needs
  `/var/lib/apt/lists`, `/var/lib/dpkg` and `/usr` to be writable).
  `false` gives the privileged deployment a root that can actually
  change something; the writable layer is the container's own and dies
  with it, so the boundary does not move.

## Development

```bash
make help        # list targets
make build       # docker compose build
make up          # start the stack
make test        # run the unit suite (no pytest required)
make lint        # ruff check
make format      # ruff format
make health      # curl every public endpoint
```

The test suite is written with stdlib `unittest`, so the **runner** needs no
pytest — `make test` runs `python3 -m unittest discover`. The suite does
need the runtime dependencies, though: it imports `agent_api`, which mounts
the MCP endpoint, so `fastapi`, `httpx` and the MCP SDK must be importable.
Install them with `pip install -r requirements-dev.txt`. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the full development workflow.

## API quick reference

### `sandbox-agent` (port 8082, no host port mapping — internal network only; bearer token required)

| Method | Path                              | Purpose                                |
|--------|-----------------------------------|----------------------------------------|
| GET    | `/healthz`                        | Liveness                               |
| GET    | `/sessions`                       | List active sessions                   |
| POST   | `/sessions`                       | Create/attach (cwd must be in workspace) |
| DELETE | `/sessions/{id}`                  | Kill a session                         |
| POST   | `/sessions/{id}/exec`             | Run a shell command                    |
| GET    | `/sessions/{id}/capture?lines=N`  | Tail tmux scrollback (no shell wrapper) |
| GET    | `/sessions/{id}/output?lines=N`   | Alias of `/capture`                    |
| GET    | `/files/meta?path=P`              | Size/mtime/name of a workspace file   |
| GET    | `/files?path=P`                   | The file's **raw bytes** (no base64)   |
| DELETE | `/files?path=P`                   | Remove a delivered file from the sandbox |
| GET    | `/metrics`                        | Prometheus exposition (bearer token)   |
| POST   | `/mcp`                            | **MCP endpoint** — the same tools in the MCP protocol |
| GET    | `/mcp`                            | The MCP event stream (Streamable HTTP) — a long-lived SSE connection. This server has no server-initiated messages to carry over it, and the SDK's own client never opens it (measured on mcp 2.3.0) |
| DELETE | `/mcp`                            | Not supported: the transport is stateless, so there is no session to terminate (405) |

### `openapi-proxy` (port 8090, **bearer auth required**)

| Method | Path             | Purpose                                                 |
|--------|------------------|---------------------------------------------------------|
| GET    | `/healthz`       | Liveness (auth required)                                |
| GET    | `/api/config`    | Open WebUI capability probe (auth required)             |
| GET    | `/openapi.json`  | OpenAPI 3.0 spec (consumed by Open WebUI)               |
| POST   | `/bash`          | Run a shell command (the `bash` tool)                   |
| POST   | `/shell_exec`    | Alias of `/bash`                                        |
| GET    | `/files/meta?path=P` | Size/mtime/name of a workspace file (streamed)       |
| GET    | `/files?path=P`  | The file's **raw bytes** (streamed, no base64)          |
| DELETE | `/files?path=P`  | Remove a delivered file from the sandbox               |
| GET    | `/metrics`       | Prometheus exposition (auth required)                   |
| WS     | `/ws` (token via `Sec-WebSocket-Protocol: bearer.<token>` header) | Experimental terminal (per-connection session) |
| POST   | `/mcp`     | Relay of the agent's MCP endpoint (same token)   |
| GET    | `/mcp`     | Refused (405): the proxy does not relay the event stream — a long-lived stream would hold a connection out of the pool for as long as the client keeps it, and there is nothing to carry over it |
| DELETE | `/mcp`     | Refused (405): the transport is stateless, there is no session to terminate |

> **Never** pass the token via `?token=...` query param — reverse proxies
> (nginx, Caddy, Cloudflare, ALB) log URLs by default and that is a token
> leak. Token is rejected if you try, see `tests/test_wave2_fixes.py`.

## Talking to it over MCP

The sandbox is also a **Model Context Protocol server**. Same sandbox,
same tools, different protocol — so a telegram harness, an Open WebUI
instance or a llama.cpp-style caller can all drive it, and none of them
needs to know that the sandbox was ever an OpenAPI service.

The endpoint is `POST /mcp` (the Streamable HTTP transport), on the
proxy (the published surface) or on the agent directly. The bearer
token is the one you already have.

```bash
# A handshake, in plain JSON-RPC. The answer names the protocol
# version the client offered and the tools on offer.
curl -sS -H "Authorization: Bearer $OPENAPI_PROXY_TOKEN" \
     -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
           "protocolVersion":"2025-06-18","capabilities":{},
           "clientInfo":{"name":"probe","version":"1"}}}' \
     http://127.0.0.1:8099/mcp
```

Four tools, one per capability — the same ones the HTTP API exposes:

| Tool          | What it does                                              |
|---------------|-----------------------------------------------------------|
| `bash`        | run a command, get stdout, the exit code and the timing   |
| `read_file`   | take a file out of the workspace (text as text, binary as a base64 blob) |
| `file_info`   | name, size, mime and mtime — without reading the file      |
| `delete_file` | remove a file from the workspace                           |

The full contract — the tool schemas, the error shapes, what the
session handling means — is in
[docs/HANDOFF-mcp.md](docs/HANDOFF-mcp.md). The short version: the
MCP door grants exactly what the HTTP door grants, and the token is
the same gate.

Four things about the MCP door differ from the HTTP door, and all four
are worth knowing before a caller is written:

- **The shell is persistent and shared.** A `bash` call runs in a tmux
  session that survives the call, so `cd` and `export` carry over. Two
  callers that use the same `session_id` share a working directory, an
  environment and a history. Give every conversation its own
  `session_id`.
- **`read_file` holds the file in memory** and base64-encodes it on the
  way out. A text file comes back as text; anything else comes back as a
  blob a third larger than the file was. One read is capped at
  **`AGENT_MCP_MAX_READ_BYTES`** — 1 MiB by default — and the effective
  limit is the smallest of that budget, the file policy the HTTP door
  applies (`AGENT_BINARY_LIMIT`) and a 64 MiB ceiling. A file over the
  limit is refused, and the refusal names the file's size and the limit.
  The HTTP door keeps its own limit untouched: there the bytes land in a
  file, not in a prompt.
- **A timeout does not stop the command.** The agent answers
  `timed_out: true` when the command outran its budget, and the
  command keeps running in the session — that is what a persistent
  session means. The command can be found and stopped through the
  session afterwards.
- **A busy session answers with a reason, not with the pane.** While an
  earlier command is still running, the next call's input waits in the
  terminal and the call runs out its timeout. The answer is an empty
  `stdout` and a `stderr` that says the command did not start because
  the session is busy. The pane still holds the command that is running
  and is never handed over as if it were the new command's output.

## Monitoring

Both services expose a Prometheus exposition at `/metrics` (bearer token
required, like every other endpoint):

| Metric                                | Service | What it counts                        |
|---------------------------------------|---------|---------------------------------------|
| `sandbox_proxy_requests_total`        | proxy   | requests handled                      |
| `sandbox_proxy_rate_limited_total`    | proxy   | requests refused with 429             |
| `sandbox_proxy_exec_timeouts_total`   | proxy   | upstream calls that ran out of time   |
| `sandbox_proxy_upstream_errors_total` | proxy   | upstream calls that failed otherwise  |
| `sandbox_agent_exec_total`            | agent   | commands executed                     |
| `sandbox_agent_exec_timeouts_total`   | agent   | commands that ran out of time         |
| `sandbox_agent_active_sessions`       | agent   | sessions held right now               |

A ready-made stack — Prometheus, Grafana and cAdvisor — is in
`docker-compose.monitoring.yml`. It is a **separate** compose project,
so the sandbox starts and works without it:

```bash
mkdir -p monitoring/secrets
grep '^OPENAPI_PROXY_TOKEN=' .env | cut -d= -f2 > monitoring/secrets/proxy_token
# Prometheus reads the file as the user it runs as — 65534 (nobody).
# A root-owned 0600 file is unreadable for it, and the scrape fails
# with "unable to read authorization credentials".
chown 65534:65534 monitoring/secrets/proxy_token
chmod 600 monitoring/secrets/proxy_token

MONITOR_NETWORK=agent-stack_agent-net \
  docker compose -f docker-compose.monitoring.yml up -d
```

Prometheus lands on `127.0.0.1:9090`, Grafana on `127.0.0.1:3002`
(admin/admin — change the password on the first login). The dashboard is
provisioned from `monitoring/grafana/dashboards/sandbox.json`.

`MONITOR_NETWORK` is the network of the stack you want to watch — find
its name with `docker network ls | grep agent-net`. Prometheus reads the
bearer token from a file, because it cannot expand variables in its
config and the token has no business being pasted into one.

## Moving files out of the sandbox

Text goes through the shell; bytes do not. A file is fetched over HTTP as
**raw bytes** — never base64, never through a terminal pane:

```bash
# What is there, and how big is it?
curl -H "Authorization: Bearer $OPENAPI_PROXY_TOKEN" \
  'http://localhost:8090/files/meta?path=report.pdf'

# Pull it. The bytes are exactly the bytes on disk.
curl -H "Authorization: Bearer $OPENAPI_PROXY_TOKEN" \
  -o report.pdf 'http://localhost:8090/files?path=report.pdf'
```

Why it is a separate door:

* **Base64 costs a third.** A 3 MB file becomes 4 MB of text, and the
  text has to fit into a terminal pane, which is bounded. The bytes do not
  have to fit in a message.
* **A pane loses the *beginning*.** When a tmux pane overflows it drops
  its oldest lines — silently. A base64 payload that overflows comes back
  as a tail that decodes to garbage, and nothing says so.
* **Streaming costs no memory.** The proxy relays the file as a stream,
  so a 200 MB file does not become 200 MB of RAM in the proxy.

The path is resolved inside the workspace, with symlinks resolved first: a
path that points outside is refused. That is hygiene, not a security
boundary — the model already has a shell.

| Knob                | Default | Meaning                                              |
|---------------------|---------|------------------------------------------------------|
| `AGENT_BINARY_LIMIT`| `2048k` | Largest file the endpoint will serve. `0` = unlimited. |
| `AGENT_STDOUT_LIMIT`| `512k`  | Largest text one command may return. 32k..1024k.       |

The endpoint is not rate limited on purpose: a per-minute request budget
would break a chunked transfer for a reason that has nothing to do with
the transfer.

### What the sandbox does *not* do

* **It does not stream a file that is not finished.** The door serves a file
  that exists, completely, on disk. A download still in flight, or a file
  still being written, has no URL here — the producer has to materialise
  it first (`curl -o out.bin … && sha256sum out.bin && stat -c%s out.bin`).
* **It is not a file host.** No sessions, no temporary storage, no quotas,
  no garbage collection. A request is a request: a path in, bytes out.
* **It is not a security boundary.** The workspace scope is hygiene; the
  model already has a shell.

### Delivering a file: the client's contract

The sandbox hands out bytes; somebody else has to deliver them. The
client (the bot) should:

1. **Pull it as a stream** — `GET /files?path=P` with `Accept-Ranges`
   honoured. The file never has to fit in the client's memory, and the
   proxy never buffers it either.
2. **Hash while reading** — the digest of the stream is free, and it is
   what makes the delivery verifiable. No server-side digest endpoint is
   needed for that.
3. **Deliver it** — a Telegram document, an Open WebUI attachment, or
   whatever the client's own channel is. After the upload, the client
   holds the channel's own id for the file (`file_id` in Telegram), and
   that id is the pointer: re-sending the file to another chat costs no
   bytes at all.
4. **Purge the sandbox copy** — `DELETE /files?path=P` removes it, so the
   artifact does not live in two places at once. The sandbox is a
   scratchpad; the delivery is the copy that counts.

```bash
# Pull it, verify it, deliver it, then drop the scratch copy.
curl -H "Authorization: Bearer $OPENAPI_PROXY_TOKEN" \
  -o report.pdf 'http://localhost:8090/files?path=report.pdf'
sha256sum report.pdf
# … upload report.pdf to the channel …
curl -fsS -X DELETE -H "Authorization: Bearer $OPENAPI_PROXY_TOKEN" \
  'http://localhost:8090/files?path=report.pdf'
```

Two limits worth knowing before sizing anything:

* `AGENT_BINARY_LIMIT` (default `2048k`) is the **sandbox's** policy. A
  file over it is refused with its real size in the message, so the
  client can decide what to do instead of discovering a truncated file.
* Telegram's `sendDocument` accepts **50 MB** through the public Bot API
  (2000 MB with a local `telegram-bot-api`). A sandbox limit above the
  channel's own limit is a limit nobody can use — raise the channel first,
  then the sandbox.

`DELETE /files` shows up in the proxy's OpenAPI spec, and Open WebUI
turns that into a tool for the model. It is a client-lifecycle call, not
a model tool — the model has a shell in the same workspace and could
`rm` the file itself — but the client is the one that should make it.

## Examples

### Use it from `curl`

```bash
# Run a one-off command in the default session
curl -X POST http://localhost:8090/bash \
  -H "Authorization: Bearer $OPENAPI_PROXY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"command":"hostname && date"}'

# Use a named session (state persists across calls)
curl -X POST http://localhost:8090/bash \
  -H "Authorization: Bearer $OPENAPI_PROXY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"command":"cd /workspace && git clone https://github.com/foo/bar", "session_id":"work-2026-10"}'

# Next call, same session_id, cwd and any env vars are still set
curl -X POST http://localhost:8090/bash \
  -H "Authorization: Bearer $OPENAPI_PROXY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"command":"cd bar && git log --oneline | head -5", "session_id":"work-2026-10"}'
```

### Use it from Python

```python
import httpx

with httpx.Client() as client:
    r = client.post(
        "http://localhost:8090/bash",
        headers={"Authorization": f"Bearer {token}"},
        json={"command": "echo hello", "session_id": "demo"},
    )
    print(r.json())
    # {'stdout': 'hello\n', 'exit_code': 0, 'duration_ms': 12, ...}
```

## For AI agents

If you (an AI agent) are asked to set this up from scratch or fix a broken
instance, do this:

### Getting a file out of the sandbox
If your task produced a file, **do not `cat` it and do not base64 it**.
Read the bytes over HTTP:

```
GET /files/meta?path=<path>   → { name, path, size, mtime, mime, limit }
GET /files?path=<path>         → the raw bytes of the file
```

`/files` supports a single `Range` header, so a large file can be pulled in
pieces. The reason not to use the shell for this: base64 costs a third of
the size, and a terminal pane that overflows drops its **oldest** lines
without saying so — the payload comes back as a tail that decodes to
garbage. See "Moving files out of the sandbox" above.

### Reading command output
`stdout` of one command is capped (`AGENT_STDOUT_LIMIT`, default 512k). If
it was capped, the payload ends with `[output truncated at N bytes]`. If
the output was **larger than the session's scrollback**, the first line
says the beginning is gone — that result is a tail, not a whole answer, and
re-running the command in smaller pieces is the fix.

### Setup from scratch
1. `git clone https://github.com/vaskes/OpenWebUI-bash-sandbox.git`
2. `cd OpenWebUI-bash-sandbox`
3. `cp .env.example .env` and fill in the secrets.
4. `docker compose up -d --build`
5. Wait for `docker compose ps` to show all three containers `(healthy)`
6. Add External Tool Server in Open WebUI: URL `http://openapi-proxy:8090`,
   path `openapi.json`, type `openapi`, auth `bearer`, key = `OPENAPI_PROXY_TOKEN`

### Diagnose a broken instance
1. `docker compose ps` — which container is unhealthy?
2. `docker logs <container> --tail 50`
3. For `openapi-proxy`: `docker exec openapi-proxy env | grep OPENAPI_PROXY_TOKEN` — must not be empty
4. For `sandbox-agent`: `docker exec sandbox-agent curl -fsS http://127.0.0.1:8082/healthz` —
   must return `{"status":"ok",...}`. The agent has **no host port**: it is reached over the
   internal network, so a `curl` from the host cannot reach it.
5. For `open-webui`: `curl http://127.0.0.1:3001/api/version` — must return a JSON version.
   The published address is the bind address (`SANDBOX_BIND_HOST`), and the default is the
   loopback.

### Fix common issues

**`openapi-proxy` env is empty** → `docker compose up -d` (not `restart`).
`docker compose restart` alone doesn't re-read env vars.

**`openapi-proxy` unhealthy** → the image installs **nothing** from the
distribution — there is no `apt-get` in it, and the healthcheck asks
Python, not `curl`. Look at `docker logs openapi-proxy --tail 50`, and
re-read the env with `docker compose up -d` (not `restart`).

**`/workspace` empty** → check that `/opt/agent-workspace` exists on the
host and is owned by UID 1000 (or `chown -R 1000:1000` it for testing).

**Open WebUI loses the tool connection on restart** → re-add via
Settings → External Tool Servers, or set `TOOL_SERVER_CONNECTIONS` in
the compose file and `docker compose up -d`.

**`bash` tool returns 401** → token mismatch. Both the proxy
(`OPENAPI_PROXY_TOKEN`) and the Open WebUI connection
(`TOOL_SERVER_CONNECTIONS[].key`) must use the same value.

**`bash` tool returns "All connection attempts failed"** → see the env-var
issue above. Almost always means `SANDBOX_URL` is unset inside the proxy.

## Troubleshooting

| Symptom                                              | Cause                                          | Fix                                                |
|------------------------------------------------------|------------------------------------------------|----------------------------------------------------|
| `bash` tool returns 401                              | Token mismatch                                 | Match `OPENAPI_PROXY_TOKEN` in proxy and webui     |
| `bash` tool returns `connection refused`             | `SANDBOX_URL` env not set in openapi-proxy     | `docker compose up -d` (not `restart`)             |
| `openapi-proxy` unhealthy                            | the healthcheck itself failed                  | `docker logs openapi-proxy --tail 50` — the image has no `apt-get`; the check asks Python |
| `sandbox-agent` unhealthy                            | tmux not installed or port 8082 conflict       | Check `docker logs sandbox-agent --tail 30`        |
| Tool not visible in chat                             | Connection not added or not enabled per-chat   | Settings → External Tool Servers + click 🔧       |
| `sandbox-agent` keeps restarting                     | Healthcheck fails — usually port in use        | `docker ps` to find the conflict                  |
| Commands run but exit code is always `None`          | Marker regex didn't match                      | Check tmux pane state, increase `timeout`         |
| WebSocket terminal hangs                             | Polling loop crashed                           | `docker logs openapi-proxy` for tracebacks         |
| "All connection attempts failed" on bash             | `SANDBOX_URL` not set inside proxy container   | See `SANDBOX_URL` env-fix above                    |
| Path traversal `cwd` rejected                        | Working as designed — that path is outside the workspace | Use a path under `/workspace`             |
| `ssh` returns "not in allowlist"                     | SSH host not in `OPENAPI_PROXY_SSH_ALLOWLIST`  | Add the host to the allowlist                      |

## Limitations

### Single trust boundary (by design, not by accident)

This stack assumes **one operator controls the Open WebUI instance and
the agents behind it**. It is **not** a multi-tenant system. Concretely:

* **One bearer token** is shared between the UI, the proxy and the agent.
  Anyone who can talk to the UI can talk to the agent with the same
  privileges.
* **One tmux server** backs all chat sessions. Users can guess each
  other's `session_id` and `tmux attach` to it.
* **No per-user rate limits.** The rate limiter keys on the bearer
  token, which is shared.

If you need multi-tenant, the right answer is **per-user containers** —
run a separate `sandbox-agent` per user, each with its own
`AGENT_TOKEN`, its own `/workspace`, its own volumes. That is
intentionally out of scope here. Trying to bolt multi-tenant auth onto
this stack would be the wrong abstraction.

### Other limitations

- **No external network isolation by default.** The container can reach
  any host the host can. Use a firewall or run inside a network namespace.
- **No resource isolation between users.** A user who runs `fork()` in
  a loop will exhaust the per-container `pids_limit` and stop everyone.
- **A command may run for an hour.** The hard cap is 3600 seconds, and
  the relay waits an hour and a minute for the answer. A restart of the
  stack during a long command cuts it off: the container stops
  gracefully for 25 seconds, and the caller sees a dropped connection
  rather than a timeout. See
  [docs/CONFIGURATION.md](docs/CONFIGURATION.md#an-hour-long-command-is-an-operational-event).

## Credits

- **Developer** — Mavis Agent (powered by MiniMax M3)
- **Code reviews** — Qwen 3.8-Max and Mavis (self-review, multi-wave)
- **Vibecoding, network infrastructure, human testing, deployment & integration** — Vaskes

See [CREDITS.md](CREDITS.md) for the full list.

## License

MIT — see [LICENSE](LICENSE).
