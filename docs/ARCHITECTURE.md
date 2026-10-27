# Architecture

The sandbox is **two services** — `sandbox-agent` and `openapi-proxy` — plus
an optional upstream (`open-webui`) that is *not* built from this repository.
The proxy is the only published surface; the agent is reachable only from
inside the Docker network.

Either door can be used by any client: the plain HTTP API (what Open WebUI's
tool server speaks) or the MCP endpoint (what a telegram harness or a
llama.cpp-style client speaks). Both doors open the same sandbox.

## High-level diagram

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
        │  relays both doors to the agent; the caller's              │
        │  Authorization header is never forwarded upstream           │
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

## Two doors, one sandbox

The HTTP API and the MCP endpoint are not two implementations of the sandbox.
They are two serialisations of the same four capabilities:

| | HTTP | MCP |
|---|---|---|
| surface | `/bash`, `/sessions/{id}/exec`, `/files*` | `POST /mcp` |
| auth | the same bearer token | the same bearer token |
| comparison | constant-time, shared helper | the same shared helper |
| refusals | HTTP status + a named reason | JSON-RPC result with `isError = true` and the reason in the tool text |
| tools | — | the four tools named in the next paragraph |

Rules that keep the two doors honest:

* **One token.** The MCP door uses the same token and the same comparison
  function as every HTTP route. There is no second credential and no SDK
  OAuth layer.
* **The route is exact.** `/mcp` is registered as an exact route, not a
  mounted sub-app: a mount answers `/mcp` with a `307` to `/mcp/`, and some
  clients do not follow POST redirects — the handshake then simply never
  happens.
* **Stateless transport.** The MCP app runs with `json_response=True` and
  `stateless_http=True`, so no session affinity is needed behind the proxy.
  A client may reconnect at any time.
* **The lifespan lives in the agent.** The MCP app's `session_manager.run()`
  is entered from the agent's own lifespan — a mounted sub-app receives no
  lifespan of its own, and without that the endpoint answers
  «Task group is not initialized».
* **The proxy forwards the caller's `Host`/`Origin` upstream**, so the agent
  checks the host the caller actually used. It calls the agent with its own
  upstream token; the caller's `Authorization` header never leaves the proxy.
* **DNS-rebinding protection is opt-in.** `AGENT_MCP_ALLOWED_HOSTS` names
  the hosts the endpoint answers for; empty (the default) means the check is
  off, because a containerised service is reached by its service name and a
  list that lacks that name refuses every legitimate caller. A request with
  an unlisted `Host` is refused with `421`. The bearer token is the guard
  either way.
* **The protocol version offered is `2025-06-18`.** The server answers with
  the version the client offered.
* **The budgets differ on purpose.** The two doors grant the same
  *capabilities*, but they are not the same question. The HTTP door serves
  a file — the bytes land in a file on the caller's disk, and the
  deployment runs that limit at 50 MiB. The MCP door puts the bytes into a
  model's context, where they cost attention and memory, so its limit is
  `AGENT_MCP_MAX_READ_BYTES` (1 MiB by default) and never above the file
  policy or the 64 MiB ceiling. A model-facing budget that were the HTTP
  door's would make one tool call cost 50 MiB of prompt.

The MCP door exposes four tools: `bash`, `read_file`, `file_info`, `delete_file`.

## Component responsibilities

### 1. `sandbox-agent` (`agent_api.py` + `mcp_server.py`)

The only component that actually executes shell commands and touches files.
It owns:

* a per-`session_id` tmux session (one shell per chat),
* a small in-process registry of `session_id → {tmux_name, cwd, created}`,
* the workspace files,
* the HTTP API and the MCP server that the proxy calls.

It does **not** know anything about Open WebUI, OpenAPI or MCP
clients. It can be used standalone by any client that needs a persistent
sandbox.

| Method | Path | Purpose |
|--------|------|---------|
| GET    | `/healthz` | Liveness (`{"status": "ok"}`) |
| GET    | `/metrics` | Prometheus exposition (bearer token) |
| GET    | `/sessions` | List active sessions |
| POST   | `/sessions` | Create or attach to a session |
| DELETE | `/sessions/{id}` | Kill a session |
| POST   | `/sessions/{id}/exec` | Run a shell command |
| GET    | `/sessions/{id}/capture?lines=N` | Tail the tmux scrollback buffer |
| GET    | `/sessions/{id}/output?lines=N` | Alias of `/capture` |
| GET    | `/files/meta?path=P` | Size/mtime/name of a workspace file |
| GET    | `/files?path=P` | The file's **raw bytes** (no base64) |
| DELETE | `/files?path=P` | Remove a delivered file from the sandbox |
| POST   | `/mcp` | The MCP endpoint (Streamable HTTP) |
| GET    | `/mcp` | The MCP event stream |
| DELETE | `/mcp` | Terminate an MCP session |

### 2. `openapi-proxy` (`openapi_proxy.py`)

A thin relay that turns the sandbox-agent API into the OpenAPI 3.0 tool
surface Open WebUI discovers automatically, and that relays the MCP door to
the same agent. It also:

* recognises the `ssh <rest>` pseudo-command and routes it to the
  sandbox, and forwards every other command verbatim,
* serves `/openapi.json` so Open WebUI can introspect the tool surface,
* relays `/mcp` with its own upstream token,
* exposes an experimental `/ws` WebSocket terminal.

| Method | Path | Purpose |
|--------|------|---------|
| GET    | `/healthz` | Liveness (auth required) |
| GET    | `/metrics` | Prometheus exposition (auth required) |
| GET    | `/api/config` | Open WebUI capability probe (auth required) |
| GET    | `/openapi.json` | OpenAPI 3.0 spec (consumed by Open WebUI) |
| POST   | `/bash` | Run a shell command (the `bash` tool) |
| POST   | `/shell_exec` | Alias of `/bash` |
| GET    | `/files/meta?path=P` | Size/mtime/name of a workspace file |
| GET    | `/files?path=P` | The file's **raw bytes** (streamed, no base64) |
| DELETE | `/files?path=P` | Remove a delivered file from the sandbox |
| POST   | `/mcp` | Relay of the agent's MCP endpoint |
| GET    | `/mcp` | Relay of the MCP event stream |
| DELETE | `/mcp` | Relay of the MCP session termination |
| WS     | `/ws` | Experimental terminal (per-connection session) |

### 3. `open-webui` (upstream image, optional)

The standard Open WebUI Docker image, with two customisations:

* `TOOL_SERVER_CONNECTIONS` env var pre-wires the connection to
  `openapi-proxy`,
* `SANDBOX_URL` env var tells Open WebUI where the local proxy is.

It appears in `docker-compose.yml` and in **no** other stack: the production
compose runs the agent and the proxy only, and the CI stack runs the agent
and the proxy only.

## Why a separate agent and proxy

* **Independent scaling.** `sandbox-agent` is the only one that does real
  work; you can run it on a beefy host and put the other anywhere.
* **Independent failure modes.** If the Open WebUI upgrade breaks tool
  servers, the sandbox is untouched and you keep using it with curl.
* **One published surface.** The agent has no host port mapping at all.
  Everything that reaches the outside world goes through the proxy, which is
  where the token, the CORS policy, the rate limit and the body limit live.
* **Testability.** `openapi-proxy` can be pointed at any sandbox-agent
  instance in tests, and the sandbox-agent can be exercised directly
  without standing up Open WebUI.

## Data flow for a single tool call

### Open WebUI, over HTTP

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

### An MCP client, over MCP

```
MCP client (llama.cpp-style, telegram harness)
   │
   ▼
POST http://openapi-proxy:8090/mcp
   Authorization: Bearer <token>
   Accept: application/json, text/event-stream
   │
   ▼
openapi-proxy relays the JSON-RPC to the agent
   — the caller's Host/Origin go upstream, the caller's token does not
   │
   ▼
sandbox-agent answers with the handshake (protocol 2025-06-18),
then the client asks for tools/list and calls one
   │
   ▼
the tool runs in the tmux session; a refusal comes back as a successful
JSON-RPC response with isError = true and the reason in the tool text
```

## Module layout

```
agent_api.py          # FastAPI app — HTTP endpoints and tmux plumbing
agent_config.py       # Environment parsing: stdout, binary, history limits
agent_core.py         # Pure functions: safe_id, tmux_name, path checks
body_limit.py         # Pure-ASGI body-size limiter, shared by both services
json_logging.py       # JSON log formatting for the app and uvicorn
mcp_server.py         # MCP server: the four tools, refusals, protocol
metrics.py            # Prometheus metrics
openapi_proxy.py      # FastAPI app — OpenAPI surface, relay, WS, /mcp relay
proxy_core.py         # Pure constants: command prefixes, timeouts, WS tunables
tests/                # unittest suite, run by pytest in CI
docker-compose.yml    # dev stack: agent + proxy + open-webui
docker-compose.production.yml   # production stack: agent + proxy
docker-compose.ci.yml           # CI stack: agent + proxy
docker-compose.monitoring.yml   # Prometheus, Grafana, cAdvisor
Dockerfile.*          # Per-service image
agent-stack.service   # systemd unit for autostart
```

Pure functions live in `*_core.py` so they can be unit-tested without
importing FastAPI/Pydantic/httpx.

## Persistent state

| What | Where | Survives `docker compose restart`? |
|------|-------|-----------------------------------|
| `/workspace` files | bind-mount (the host path is the operator's choice) | yes (host filesystem) |
| apt download cache | named volume, `/var/cache/apt` | yes (the cache only — packages themselves land in `/usr`, which is **not** a volume) |
| pip cache | named volume, `/home/agent/.cache/pip` | yes |
| the code | baked into the image | yes — a new container starts from the image again |
| tmux sessions | `/tmp` inside the container | no — but auto-recreate on first use |
| MCP sessions | none: the transport is stateless | n/a |
| the bearer token | environment variable, from the operator's `.env` | yes (re-read on `up -d`) |

A volume mounted **over** the code directory freezes the release: Docker
serves the named volume's copy and stops seeing the image's files. Mount
under a path (`/home/agent/.cache/pip`), never over the code.

## Limits

| Limit | Default | Where |
|-------|---------|-------|
| request body | 2 MiB, counted as the bytes arrive (chunked bodies included) | both services |
| text returned by one command | `AGENT_STDOUT_LIMIT`, 512 KB | agent |
| size of a file served by `/files` | `AGENT_BINARY_LIMIT`, 2 MiB (`0` = unlimited) | agent |
| size of a file read by the MCP `read_file` tool | the smallest of `AGENT_MCP_MAX_READ_BYTES` (1 MiB), `AGENT_BINARY_LIMIT` and 64 MiB | agent |
| one command's runtime | `AGENT_EXEC_TIMEOUT`, 60 s default (5..3600); the agent's own request default is 30 s and its hard cap 3600 s | the proxy asks, the agent runs |
| the MCP relay's patience | 3660 s — longer than the agent's own 3600 s cap | proxy |

The limits chain: the smallest link wins. A limit the channel cannot carry
is a limit nobody can use.

## Failure modes

* **sandbox-agent down** → the `bash` door answers `200` with a body of
  `{"error": "sandbox-agent request failed: ..."}` — the LLM sees it and can
  decide what to do. The file doors and the MCP relay answer `502`.
* **openapi-proxy down** → the published surface is gone; the agent is still
  reachable from inside the Docker network.
* **open-webui down** → nothing else is affected; the sandbox can be used
  with curl, or over MCP, directly.
* **tmux session lost** (container restart) → next call to that `session_id`
  auto-creates a fresh shell. State is **not** preserved across container
  restarts by design; mount `/workspace` for persistent files.
* **body over the limit** → `413`, on either door, before the request is
  parsed. The limit counts bytes as they arrive, so a chunked body cannot
  slip past it.
* **token missing or wrong** → `401`, on either door.
* **a file the agent may not read** → `403`, with the reason in the body —
  never a `200` with an empty body.
* **the path was replaced while it was being opened** → `409`, with the
  reason in the body. The file door opens the vetted path *relative to a
  descriptor of the workspace*, one component at a time, and every
  component with `O_NOFOLLOW`: a directory (or the file itself) that
  turned into a symlink after the containment check fails the open
  instead of being followed out of the workspace.
* **MCP tool refused** → a successful JSON-RPC response with `isError = true`
  and the reason in the tool text, so the model can read it and the operator
  gets a traceback in the log.
* **`Host` not in `AGENT_MCP_ALLOWED_HOSTS`** → `421`. The check is off
  unless the variable is set.
* **the session is busy** (an earlier command is still running) → the call
  runs out its timeout and answers with an empty `stdout` and a `stderr`
  that names the reason. The pane still holds the command that is running
  and is never handed to the caller as if it were the new command's
  output.
