# Configuration Reference

Every knob you can turn on this stack, organised by service. All env vars
are set in `docker-compose.yml` unless noted otherwise.

## Sandbox agent (`sandbox-agent` service)

| Env var     | Default | Description                                  |
|-------------|---------|----------------------------------------------|
| (none)      |         | The agent reads no env vars; everything is path-based. |

### Filesystem layout inside the container

| Path                | Source                                  | Writable? |
|---------------------|-----------------------------------------|-----------|
| `/workspace`        | bind-mount `/opt/agent-workspace`       | yes       |
| `/root`             | named volume `sandbox-agent-home`       | yes       |
| `/var/cache/apt`    | named volume `sandbox-agent-apt`        | yes       |
| `/root/.cache/pip`  | named volume `sandbox-agent-pip`        | yes       |
| `/opt/sandbox/`     | baked into image                        | no        |
| `/opt/search`       | bind-mount `/opt/search` (read-only)    | no        |
| `/root/.ssh`        | bind-mount `/opt/agent-workspace/.ssh` (read-only) | no |
| `/tmp`              | container tmpfs                         | yes (ephemeral) |

### Healthcheck

```
curl -fsS http://127.0.0.1:8082/healthz
```

Runs every 10 s; container is marked unhealthy after 3 consecutive failures.

## OpenAPI proxy (`openapi-proxy` service)

| Env var        | Default                  | Description                              |
|----------------|--------------------------|------------------------------------------|
| `SANDBOX_URL`  | `http://127.0.0.1:8082`  | Upstream sandbox-agent URL               |
| `SEARXNG_URL`  | `http://127.0.0.1:8888`  | SearXNG instance used by `web_search`    |

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
| `ENABLE_SIGNUP`               | `true`               | Allow new accounts                                |
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
  "auth_type": "none",
  "spec_type": "url"
}]
```

## Volume mounts (named)

| Name                  | Mounted at              | Purpose                              |
|-----------------------|-------------------------|--------------------------------------|
| `sandbox-agent-home`  | `/root`                 | `~/.bashrc`, configs, installed tools |
| `sandbox-agent-apt`   | `/var/cache/apt`        | apt download cache                   |
| `sandbox-agent-pip`   | `/root/.cache/pip`      | pip download cache                   |

Delete these with `docker volume rm <name>` to start fresh.

## Bind-mounts (host)

| Host path                              | Container path        | Mode  | Purpose                        |
|----------------------------------------|-----------------------|-------|--------------------------------|
| `/opt/agent-workspace`                 | `/workspace`          | rw    | Main working dir               |
| `/opt/agent-workspace/.ssh`            | `/root/.ssh`          | ro    | SSH keys for outbound ssh      |
| `/opt/agent-workspace/.open-webui-data`| `/app/backend/data`   | rw    | Open WebUI's SQLite DB         |
| `/opt/search`                          | `/opt/search`         | ro    | Existing toolchain             |

## Ports (host → container)

| Host port           | Service        | Notes                                 |
|---------------------|----------------|---------------------------------------|
| `192.168.10.7:3001` | open-webui     | Web UI                                |
| `192.168.10.7:8082` | sandbox-agent  | FastAPI                               |
| `192.168.10.7:8090` | openapi-proxy  | OpenAPI + WS                           |

Change the host IP / port mapping in `docker-compose.yml` if your host has
a different address. The internal container ports (3001, 8082, 8090)
should stay the same — they are referenced by inter-service URLs.

## systemd unit (`/etc/systemd/system/agent-stack.service`)

`User=git` and `Group=docker` — adjust if your Docker socket is owned by
a different group. The `WorkingDirectory` must point at the directory
containing `docker-compose.yml`.
