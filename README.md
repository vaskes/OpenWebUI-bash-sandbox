# OpenWebUI-bash-sandbox

> Persistent bash sandbox for [Open WebUI](https://github.com/open-webui/open-webui) via
> built-in External Tool Servers. One `bash` tool, persistent state, no
> middleware patching, no model reconfiguration.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Open WebUI: compatible](https://img.shields.io/badge/Open_WebUI-compatible-blue)](https://github.com/open-webui/open-webui)
[![Docker Compose: v2](https://img.shields.io/badge/Docker_Compose-v2-2496ED)](https://docs.docker.com/compose/)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)](https://www.python.org/)
[![Tests: 129](https://img.shields.io/badge/tests-129_passing-brightgreen)](tests/)

---

## Why this exists

Open WebUI has first-class support for **External Tool Servers** that speak
OpenAPI 3.0. Instead of patching Open WebUI or writing a custom
integration, you drop in a single HTTP service that exposes a `bash` tool.
The LLM calls it, gets back a shell transcript, and continues — with
**persistent state across calls** in the same chat (cwd, env vars, history,
running processes).

This repo is the minimal, production-shaped packaging of that pattern:
three containers, one OpenAPI endpoint, zero patches.

```
User ──▶ Open WebUI ──▶ openapi-proxy (OpenAPI 3.0, bearer auth) ──▶ sandbox-agent (UID 1000, r/o fs) ──▶ tmux ──▶ bash
```

## Features

- ✅ **One `bash` tool** — runs any shell command in a persistent sandbox
- ✅ **Persistent state** — cwd, env vars, history survive across LLM calls
- ✅ **Auto-wire** — `TOOL_SERVER_CONNECTIONS` env var pre-loads the tool
- ✅ **Zero patches** — uses Open WebUI's built-in External Tool Servers
- ✅ **Bearer-token auth** on the proxy (required, constant-time compare)
- ✅ **CORS allowlist** (opt-in, off by default)
- ✅ **Rate limiting** (token bucket, configurable rpm)
- ✅ **SSH host allowlist** (CIDR + hostname, empty = disabled)
- ✅ **Per-WebSocket session** — no cross-user output leak
- ✅ **Singleton HTTP client** — no per-request TCP handshake
- ✅ **`web_search <q>` shortcut** — proxied to SearXNG
- ✅ **systemd unit** for autostart on boot
- ✅ **Multi-stage Dockerfiles** — small images
- ✅ **129 unit + integration tests** (stdlib `unittest`, no pytest required)
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

The model should call `bash(command="pwd && ls /workspace")` and you should
see `/workspace` and the contents of the workspace.

> **Heads up:** the tool toggle is **per-chat** in Open WebUI. You have to
> enable it once per new chat. This is Open WebUI's design, not a bug.

## Production deployment

See [docs/SECURITY.md](docs/SECURITY.md) for the full hardening checklist.
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
┌─────────────────┐    HTTP     ┌─────────────────┐    HTTP     ┌──────────────────┐
│   Open WebUI    │ ──────────▶ │  openapi-proxy  │ ──────────▶ │  sandbox-agent   │
│   :3001         │             │   :8090         │             │   :8082          │
└─────────────────┘             └─────────────────┘             └──────────────────┘
        ▲                                                                  │
        │            WebSocket                                       tmux  │
        └───────────  /ws (per-conn session)  ────────────────────── agent_<id>
```

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
- **`/workspace` mount** — bind-mount `/opt/agent-workspace` to put files
  on the host that survive container restarts.

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

The test suite uses stdlib `unittest` so it runs without installing
pytest, fastapi, or httpx. See [CONTRIBUTING.md](CONTRIBUTING.md) for
the full development workflow.

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

### `openapi-proxy` (port 8090, **bearer auth required**)

| Method | Path             | Purpose                                                 |
|--------|------------------|---------------------------------------------------------|
| GET    | `/healthz`       | Liveness (auth required)                                |
| GET    | `/api/config`    | Open WebUI capability probe (auth required)             |
| GET    | `/openapi.json`  | OpenAPI 3.0 spec (consumed by Open WebUI)               |
| POST   | `/bash`          | Run a shell command (the `bash` tool)                   |
| POST   | `/shell_exec`    | Alias of `/bash`                                        |
| WS     | `/ws` (token via `Sec-WebSocket-Protocol: bearer.<token>` header) | Experimental terminal (per-connection session) |

> **Never** pass the token via `?token=...` query param — reverse proxies
> (nginx, Caddy, Cloudflare, ALB) log URLs by default and that is a token
> leak. Token is rejected if you try, see `tests/test_wave2_fixes.py`.

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

# Web search shortcut (proxied to SearXNG)
curl -X POST http://localhost:8090/bash \
  -H "Authorization: Bearer $OPENAPI_PROXY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"command":"web_search latest cve docker"}'
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
4. For `sandbox-agent`: `curl http://localhost:8082/healthz` — must return `{"status":"ok",...}`
5. For `open-webui`: `curl http://localhost:3001/api/version` — must return a JSON version

### Fix common issues

**`openapi-proxy` env is empty** → `docker compose up -d` (not `restart`).
`docker compose restart` alone doesn't re-read env vars.

**`curl` missing in `openapi-proxy`** → check `Dockerfile.openapi-proxy`
has `apt-get install curl`, then
`docker compose build openapi-proxy && docker compose up -d openapi-proxy`.

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
| `openapi-proxy` unhealthy                            | `curl` missing in image                        | Add `apt-get install curl` to Dockerfile, rebuild  |
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

## Credits

- **Developer** — Mavis Agent (powered by MiniMax M3)
- **Code reviews** — Qwen 3.8-Max and Mavis (self-review, multi-wave)
- **Vibecoding, network infrastructure, human testing, deployment & integration** — Vaskes

See [CREDITS.md](CREDITS.md) for the full list.

## License

MIT — see [LICENSE](LICENSE).
