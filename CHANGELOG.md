# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.1.0] - 2026-10-13

### Fixed (security audit response)
- **`import re` was missing from `agent_api.py`** — every successful
  command crashed with `NameError`. v1 was broken in production.
- **Path-traversal in `cwd`** — `POST /sessions` accepted any path,
  including `..` and symlinks pointing outside the workspace. Now uses
  `os.path.realpath` + workspace whitelist; rejects with 400.
- **CORS `allow_origins=["*"]` + no auth** on `openapi-proxy` —
  any browser could call the API. Now requires `OPENAPI_PROXY_TOKEN`
  (constant-time compare) and uses an opt-in CORS allowlist.
- **Container ran as root** with no resource limits. Now runs as
  `1000:1000` with `mem_limit`, `cpus`, `pids_limit`,
  `cap_drop: [ALL]`, `no-new-privileges: true`, and a `read_only: true`
  root filesystem with tmpfs.
- **`WEBUI_SECRET_KEY` and other secrets were hard-coded in
  `docker-compose.yml`**. Moved to `.env` (with `.env.example`
  template).
- **SSH `StrictHostKeyChecking=accept-new` for any host** — MITM was
  trivial. Added `OPENAPI_PROXY_SSH_ALLOWLIST` (CIDR + hostname).
- **Per-WebSocket session was shared** — two users saw each other's
  terminal output. Now generates a unique `ws-<hex>` per connection
  and tears it down on disconnect.
- **WebSocket polled via `exec` (spawns bash per tick)** — added a
  dedicated `/capture` endpoint that just calls `tmux capture-pane`
  with no shell wrapper.
- **`httpx.AsyncClient()` was created on every request** — fixed with
  a singleton client in the FastAPI `lifespan` handler.
- **In-memory `SESSIONS` dict lost state on container restart** —
  now persisted to `/workspace/.agent-sessions.json` and reloaded on
  boot.

### Added
- Structured JSON logging with session_id, command length, exit code,
  duration_ms on every `exec`.
- Token-bucket rate limiter (`OPENAPI_PROXY_RATE_LIMIT_RPM`).
- Multi-stage Dockerfiles — smaller images, build deps stay in builder.
- Uvicorn graceful shutdown (`timeout-graceful_shutdown=25`).
- 35 new integration tests covering auth, CORS, path traversal, SSH
  allowlist, rate limit, validation, persistence, exec timeout.
- `.env.example` documenting every secret.
- CI: bandit SAST, gitleaks secret scan, trivy container scan,
  coverage threshold (`--cov-fail-under=70`).
- `docs/SECURITY.md` rewritten as a status table + hardening checklist.

### Changed
- `agent_api.py` and `openapi_proxy.py` rewrite of error handling —
  every upstream failure returns a structured `{"error": "..."}`.
- `BashReq.command` and `ExecRequest.command` now have
  `min_length=1, max_length=1_000_000` for early rejection of bad input.
- `OpenAPI` `info.version` for both services is now sourced from
  `__version__` in the corresponding `*_core` module.

## [1.0.0] - 2026-09-13

### Added
- Initial three-container stack: Open WebUI + sandbox-agent + openapi-proxy
- Single `bash` tool exposed via OpenAPI 3.0
- Persistent tmux sessions per chat session_id
- Persistent named volumes for `/root`, apt cache, pip cache
- systemd unit `agent-stack.service` for autostart on boot
- Auto-wired Tool Server connection in `TOOL_SERVER_CONNECTIONS` env var
- WebSocket `/ws` terminal endpoint (experimental)

[2.1.0]: https://github.com/vaskes/OpenWebUI-bash-sandbox/compare/v1.0.0...v2.1.0
[1.0.0]: https://github.com/vaskes/OpenWebUI-bash-sandbox/releases/tag/v1.0.0
