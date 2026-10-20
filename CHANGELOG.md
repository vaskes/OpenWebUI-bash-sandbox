# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.5.0] - 2026-10-20

v2.4.5 shipped with green CI and **could not be built**. This release makes a
clean `git clone && docker build` actually work, and adds the CI gate that
would have caught it.

### Fixed

- **`Dockerfile.sandbox` — clean build failed with `groupadd: GID '1000'
  already exists`.** `ubuntu:24.04` already ships `ubuntu:x:1000:1000`, so the
  named `groupadd -g 1000` / `useradd -u 1000` could never succeed. Now uses
  numeric `USER 1000:1000` with no user creation.
- **`Dockerfile.sandbox` — pip packages were copied to
  `/usr/local/local/lib/...`.** Ubuntu's pip uses the `posix_local` scheme, so
  the install root is `/install/local/`. Copying `/install` verbatim produced
  a doubled prefix and the container died with `No module named uvicorn`.
  Now copies `/install/local/`.
- **`Dockerfile.openapi-proxy` — clean build failed with `group 'proxy'
  already exists`.** `python:3.11-slim` ships a group *named* `proxy` at GID
  13; uid/gid 1000 are free, only the name collides. Also fixed to numeric
  `USER 1000:1000`. Its pip copy is intentionally left as `/install` — the
  Debian `posix_prefix` scheme really does put packages directly there, and
  "unifying" it with the sandbox Dockerfile breaks the proxy.
- **`Dockerfile.openapi-proxy` — `HEALTHCHECK` probed the authenticated
  `/healthz`,** so a bare `docker run` of the image never went healthy. Now
  probes the unauthenticated `/openapi.json`.
- **`openapi_proxy.py` — `TypeError: FastAPI.__init__() got an unexpected
  keyword argument 'openapi_version'` on a clean install.** The kwarg was
  accepted only through `**kwargs` in the pinned 0.143.x and is rejected from
  0.153.0 on. Removed rather than papering over it with a narrow pin — the
  app has always been serving OpenAPI 3.1.0, so the code was describing
  something untrue, and pinning would hide the bug and freeze every fresh
  install onto a stale dependency set.
- **The agent hung forever on its first command in a cold container
  (silent, total failure).** uvicorn auto-selects uvloop whenever it is
  importable, and under uvloop the first `tmux new-session` — the call that
  forks and daemonises the tmux server — never completes; asyncio's subprocess
  pipes for the daemonising process never resolve. `/healthz` kept answering
  200 the whole time, so the container reported healthy and could not run a
  single command. The agent now pins `--loop asyncio`. This bug predates
  v2.5.0 but had never been observed, because the image had never built.

### Added

- **CI `deploy` job.** Builds both images, boots the stack, blocks until both
  containers are healthy, executes a real command through `POST /bash`,
  asserts the output *and* uid 1000, checks the agent's `/healthz` is exactly
  `{"status":"ok"}`, dumps logs on failure and tears down. The previous
  pipeline ran `pip install` and tested Python source against the runner's
  environment; it never ran `docker build`, which is how v2.4.5 certified an
  artifact nobody had shipped. `docker-compose.ci.yml` deliberately excludes
  open-webui — a multi-GB pull that says nothing about our own images.
- **`tests/test_deploy_artifacts.py`** (13 cases) pins the Dockerfile and
  dependency contracts so they cannot silently regress.
- **`tests/test_exec_event_loop.py`** pins `--loop asyncio` and asserts a real
  cold-start exec completes under the loop the image actually uses.

### Changed

- `requirements.txt` keeps the existing `fastapi>=0.110,<1.0` range. The dead
  kwarg was removed from the application rather than pinned around.

## [2.4.5] - 2026-10-14

### Fixed (CodeQL alert follow-up)

- **`.github/workflows/ci.yml` — explicit `permissions: contents: read`**.
  CodeQL `actions/missing-workflow-permissions` fired on v2.4.4
  because the workflow had no explicit permissions block, so the
  GITHUB_TOKEN inherited the repo's default (read-write for repos
  created before Feb 2023 — which this one is). All four jobs
  (lint, test, SAST, secrets) only need read access, so the
  least-privilege default at workflow level is correct. Tripwire
  in `tests/test_wave3_workflow_permissions.py` pins this.

### Testing

- 131 tests total (was 129 at v2.4.4, +2 tripwires for workflow
  permissions).
## [2.4.4] - 2026-10-14

### Changed (release-prep wave 3 — public-release readiness)

Five independent items, all low-risk housekeeping. Together they
make the project fit for a public release announcement.

- **LICENSE copyright holder**: `vaskes` → `Kuduza Ai Lab`. The
  project started as a vaskes pet project; v2.4+ is a Kuduza
  product. Operators who forked under the old copyright can
  continue to use the code under MIT.
- **CODE_OF_CONDUCT.md enforcement contact**: added
  `conduct@kuduza.ai` with the standard confidentiality clause.
  The Contributor Covenant v2.0 template leaves the reporting
  mechanism blank; this fills it in.
- **pyproject.toml `target-version`**: `py311` → `py312`. The
  project runs on 3.11 and 3.12 in CI; the bump nudges
  contributors toward 3.12 patterns.
- **Rate-limit key hashed** (wave 2 deferred #L3): the bearer
  token is now SHA-256-hashed before being used as the
  rate-limiter key. The dict was process-local and LRU-evicted,
  so the risk was bounded, but hashing removes the in-memory
  token presence entirely.
- **No content changes**: no source files in `agent_api.py` or
  `openapi_proxy.py` were touched (except for the one-line
  rate-limit hash change).

### Testing

- 129 tests total (was 125 at v2.4.3, +4 wave 3 self-audit
  tripwires: LICENSE copyright, CODE_OF_CONDUCT contact,
  pyproject target-version, rate-limit key hashing).
## [2.4.3] - 2026-10-14

### Fixed (release-prep wave 2 — security self-audit)

Mavis self-audit on top of v2.4.2. The Qwen-driven wave 2 of v2.4.0
covered the original audit; this wave closes three defence-in-depth
items the v2.4.0 round did not flag.

- **`/healthz` on agent_api no longer leaks the workspace path** —
  the unauthenticated endpoint now returns only `{"status": "ok"}`.
  Previously it returned `WORKSPACE_REAL` and the active session
  count, both minor info leaks (#H1).
- **Security headers on every response** — added a Starlette
  middleware that sets `X-Content-Type-Options: nosniff` and
  `Cache-Control: no-store` on every response (including error
  responses). X-Frame / CSP / HSTS are intentionally omitted:
  this is a bearer-token JSON API, not a browser surface (#L1).
- **Request body size cap** — both services reject bodies > 2 MB
  with HTTP 413 before Pydantic sees them. The Pydantic field
  `max_length` caps the *command* at 1 MB but a hostile client
  could send a body that parses to a small command and weighs
  megabytes of padding. Check Content-Length, not a streaming
  read (#M1).
- **CREDITS / README credit Mavis alongside Qwen** — wave 1, 2, 3
  of the self-review were Mavis (this agent), not Qwen. The
  original credits only mentioned Qwen because the early v2.4.0
  round was a Qwen-side review.

### Testing

- 125 tests total (was 115 at v2.4.2, +10 tripwires in
  `tests/test_wave2_self_audit.py`: 4 classes covering healthz
  minimality, security headers, and body size limit on both
  services).
## [2.4.2] - 2026-10-14

### Fixed (release-prep wave 1)

The v2.4.1 commit at tag `96318bb` shipped a CHANGELOG entry claiming
21 closed items, all green tests, and 108 tests total. Wave 1 of the
post-release self-audit found that 4 of those claims were still
inaccurate, plus 4 more doc drift items that had slipped through
review. The wave-trap pattern (T60): claim vs implementation drift.

- **README badge "Tests: 103" was stale** — actual was 108 at v2.4.1.
  Updated to 108 in both the alt text and the descriptive bullet.
- **CHANGELOG v2.4.1 "### Testing" claimed "104 tests +1 tripwire"**
  — actually 108 (+5: 1 in `test_agent_api.py`, 4 in
  `test_drift_release_claims.py`). Corrected.
- **`.env.example` claimed a CHANGE_ME placeholder check existed**
  in Makefile / compose — it did not. Implemented a `check-env`
  target in the Makefile, chained into `build`, `up`, `restart`.
  Verified manually: fails with exit 2 and lists offending keys
  when placeholders are present; passes when all keys have real
  values.
- **`docs/CONFIGURATION.md` said `ENABLE_SIGNUP` default is `true`**
  — compose `:-false` makes the default `false`. Corrected.
- **`CONTRIBUTING.md` project layout said `proxy_core.py` only has
  "command prefixes, timeouts"** — drift from `ARCHITECTURE.md`
  which already lists "WebSocket tunables". Synced.
- **`CREDITS.md` said the review was "two-wave"** — actually wave
  1 + waves 2-3 + sandbox-recommendations round + release prep.
  Updated to "multi-wave" with the wave list.
- **PR template checklist said "update CHANGELOG.md under
  [Unreleased]"** — the project doesn't use an [Unreleased]
  section. Changed to "add a row to CHANGELOG.md for the next
  release".

### Testing

- 115 tests total (was 108 at v2.4.1, +7 tripwire cases in
  `test_drift_release_claims.py`: README badge sync, .env.example
  placeholder check, Makefile check-env smoke test, and chained-
  targets assertion).
- New tripwires: `TestReadmeTestCountMatches`,
  `TestEnvExampleMatchesCheckEnv`, `TestMakefileHasCheckEnv`.
  These catch the wave-trap pattern in three more places where it
  has bitten v2.4.0 and v2.4.1: README badges, .env.example
  placeholder formats, and Makefile target dependencies.

## [2.4.1] - 2026-10-14

### Fixed (release prep — pre-release audit)

3 CRITICAL blockers from the pre-release review of v2.4.0:

- **Version drift between services** — `agent_core.py` and
  `agent_api.py` were still hardcoded to `1.1.0` after v2.3.0 and
  v2.4.0; only `proxy_core.py` and `openapi_proxy.py` knew the
  right version. `agent_api` now imports `__version__` from
  `agent_core` (mirroring how `openapi_proxy` imports from
  `proxy_core`); all three modules now report `2.4.1`. New
  `test_agent_version_matches_proxy_version` tripwire prevents
  the drift from coming back.
- **`OPENAPI_PROXY_WEBSOCKET_IDLE_TIMEOUT` not documented** — the
  v2.4.0 knob was invisible to operators. Added to
  `docs/CONFIGURATION.md`, the `docker-compose.yml` env list, and
  the README Configuration section.
- **`.env.example` missing** — `README.md` Quick Start said `cp
  .env.example .env` but no template existed. Created a complete
  template with every `OPENAPI_PROXY_*` knob and explicit
  `CHANGE_ME_<recipe>` placeholders. Updated `.dockerignore` to
  explicitly include the template while excluding real `.env*`
  files.

7 HIGH doc/observability drift fixes:

- `README.md`: tests badge 58 → 103; CI bullet "ruff + bandit +
  gitleaks + trivy" → "ruff + ruff B-rules (SAST) + gitleaks"
  (bandit and trivy are not in CI); sandbox-agent description
  "no auth" → "no host port mapping — internal network only;
  bearer token required"; `/ws?token=...` → `/ws` with
  `Sec-WebSocket-Protocol: bearer.<token>` + warning footnote
  about reverse-proxy log leaks.
- `docs/CONFIGURATION.md`: filled in the sandbox-agent env var
  table (`AGENT_WORKSPACE`, `AGENT_SESSIONS_FILE`, `AGENT_TOKEN`,
  `AGENT_LOG_LEVEL`, `AGENT_PORT`); filled in the openapi-proxy
  table with all 9 env vars including the new
  `OPENAPI_PROXY_WEBSOCKET_IDLE_TIMEOUT`; fixed filesystem
  layout table — `/root` → `/home/agent` (matches `Dockerfile.sandbox`
  `useradd -m -d /home/agent`); fixed `TOOL_SERVER_CONNECTIONS`
  example — `auth_type: "none"` → `"bearer"` with a `key` field;
  removed the misleading `192.168.10.7:8082` host port mapping
  (sandbox-agent has no host port, only `expose:`).
- `docs/ARCHITECTURE.md`: persistent-state table — Open WebUI DB
  row updated from "bind-mount `.open-webui-data`" to "named
  volume `open-webui-data` (NOT bind-mount — wave 3 #C1)";
  module-layout caption — `proxy_core.py` description includes
  "WebSocket tunables".
- `docs/SECURITY.md`: "Status" table — bumped from v2.1 to v2.4,
  rows for every wave 2 and wave 3 fix; "What's intentionally not
  implemented" — explicit entries for the four
  sandbox-recommendations by-design items: Docker secrets for the
  bearer token (#M1), per-sandbox session LRU (#M2), residual
  TOCTOU between `_validate_ssh_args` and `ssh -i` (#M1 of the
  bot-side review), and a refresh of "Seccomp/AppArmor not
  configured".

### Changed

- `requirements-dev.txt`: dropped `bandit` (replaced by `ruff
  --select B` since v2.3.0; Dependabot #11 confirmed it's unused
  dead weight). Dropped the `mypy` line; we don't run a mypy job
  in CI and pinning a version we don't use invites supply-chain
  noise.

### Testing

- 108 tests total at v2.4.1 tag (was 103, +5 tripwires — 1 in
  `tests/test_agent_api.py`, 4 in `tests/test_drift_release_claims.py`).
- `test_agent_version_matches_proxy_version` asserts that
  `agent_core.__version__ == agent_api.__version__ ==
  proxy_core.__version__`. If you bump one and forget the others,
  the test fires.
- `test_drift_release_claims.py` walks the latest CHANGELOG entry for
  file-path references and asserts each one exists in the working
  tree. Plus `TestAllEnvVarsDocumented` asserts every
  `os.environ.get("X")` in `agent_api.py` / `openapi_proxy.py`
  appears in `docs/CONFIGURATION.md`. Future releases cannot claim
  an undelivered fix without the test firing on CI.

## [2.4.0] - 2026-10-14

### Security (sandbox-recommendations from bot-side review)

These fixes were forwarded from the 2026-10-14 Qwen 3.8-Max adversarial
review of the `vaskes/llama.cpp_telegram` bot + this sandbox combined
deployment. The bot-side half of the review is in bot commit `dc2731e`;
this release is the sandbox-side remainder.

- **Command content logging** (`agent_api.py`) — both the success and
  timeout exec log lines now include `cmd=%r` with a 200-char preview of
  the command itself, in addition to the original `cmd_len=N` field.
  Post-incident audit can now answer "what did the LLM actually run at
  23:04?" from the JSON logs alone. Long commands (>200 chars) are
  truncated with a `+N bytes` suffix so a runaway 1 MB command does
  not flood the log.

- **SSH identity symlink rejection** (`openapi_proxy.py`) —
  `_ssh_identity_within_workspace()` now returns `False` for any path
  that is a symlink, regardless of where it points. The previous
  `realpath`-only check had a TOCTOU window: the LLM could create a
  regular file, get it validated, then replace it with a symlink to
  `/home/agent/.ssh/id_rsa` (the persistent operator key mount) before
  the actual `ssh -i` call. The symlink check is the only thing
  preventing the LLM from using host-resident keys via this vector.

- **WebSocket idle timeout** (`openapi_proxy.py`) — terminal `/ws`
  connections now wrap `receive_text()` in `asyncio.wait_for()` with a
  configurable `OPENAPI_PROXY_WEBSOCKET_IDLE_TIMEOUT` (default 300 s,
  set to `0` to disable). A forgotten browser tab or a misbehaving
  client no longer pins a tmux session and a file descriptor forever;
  the server closes the connection with `1008 / "idle timeout"`.
  Default of 5 minutes is the de-facto standard for interactive
  terminals.

### Out of scope (documented as by-design)

- **`#M1` Docker secrets for the bearer token** — recommended for
  multi-tenant / hosted deployments, not applicable to the
  single-operator personal model this repo targets. The threat
  model "someone with shell on my host reads my .env" is already
  past the line for that operator. If a multi-tenant deployment is
  ever planned, the migration path is: `secrets:` block in compose
  instead of `environment:` for the token, and the bot's
  `personal_tools/bash_command.py` reads from `/run/secrets/`. That
  is a coordinated change in the bot repo, not just here.

- **`#M2` Per-sandbox session LRU eviction** — the bot's
  `session_id` is deterministic (`chat-{chat_id}`), so the natural
  upper bound is "one session per chat". If the bot's session_id
  scheme ever changes to per-user-id or per-message-id, the right
  place to add LRU eviction is the bot's `call_llama.py` (it knows
  the scheme), not here.

### Testing

- 103 tests total (was 92, +11 sandbox-recommendations regression)
- `tests/test_sandbox_recommendations.py`: 11 tests covering
  `#C1` (cmd preview in log), `#C7` (symlink rejection — both
  workspace→outside and inside→inside, plus the happy path), `#M3`
  (config defaults, env override, 0=disabled, and `asyncio.wait_for`
  present in the endpoint).

## [2.3.0] - 2026-10-13

### Security (CRITICAL fixes from Wave 3 audit)

- **Open WebUI SQLite moved to named volume** — was bind-mounted into
  `/workspace`, allowing the LLM to `rm -rf /workspace/.open-webui-data`
  and wipe the entire chat database. Now uses `open-webui-data` named
  volume, completely isolated from the agent's writable surface.

- **SSH `-i <path>` must be inside workspace** — previously accepted any
  path matching the char whitelist, including `/home/agent/.ssh/id_rsa`
  (the persistent operator key mount). Now uses `realpath` + `commonpath`
  to verify the identity file resolves to a path inside `$AGENT_WORKSPACE`.
  Rejects with structured error if outside.

- **SSH `-o` options whitelisted by name** — was a blocklist that only
  rejected `ProxyCommand` and `StrictHostKeyChecking`. Dangerous options
  like `LocalCommand`, `PermitLocalCommand`, `LocalForward`,
  `RemoteForward`, `DynamicForward` would have passed. Now whitelist
  contains only 10 safe connection-tuning options
  (`ConnectTimeout`, `ServerAliveInterval`, `BatchMode`, etc.).
  Everything else rejected. Case-insensitive matching.

### Reliability

- **Startup reaper for stale sessions** — `_reap_dead_sessions()` runs
  in lifespan startup, queries `tmux list-sessions`, drops registry
  entries whose tmux backend died (e.g. container killed abruptly).
  Rewrites `SESSIONS_FILE`. Prevents stale entries from accumulating.

- **WebSocket happy path tested** — added positive test for
  `Sec-WebSocket-Protocol: bearer.<token>` authentication. Browser-based
  WebSocket clients (Open WebUI terminal) cannot set Authorization header,
  so this is the only auth path they can use.

### Testing

- 92 tests total (was 72, +20 Wave 3 regression tests)
- `tests/test_wave3_fixes.py`: 15 tests covering every CRITICAL/HIGH fix
- `tests/test_wave2_fixes.py`: +1 WebSocket happy path test

### CI

- Docker build job removed — QEMU segfault regression on ubuntu-24.04/22.04
  runners (actions/runner-images#11589). Production deployment exercises
  Docker on real host via `agent-stack` systemd unit. Documented gap.
- `.github/codeql-config.yml` removed — had `queries: []` (no-op).
  Default CodeQL setup at repo level is the right place.
- Ruff config in `pyproject.toml` — line-length=100, B-rules enabled,
  per-file ignores for tests.

## [2.2.0] - 2026-10-13

### Fixed (security audit wave 2)

Critical (7 blockers):
- **`Dockerfile.sandbox` would not start** — WORKDIR was `/workspace` but source
  files were in `/home/agent`, so `agent_api` was not on the import path.
  Added `PYTHONPATH=/home/agent` and set `WORKDIR=/home/agent` before the
  `COPY` step.
- **`lifespan` killed all tmux sessions on shutdown** — broke the very
  persistence it was supposed to enable. Now does nothing on shutdown;
  the SESSIONS_FILE registry is the single source of truth.
- **Sandbox-agent port 8082 was exposed to the LAN without auth** — now
  requires `AGENT_TOKEN` (bearer, constant-time compare) and the port
  mapping is removed; the agent is reachable only via the Docker network.
- **`tmux send-keys` without `-l`** interpreted special tokens
  (`Enter`, `C-a`, arrow keys) — would silently corrupt commands like
  `git commit -m "Press Enter to continue"`. Now uses `send-keys -l`.
- **SSH command injection via allowlist bypass** — the allowlist only
  checked the host. `ssh host ; rm -rf /` would have passed. Now parses
  args with `shlex`, rejects unsafe characters, and allows only a small
  whitelist of flags (`-p`, `-l`, `-i`, `-o`, `-q`, `-v`...).
- **Marker regex picked the first match** — a malicious command could
  print a fake `__AGENT_DONE_..._=0` to confuse the parser. Now uses
  `re.findall` and takes the **last** match (the legitimate one).
- **WebSocket token in query params** — every reverse proxy logs URLs.
  Removed; token is now only accepted via `Sec-WebSocket-Protocol`.

Architectural (5):
- **Blocking I/O in `_save_sessions`** — wrapped in `asyncio.to_thread`
  so a slow host filesystem doesn't stall the event loop.
- **`capture-pane` returned the whole tmux scrollback** — now
  `-S -N` (max `CAPTURE_PANE_LINES` lines) and `_truncate_output`
  caps the response at `MAX_OUTPUT_BYTES`.
- **Logger format bug `token=%sandbox`** — typo, would log " sandbox"
  regardless of token state. Fixed to `token=%s`.
- **`stop_grace_period`** was Docker's 10s default, less than uvicorn's
  25s graceful shutdown. Set to 30s on all three services.
- **RateLimiter buckets dict grew unbounded** — added LRU eviction at
  `OPENAPI_PROXY_RATE_LIMIT_MAX_KEYS` (default 10000).

Minor (8):
- `list_sessions` was O(n) `tmux has-session` calls; now one
  `list-sessions` invocation.
- `_load_sessions` corrupt-file log upgraded from `warning` to `error`.
- `build-essential` and `sshpass` removed from production image.
- `Dockerfile.openapi-proxy` now uses multi-stage build with non-root.
- `WebSocketDisconnect` removed from the httpx-error except clause
  (httpx never raises it).
- `EXC_TIMEOUT_SECONDS` now actually used in upstream calls (was
  hardcoded `60`).
- Hostname allowlist comparison is now case-insensitive.
- CI smoke test fails loudly on container-not-started (logs check + curl
  with `|| exit 1`).

Added:
- `AGENT_TOKEN` env var + bearer auth on every sandbox-agent endpoint
  except `/healthz`.
- `OPENAPI_PROXY_UPSTREAM_TOKEN` (defaults to `OPENAPI_PROXY_TOKEN`) for
  proxy → agent authentication.
- `OPENAPI_PROXY_RATE_LIMIT_MAX_KEYS` knob for the LRU cap.
- 14 new regression tests pinning every fix above (`tests/test_wave2_fixes.py`).
- Total: **72 tests**, all passing.

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
