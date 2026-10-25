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

If `AGENT_TOKEN` is empty, the agent refuses to serve any request
except `/healthz` (returns 503). This is fail-closed by design.

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
| `/home/agent`       | the image itself (the code lives there)  | yes, but **never** mount a volume here |
| `/var/cache/apt`    | named volume `sandbox-agent-apt`        | yes       |
| `/home/agent/.cache/pip`  | named volume `sandbox-agent-pip` | yes       |
| `/opt/sandbox/`     | baked into image                        | no        |
| `/opt/search`       | bind-mount `/opt/search` (read-only)    | no        |
| `/home/agent/.ssh`  | bind-mount `/opt/agent-workspace/.ssh` (read-only) | no |
| `/tmp`              | container tmpfs                         | yes (ephemeral) |

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
| `OPENAPI_PROXY_PORT`                   | `8090`                           | uvicorn listen port.                                                    |
| `SANDBOX_URL`                          | `http://127.0.0.1:8082`          | Upstream sandbox-agent URL.                                             |
| `SEARXNG_URL`                          | `http://127.0.0.1:8888`          | SearXNG instance used by `web_search`.                                  |

`SANDBOX_URL` is **critical**: if it's empty or wrong, every `bash` call
returns `{"error": "sandbox-agent request failed: ..."}`. After changing it
in `docker-compose.yml` you must run `docker compose up -d` (not
`docker compose restart` — that doesn't re-read env).

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
| `ENABLE_WEB_SEARCH`           | `true`               | Use SearXNG for built-in web search               |
| `WEB_SEARCH_ENGINE`           | `searxng`            | Backend                                          |
| `SEARXNG_URL`                 | `http://192.168.10.7:8888` | Open WebUI's own web-search instance          |
| `ENABLE_AUDIO_STT`            | `true`               | Allow speech-to-text                              |
| `AUDIO_STT_ENGINE`            | `openai`             | Use OpenAI-compatible API                         |
| `AUDIO_STT_OPENAI_API_BASE_URL` | `http://192.168.10.7:8000/v1` | whisper-api URL                          |
| `AUDIO_STT_OPENAI_API_KEY`    | `sk-not-needed`      | whisper-api doesn't validate it                   |
| `SANDBOX_URL`                 | `http://sandbox-agent:8082` | (optional, used by some integrations)        |
| `TOOL_SERVER_CONNECTIONS`     | (JSON list)          | Pre-wires the OpenAPI tool connection             |

### `TOOL_SERVER_CONNECTIONS` format

```json
[{
  "type": "openapi",
  "url": "http://openapi-proxy:8090",
  "path": "openapi.json",
  "info": {
    "id": "sandbox-tools",
    "name": "SandboxTools",
    "description": "Persistent shell + web search"
  },
  "config": { "enable": true },
  "auth_type": "bearer",
  "key": "${OPENAPI_PROXY_TOKEN}",
  "spec_type": "url"
}]
```

The `key` here is the same bearer token the proxy validates against
`OPENAPI_PROXY_TOKEN`. If they don't match, every call returns 401.

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
| `/opt/search`                          | `/opt/search`         | ro    | Existing toolchain             |

## Ports (host → container)

| Host port           | Service        | Notes                                 |
|---------------------|----------------|---------------------------------------|
| `192.168.10.7:3001` | open-webui     | Web UI                                |
| `192.168.10.7:8090` | openapi-proxy  | OpenAPI + WS                           |

`sandbox-agent` has **no host port mapping** — only `expose:` in compose
so the openapi-proxy can reach it via the Docker network. The bearer
token is still required for defence in depth.

Change the host IP / port mapping in `docker-compose.yml` if your host has
a different address. The internal container ports (3001, 8082, 8090)
should stay the same — they are referenced by inter-service URLs.

## systemd unit (`/etc/systemd/system/agent-stack.service`)

`User=git` and `Group=docker` — adjust if your Docker socket is owned by
a different group. The `WorkingDirectory` must point at the directory
containing `docker-compose.yml`.
