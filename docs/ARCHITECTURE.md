# Architecture

OpenWebUI-bash-sandbox is three loosely-coupled HTTP services plus a shared
Docker network. Each service has a single responsibility and a stable wire
contract; nothing requires you to run all three.

## High-level diagram

```
┌─────────────────┐    HTTP     ┌─────────────────┐    HTTP     ┌──────────────────┐
│   Open WebUI    │ ──────────▶ │  openapi-proxy  │ ──────────▶ │  sandbox-agent   │
│   (port 3001)   │             │   (port 8090)   │             │   (port 8082)    │
└─────────────────┘             └─────────────────┘             └──────────────────┘
        ▲                                                                  │
        │                                                                  ▼
        │            WebSocket                                       ┌────────────┐
        └───────────  /ws  ──────────────────────────────────────────│  tmux      │
                    (experimental)                                  │  agent_<id>│
                                                                    └────────────┘
```

## Component responsibilities

### 1. `sandbox-agent` (`agent_api.py`)

The only component that actually executes shell commands. It owns:

* a per-`session_id` tmux session (one shell per chat),
* a small in-process registry of `session_id → {tmux_name, cwd, created}`,
* the HTTP API the proxy calls.

It does **not** know anything about Open WebUI, OpenAPI, or SearXNG. It can
be used standalone by any HTTP client that needs a persistent sandbox.

Endpoints:

| Method | Path                              | Purpose                              |
|--------|-----------------------------------|--------------------------------------|
| GET    | `/healthz`                        | Liveness + session count             |
| GET    | `/sessions`                       | List active sessions                 |
| POST   | `/sessions`                       | Create or attach to a session        |
| DELETE | `/sessions/{id}`                  | Kill a session                       |
| POST   | `/sessions/{id}/exec`             | Run a shell command                  |
| GET    | `/sessions/{id}/output?lines=N`   | Tail the tmux scrollback buffer      |

### 2. `openapi-proxy` (`openapi_proxy.py`)

A thin wrapper that converts the sandbox-agent API into a single OpenAPI
3.0 tool named `bash` that Open WebUI can discover automatically. It also:

* recognises the `web_search <q>` and `ssh <rest>` pseudo-commands and
  routes them to the right backend (SearXNG and the sandbox respectively),
* serves `/openapi.json` so Open WebUI can introspect the tool surface,
* exposes an experimental `/ws` WebSocket terminal.

### 3. `open-webui` (upstream image)

The standard Open WebUI Docker image, with two customisations:

* `TOOL_SERVER_CONNECTIONS` env var pre-wires the connection to
  `openapi-proxy`,
* `SANDBOX_URL` and `SEARXNG_URL` env vars tell Open WebUI where the local
  services are.

## Why three services, not one

* **Independent scaling.** `sandbox-agent` is the only one that does real
  work; you can run it on a beefy host and put the others anywhere.
* **Independent failure modes.** If the Open WebUI upgrade breaks tool
  servers, the sandbox is untouched and you keep using it with curl.
* **Testability.** `openapi-proxy` can be pointed at any sandbox-agent
  instance in tests, and the sandbox-agent can be exercised directly
  without standing up Open WebUI.

## Data flow for a single tool call

```
User types "ls /workspace" in Open WebUI
   │
   ▼
Open WebUI calls the bash tool defined in TOOL_SERVER_CONNECTIONS
   │
   ▼
POST http://openapi-proxy:8090/bash
   { "command": "ls /workspace", "session_id": "<chat-id>" }
   │
   ▼
openapi-proxy forwards to
   POST http://sandbox-agent:8082/sessions/<chat-id>/exec
   { "command": "ls /workspace", "timeout": 60 }
   │
   ▼
sandbox-agent writes the command into the tmux session,
then polls `tmux capture-pane` until a unique __AGENT_DONE_<hex>__ marker
appears in the captured output
   │
   ▼
sandbox-agent returns { stdout, exit_code, duration_ms }
   │
   ▼
openapi-proxy returns the same JSON to Open WebUI
   │
   ▼
Open WebUI appends stdout to the chat and asks the model for a final answer
```

## Module layout

```
agent_api.py        # FastAPI app — endpoints and tmux plumbing
agent_core.py       # Pure functions: safe_id, tmux_name, constants
openapi_proxy.py    # FastAPI app — OpenAPI surface + WS terminal
proxy_core.py       # Pure constants: command prefixes, timeouts, WebSocket tunables
tests/              # stdlib-unittest suite, no pytest required
docker-compose.yml  # Production-shaped stack
Dockerfile.*        # Per-service image
agent-stack.service # systemd unit for autostart
```

Pure functions live in `*_core.py` so they can be unit-tested without
importing FastAPI/Pydantic/httpx.

## Persistent state

| What                | Where                                       | Survives `docker compose restart`? |
|---------------------|---------------------------------------------|------------------------------------|
| `/workspace` files  | bind-mount `/opt/agent-workspace`           | yes (host filesystem)              |
| installed apt pkgs  | named volume `sandbox-agent-apt`            | yes                                |
| installed pip pkgs  | named volume `sandbox-agent-pip`            | yes                                |
| `~/.bashrc`, configs| named volume `sandbox-agent-home` (=/home/agent) | yes                            |
| tmux sessions       | `/tmp` inside container                     | no — but auto-recreate on first use|
| Open WebUI DB       | named volume `open-webui-data`              | yes (NOT bind-mount — wave 3 #C1)  |
| `TOOL_SERVER_CONNECTIONS` | env var in compose                     | yes (re-read on `up -d`)           |

## Failure modes

* **sandbox-agent down** → `openapi-proxy` returns 502-like JSON
  (`{"error": "sandbox-agent request failed: ..."}`). The LLM sees it and
  can decide what to do.
* **openapi-proxy down** → Open WebUI's tool toggle shows the connection
  offline; chat works without the tool.
* **open-webui down** → nothing else is affected; the sandbox can be used
  with curl directly.
* **tmux session lost** (container restart) → next call to that `session_id`
  auto-creates a fresh shell. State is **not** preserved across container
  restarts by design; mount `/workspace` for persistent files.
