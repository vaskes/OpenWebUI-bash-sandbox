# OpenWebUI-bash-sandbox

Persistent bash sandbox for Open WebUI via OpenAPI tool servers. No middleware patching, no model config — just plug a bash tool into any Open WebUI chat.

## What this is

Three Docker containers that work together:

1. **open-webui** (port 3001) — Web UI for any OpenAI-compatible LLM
2. **sandbox-agent** (port 8082) — FastAPI HTTP wrapper around tmux. One tmux session per chat, state persists across calls
3. **openapi-proxy** (port 8090) — Exposes the sandbox as an OpenAPI 3.0 spec. Open WebUI consumes it as an "External Tool Server"

**The single tool exposed is `bash`.** It runs any shell command in the sandbox and returns stdout/stderr/exit_code. Persistent state (cwd, env vars, history) preserved within the same session_id.

## Architecture

```
┌────────────┐    HTTP    ┌──────────────────┐    HTTP    ┌────────────────┐
│ Open WebUI │ ─────────▶ │ openapi-proxy    │ ─────────▶ │ sandbox-agent  │
│ :3001      │            │ :8090            │            │ :8082          │
└────────────┘            └──────────────────┘            └────────────────┘
      │                            │                              │
      │                            │                              │ tmux
      │                            │                              ▼
      │                            │                       ┌──────────────┐
      │                            │                       │ agent_<id>    │
      │                            │                       │ (persistent) │
      │                            │                       └──────────────┘
   Tool Server              OpenAPI 3.0 spec
   connection               bash(command)
```

Open WebUI calls `POST /bash` on openapi-proxy with `{command: "...", session_id: "chat_xyz"}`. openapi-proxy forwards to sandbox-agent which executes in the corresponding tmux session and returns the output. Open WebUI loops this until the model produces a final answer.

## Why this exists

Open WebUI has built-in support for External Tool Servers (OpenAPI 3.0) and Open Terminal. Both are standard features, no patches needed. This repo packages a minimal OpenAPI-compliant shell that any Open WebUI instance can use as a tool server.

## Files

```
docker-compose.yml         # All 3 services + named volumes
Dockerfile.sandbox         # ubuntu:24.04 + python + tmux + ssh
Dockerfile.openapi-proxy   # python:3.11-slim + curl + FastAPI
agent_api.py               # FastAPI: /healthz, /sessions, /sessions/{id}/exec
openapi_proxy.py           # OpenAPI 3.0 wrapper around sandbox-agent
agent-stack.service        # systemd unit for autostart
```

## Endpoints (default host: 192.168.10.7)

```
3001   Open WebUI UI
8082   Sandbox agent FastAPI (per-chat tmux sessions)
8090   OpenAPI proxy (binds /openapi.json for Open WebUI tool server)
```

## Quick deploy

### Prerequisites
- Docker + Docker Compose
- Linux host with curl
- (Optional) systemd for autostart

### One-shot install

```bash
git clone https://github.com/vaskes/OpenWebUI-bash-sandbox.git
cd OpenWebUI-bash-sandbox
docker compose up -d --build
```

Wait ~30 seconds for all three containers to become healthy:
```bash
docker compose ps
```

You should see all three with status `(healthy)`.

### Wire up Open WebUI

In Open WebUI (Admin → Settings → External Tool Servers):
- Click **+ Add Connection**
- Type: **OpenAPI**
- URL: `http://localhost:8090` (or `http://<host-ip>:8090` if Open WebUI runs on another host)
- Path: `openapi.json` (auto-detected)
- Auth: None (or Bearer with any placeholder if it complains)
- Click **Save**

Or set via env var in docker-compose for `open-webui` service:
```yaml
environment:
  - TOOL_SERVER_CONNECTIONS=[{"type":"openapi","url":"http://openapi-proxy:8090","path":"openapi.json","info":{"id":"sandbox-tools","name":"SandboxTools"},"config":{"enable":true},"auth_type":"none","spec_type":"url","key":""}]
```

### Test it

In a new chat:
1. Click the **🔧 Tools** button
2. Select **SandboxTools** → Done
3. Send: `pwd`

The model should call `bash(command="pwd")` and return `/workspace`.

Then try:
- `cat /etc/hostname`
- `python3 --version`
- `apt install tree && tree /`
- `git clone https://github.com/some/repo.git`
- `ssh root@some-host uptime`

State persists across calls within the same chat (same `session_id`).

## Persistent shell

Each chat session gets its own tmux session named `agent_<chat_id>`. State (cwd, env vars, history) is preserved across calls.

If you need to attach interactively:
```bash
docker exec -it sandbox-agent tmux attach -t agent_<chat_id>
```

To list sessions:
```bash
docker exec sandbox-agent tmux ls
```

## Configuration

### Sandbox workspace

`/workspace` inside the container maps to `/opt/agent-workspace` on the host (see `docker-compose.yml`):
```yaml
volumes:
  - /opt/agent-workspace:/workspace
```

Put files there, they appear in the sandbox.

### Persistent install cache

Three named volumes prevent re-downloads on container rebuild:
- `sandbox-agent-home` → `/root` (installed packages, configs)
- `sandbox-agent-apt` → `/var/cache/apt`
- `sandbox-agent-pip` → `/root/.cache/pip`

### SSH keys

Mounted read-only from `/opt/agent-workspace/.ssh/`:
```yaml
volumes:
  - /opt/agent-workspace/.ssh:/root/.ssh:ro
```

The sandbox can `ssh user@host ...` using these keys, but cannot modify them.

### Read-write filesystem

Sandbox is explicitly `read_only: false`, `user: root` — agents can `apt install`, `pip install`, write files anywhere.

## Autostart on boot (systemd)

```bash
sudo cp agent-stack.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agent-stack.service
```

Check status:
```bash
sudo systemctl status agent-stack.service
docker compose ps
```

## Troubleshooting

### Container unhealthy

```bash
docker inspect <container> --format "{{.State.Health.Status}}"
docker logs <container> --tail 30
```

Common causes:
- `openapi-proxy` unhealthy → `curl` missing from image. Check `Dockerfile.openapi-proxy` has `apt-get install curl`. Rebuild.
- `sandbox-agent` unhealthy → port 8082 conflict. Check `docker ps` for duplicates.
- `open-webui` unhealthy → check DB volume mount: `/opt/agent-workspace/.open-webui-data` must exist and be writable.

### bash tool returns "All connection attempts failed"

openapi-proxy can't reach sandbox-agent. Check:
```bash
docker exec openapi-proxy env | grep SANDBOX
```
Must show `SANDBOX_URL=http://sandbox-agent:8082`. If empty, restart via systemd:
```bash
sudo systemctl restart agent-stack.service
```
Note: `docker compose restart` does NOT re-read env vars. You need `docker compose up -d` (which systemd unit does).

### Tool not visible in chat

1. Connection not saved in Open WebUI → Settings → External Tool Servers → add again
2. Tool not enabled in current chat → click 🔧 → enable SandboxTools → Done
3. Open WebUI cache stale → Ctrl+Shift+R

### Sandbox-agent API not responding

```bash
curl http://localhost:8082/healthz
# → {"status":"ok","sessions":0,"workspace":"/workspace"}

curl -X POST http://localhost:8082/sessions/test/exec \
  -H "Content-Type: application/json" \
  -d "{\"command\":\"pwd\",\"timeout\":5}"
```

## For AI agents

If you (an AI agent) are asked to set this up from scratch or fix a broken instance:

### Setup from scratch
1. `git clone https://github.com/vaskes/OpenWebUI-bash-sandbox.git`
2. `cd OpenWebUI-bash-sandbox`
3. `docker compose up -d --build`
4. Wait for `docker compose ps` to show all three healthy
5. Add External Tool Server in Open WebUI: URL `http://openapi-proxy:8090`, path `openapi.json`

### Diagnose broken instance
1. `docker compose ps` — which container is unhealthy?
2. `docker logs <container> --tail 50`
3. For openapi-proxy: `docker exec openapi-proxy env | grep SANDBOX` — must not be empty
4. For sandbox-agent: `curl http://localhost:8082/healthz` — must return JSON
5. For open-webui: `curl http://localhost:3001/api/version` — must return JSON

### Fix common issues

**openapi-proxy env empty:**
```bash
sudo systemctl restart agent-stack.service
# OR
docker compose down && docker compose up -d
```
(`docker compose restart` alone doesn't pick up new env vars.)

**curl missing in openapi-proxy:**
Check Dockerfile.openapi-proxy has:
```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends curl
```
Then `docker compose build openapi-proxy && docker compose up -d openapi-proxy`.

**Sandbox-agent not responding:**
Check workspace mount:
```bash
ls -la /opt/agent-workspace/
docker exec sandbox-agent ls -la /workspace/
```
If empty/missing, create the host dir: `sudo mkdir -p /opt/agent-workspace && sudo chown $USER:$USER /opt/agent-workspace`.

**Open WebUI loses tool connection on restart:**
The connection is saved in Open WebUI's SQLite DB at `/opt/agent-workspace/.open-webui-data/webui.db` — which is mounted as a volume, so it persists. If missing, re-add via Settings → External Tool Servers.

## Limitations

- **No isolation between chats.** All users share the same `sandbox-agent` container. If multiple users connect simultaneously, they share tmux sessions if they use the same `session_id` (which Open WebUI generates per-chat, so usually fine).
- **No network isolation.** The sandbox has full network access. For sensitive workloads, add network policies or run in a separate namespace.
- **No resource limits.** A runaway model could fill the disk. Add ulimits to docker-compose if needed.

## License

MIT
