# OpenWebUI-bash-sandbox

> Persistent bash sandbox for [Open WebUI](https://github.com/open-webui/open-webui) via
> built-in External Tool Servers. One `bash` tool, persistent state, no
> middleware patching, no model reconfiguration.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Open WebUI: compatible](https://img.shields.io/badge/Open_WebUI-compatible-blue)](https://github.com/open-webui/open-webui)
[![Docker Compose: v2](https://img.shields.io/badge/Docker_Compose-v2-2496ED)](https://docs.docker.com/compose/)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)](https://www.python.org/)

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
User ──▶ Open WebUI ──▶ openapi-proxy (OpenAPI 3.0) ──▶ sandbox-agent ──▶ tmux ──▶ bash
```

## Features

- ✅ **One `bash` tool** — runs any shell command in a persistent sandbox
- ✅ **Persistent state** — cwd, env vars, history survive across LLM calls in the same chat
- ✅ **Auto-wire** — `TOOL_SERVER_CONNECTIONS` env var pre-loads the tool on every container start
- ✅ **Zero patches** — uses Open WebUI's built-in External Tool Servers
- ✅ **`web_search <q>` shortcut** — proxied to SearXNG
- ✅ **`ssh <rest>` shortcut** — auto-injects `StrictHostKeyChecking=accept-new`
- ✅ **Experimental WebSocket terminal** at `/ws`
- ✅ **systemd unit** for autostart on boot
- ✅ **Comprehensive unit tests** (no pytest required, stdlib only)
- ✅ **Pinned dependencies** in `requirements.txt`

## Quick start (60 seconds)

```bash
git clone https://github.com/vaskes/OpenWebUI-bash-sandbox.git
cd OpenWebUI-bash-sandbox
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

## What's in the box

| Path                      | What it is                                              |
|---------------------------|---------------------------------------------------------|
| `docker-compose.yml`      | All three services + named volumes + networks           |
| `Dockerfile.sandbox`      | ubuntu:24.04 + tmux + python + agent HTTP API           |
| `Dockerfile.openapi-proxy`| python:3.11-slim + FastAPI + OpenAPI spec               |
| `agent_api.py`            | FastAPI app — tmux session lifecycle + command execution|
| `agent_core.py`           | Pure functions — `safe_id`, `tmux_name`, constants      |
| `openapi_proxy.py`        | FastAPI app — OpenAPI surface + WebSocket terminal      |
| `proxy_core.py`           | Pure constants — command prefixes, timeouts             |
| `agent-stack.service`     | systemd unit for autostart on boot                      |
| `requirements.txt`        | Pinned runtime dependencies                             |
| `requirements-dev.txt`    | + pytest, ruff for development                          |
| `Makefile`                | `make build`, `make up`, `make test`, `make lint`       |
| `tests/`                  | Unit tests (stdlib `unittest`, no pytest required)      |
| `docs/ARCHITECTURE.md`    | Component diagram + data flow                           |
| `docs/CONFIGURATION.md`   | Every env var, port, volume documented                  |
| `docs/SECURITY.md`        | Threat model + hardening checklist                      |
| `CHANGELOG.md`            | Release history                                         |
| `LICENSE`                 | MIT                                                     |

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
        └───────────  /ws  ────────────────────────────────────────── agent_<id>
```

## Configuration

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for the full reference.
The most common knobs:

- **`SANDBOX_URL`** (env var on `openapi-proxy`) — where to find
  `sandbox-agent`. Default `http://sandbox-agent:8082`. **Must match the
  docker-compose service name** when running inside the stack.
- **`SEARXNG_URL`** (env var on `openapi-proxy` and `open-webui`) — where
  to find your SearXNG instance. Default `http://192.168.10.7:8888`.
- **`/workspace` mount** — bind-mount `/opt/agent-workspace` to put files
  on the host that survive container restarts.
- **Persistent volumes** — `sandbox-agent-home`, `sandbox-agent-apt`,
  `sandbox-agent-pip` survive `docker compose restart` and skip re-downloads.

## Production autostart (systemd)

```bash
sudo cp agent-stack.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agent-stack.service
```

`agent-stack.service` runs `docker compose up -d` from the directory the
service file is configured to. Edit the `WorkingDirectory=` line to match
your clone path.

> **Important:** `docker compose restart` does **not** re-read env vars
> from the compose file. If you change an env var, run `docker compose up -d`
> (which the systemd unit does at boot).

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
pytest, fastapi, or httpx. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
full development workflow.

## API quick reference

### `sandbox-agent` (port 8082)

| Method | Path                              | Purpose                       |
|--------|-----------------------------------|-------------------------------|
| GET    | `/healthz`                        | Liveness                      |
| GET    | `/sessions`                       | List active sessions          |
| POST   | `/sessions`                       | Create or attach to a session |
| DELETE | `/sessions/{id}`                  | Kill a session                |
| POST   | `/sessions/{id}/exec`             | Run a shell command           |
| GET    | `/sessions/{id}/output?lines=N`   | Tail the tmux scrollback      |

### `openapi-proxy` (port 8090)

| Method | Path             | Purpose                                                 |
|--------|------------------|---------------------------------------------------------|
| GET    | `/healthz`       | Liveness                                                |
| GET    | `/api/config`    | Open WebUI capability probe                             |
| GET    | `/openapi.json`  | OpenAPI 3.0 spec (consumed by Open WebUI)               |
| POST   | `/bash`          | Run a shell command (the `bash` tool)                   |
| POST   | `/shell_exec`    | Alias of `/bash` for older Open WebUI versions          |
| WS     | `/ws`            | Experimental terminal                                   |

## Examples

### Use it from `curl`

```bash
# Run a one-off command in the default session
curl -X POST http://localhost:8090/bash \
  -H "Content-Type: application/json" \
  -d '{"command":"hostname && date"}'

# Use a named session (state persists)
curl -X POST http://localhost:8090/bash \
  -H "Content-Type: application/json" \
  -d '{"command":"cd /workspace && git clone https://github.com/foo/bar", "session_id":"work-2026-10"}'

# Next call, same session_id, cwd and any env vars are still set
curl -X POST http://localhost:8090/bash \
  -H "Content-Type: application/json" \
  -d '{"command":"cd bar && git log --oneline | head -5", "session_id":"work-2026-10"}'
```

### Use it from Open WebUI

Just type:

```
Install tree and show me a directory listing of /workspace
```

The model will call `bash("apt-get install -y tree && tree /workspace")`
and the result appears in the chat.

### Use it from Python

```python
import httpx
r = httpx.post("http://localhost:8090/bash",
               json={"command": "echo hello", "session_id": "demo"})
print(r.json())
# {'stdout': 'hello\n', 'exit_code': 0, 'duration_ms': 12, ...}
```

## For AI agents

If you (an AI agent) are asked to set this up from scratch or fix a broken
instance, do this:

### Setup from scratch
1. `git clone https://github.com/vaskes/OpenWebUI-bash-sandbox.git`
2. `cd OpenWebUI-bash-sandbox`
3. `docker compose up -d --build`
4. Wait for `docker compose ps` to show all three containers `(healthy)`
5. Add External Tool Server in Open WebUI: URL `http://openapi-proxy:8090`, path `openapi.json`

### Diagnose a broken instance
1. `docker compose ps` — which container is unhealthy?
2. `docker logs <container> --tail 50`
3. For `openapi-proxy`: `docker exec openapi-proxy env | grep SANDBOX` — must show `SANDBOX_URL=http://sandbox-agent:8082`
4. For `sandbox-agent`: `curl http://localhost:8082/healthz` — must return `{"status":"ok",...}`
5. For `open-webui`: `curl http://localhost:3001/api/version` — must return a JSON version

### Fix common issues

**`openapi-proxy` env is empty** → `sudo systemctl restart agent-stack.service` (or `docker compose up -d`).
`docker compose restart` alone doesn't re-read env vars.

**`curl` missing in `openapi-proxy`** → check `Dockerfile.openapi-proxy` has `apt-get install curl`, then
`docker compose build openapi-proxy && docker compose up -d openapi-proxy`.

**`/workspace` empty** → check that `/opt/agent-workspace` exists on the host and is owned by a
user the agent can read (or just `chmod 777` it for testing).

**Open WebUI loses the tool connection on restart** → re-add via Settings → External Tool Servers,
or set `TOOL_SERVER_CONNECTIONS` in the compose file and `docker compose up -d`.

**`bash` tool returns "All connection attempts failed"** → see the env-var issue above. Almost
always means `SANDBOX_URL` is unset inside the proxy container.

## Troubleshooting

| Symptom                                              | Cause                                          | Fix                                                |
|------------------------------------------------------|------------------------------------------------|----------------------------------------------------|
| `bash` tool returns `connection refused`             | `SANDBOX_URL` env not set in openapi-proxy     | `docker compose up -d` (not `restart`)             |
| `openapi-proxy` unhealthy                            | `curl` missing in image                        | Add `apt-get install curl` to Dockerfile, rebuild  |
| `sandbox-agent` unhealthy                            | tmux not installed or port 8082 conflict       | Check `docker logs sandbox-agent --tail 30`        |
| Tool not visible in chat                             | Connection not added or not enabled per-chat   | Settings → External Tool Servers + click 🔧       |
| `sandbox-agent` keeps restarting                     | Healthcheck fails — usually port in use        | `docker ps` to find the conflict                  |
| Commands run but exit code is always `None`           | Marker regex didn't match                      | Check tmux pane state, increase `timeout`         |
| WebSocket terminal hangs                             | Polling loop crashed                           | `docker logs openapi-proxy` for tracebacks         |

## Limitations

- **No user-level isolation.** All users share the same `sandbox-agent`
  container, which means all users share the same tmux server. Open WebUI
  generates unique `session_id`s per chat, but a determined user could
  guess another user's `session_id` and `tmux attach` to it. See
  [docs/SECURITY.md](docs/SECURITY.md).
- **No resource limits by default.** Add `mem_limit` and `cpus` to the
  `sandbox-agent` service in compose for production.
- **No external network isolation.** The container can reach any host
  the host can. Use a firewall or run inside a network namespace.

## License

MIT — see [LICENSE](LICENSE).
