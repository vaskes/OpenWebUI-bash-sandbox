# Roadmap: the gaps against the other sandbox

**Measured 2026-11-01** on the Hermes stand (`nousresearch/hermes-agent:main-desktop`,
workstation) and on this repository. Every number below is a measurement, not a
summary of somebody else's documentation.

**What this document is.** The other sandbox gives its agent a wide surface: a
terminal in a separate container, a browser, file writes, planning, memory, code
execution, sub-agents, cron, a desktop. We give ours four MCP tools and a jail.
The question this document answers is *what to build ourselves* to close the
gaps that matter, and *what to refuse to build*.

**What this document is not.** No code was copied. The other sandbox is MIT
licensed, and a fork would have been the cheap way to get the surface — that is
precisely why it is the wrong way here. Their sandbox is a workstation for a
trusted operator's agent on a LAN; ours is a jail for an agent we do not trust.
The security concept is the product, and a fork would import a different one.
Everything below is to be written here, under our rules.

## The measured surfaces

### Their agent: 45 tools

Core list, as declared in their `toolsets.py` (read, not copied):

```
terminal        process_manage
read_file       write_file       patch           search_files
vision_analyze  image_generate   text_to_speech
skills_list     skill_view       skill_manage
browser_exec    browser_cdp      browser_dialog  browser_vault
todo_list       memory           session_search  clarify
execute_code    delegate_task    cronjob_manage  computer_use
manage_connections
kanban_show  kanban_list  kanban_complete  kanban_block  kanban_request_review
kanban_request_changes  kanban_heartbeat  kanban_comment  kanban_create
kanban_link  kanban_unblock  kanban_attach  kanban_attach_url  kanban_attachments
```

Plus research tools for the open web (the research capability itself is out of
our scope — see below), per-platform bundles (Telegram, Discord, Slack, Feishu,
Weixin, WeCom, Matrix, e-mail, WhatsApp, Signal, …) and a `safe` toolset with
no terminal at all.

### Their executor: a separate container

```
image    nousresearch/hermes-sandbox:desktop   (1.2 GB, 630 apt packages)
user    root
runtime Python 3.13.16, Node 26.11.1, tmux, X11/Xvfb/TigerVNC,
        ImageMagick, gcc-14, dbus
limits  4 CPU, 8 GB RAM, 50 GB disk, persistent, 1800 s lifetime
spawn   by the HOST docker daemon (the agent container holds /var/run/docker.sock)
mounts  docker_mount_cwd_to_workspace: false  ->  the executor does NOT see
        the agent's workspace
```

The agent's file tools write into the agent's workspace; the executor's
commands run somewhere else entirely. Two worlds, one agent.

### Ours: 4 tools, and the jail around them

```
MCP      bash, read_file, file_info, delete_file
HTTP     /healthz /metrics /sessions /sessions/{id}/exec /sessions/{id}/capture
         /sessions/{id}/output /files /files/meta
jail     user 1000:1000, cap_drop ALL, no-new-privileges, read-only rootfs,
         tmpfs caches, pids 256, 2 CPU, 2 GB, workspace = ext4 with a 4 GB
         ceiling, token on every endpoint, DNS-rebinding protection, body
         limit, exec timeout, output cap, secret deny-list
image    bash tmux jq ripgrep fd-find tree htop, numpy/matplotlib/pandas/
         sqlalchemy/openai/requests/httpx/pillow/sympy/pytest/tqdm + wheelhouse
```

## The gaps, ordered by how much they cost us

**1. The agent cannot write or search files through the tools it was given.**
The HTTP side has `POST /files` and `DELETE /files`, but the MCP surface — the
part the model actually holds — offers no write and no search. The model
therefore writes files through `bash` (`cat > …`), which is exactly the path
that bypasses the file fence: the audit trail of the file tools is empty while
the model is busy writing. This is the cheapest gap to close and the most
embarrassing one to have.

**2. The executor and the agent are the same container.** Our `exec` runs in
tmux inside the agent's own container. Whatever the model wrote into the
workspace is sitting right there for the executor, and whatever the executor
wrote is sitting right there for the model. Their design separates the two;
ours does not, and the separation is what makes "the workspace is evidence, not
instructions" a structural property instead of a convention.

**3. No way to the open web from inside the jail.** The agent has a network,
but nothing in it reaches the internet by design, and the research tools that
would answer a question about anything current are **not ours**: v2.13.0
removed the search capability from this project on purpose, because research is
another MCP's job on another host. That decision stands, so this is not a gap
we close — it is a scope boundary we respect. The only question worth asking
is whether the agent may *fetch a URL it was given*, which is not research but
retrieval; that is question 3 below.

**4. No eyes.** No `vision_analyze`. The model cannot read a screenshot, a
figure or a photo — even though our own stack runs a vision model (Strata) and
the image has `pillow`.

**5. No planning, memory, recall or clarification.** `todo_list`, `memory`,
`session_search`, `clarify` are all absent. We already store sessions, so
recall is mostly a query we have not written; the rest is missing outright.

**6. No skills.** `skills_list`, `skill_view`, `skill_manage`. We have a
`CONTRIBUTING.md` and a `docs/` tree, but no mechanism that loads a recipe into
a session when the task calls for it.

**7. No code execution as a tool, no sub-agents.** `execute_code` (Python
calling our own tools programmatically, fewer model round trips) and
`delegate_task` (sub-agent with its own context) are both missing.

**8. No cron from inside the agent.** Scheduling exists on the platform side,
but the agent cannot schedule its own work.

**9. No browser, no desktop control.** `browser_exec` (Python over CDP),
`browser_cdp`, `browser_dialog`, `browser_vault`, `computer_use`. The largest
surface of all, and the one we are least sure we want.

**10. No background process management as a tool.** tmux sessions exist and
the agent drives them through `bash`; `process_manage` would make that
first-class and auditable.

## The waves

Each wave is one release: one purpose, one green commit, mutation-verified,
and no history rewrite. Every wave states how it is proved red.

### Wave A — the agent writes and searches through the fence

New MCP tools, implemented here:

* `write_file` — the same fence as the HTTP `POST /files`: inside the
  workspace, size limit, no binary, no symlink escape, no path that resolves
  outside. *Red:* a write outside the workspace is refused; a symlink that
  points out is refused; a write through the tool is visible to `read_file`.
* `patch` — a diff applied by us, not by a shell. *Red:* a patch that does not
  apply cleanly is refused and changes nothing; a patch cannot touch a path
  outside the workspace.
* `search_files` — by name and by content, with result and time limits.
  *Red:* the search cannot reach outside the workspace; a search over a
  forbidden path returns nothing and says why.

Why first: the HTTP side of all three already exists and is already tested,
so this is a thin, well-fenced layer over code we own — and it closes the
"the model writes through bash" hole, which is the one that makes our own
audit trail incomplete.

### Wave B — the executor gets its own container

Our agent container stops executing commands itself. Commands go to a separate
executor service, reached over HTTP with its own token, with:

* its own image (read-only rootfs, `cap_drop ALL`, `no-new-privileges`, the
  same pids/CPU/memory caps we already set);
* the workspace mounted read-write **only** if a task needs it, and the
  agent's own state directory never mounted;
* no `/var/run/docker.sock` anywhere near it — the agent does not get the
  daemon, so it cannot spawn a container of its own;
* no network by default, and an explicit policy for the network it does get.

*Red:* the executor cannot see the agent's secrets; the executor cannot see the
host's docker socket; a command that tries to reach either is refused by the
image, not by a check.

This wave is the expensive one and it changes the deployment topology, so it
goes last of the "must have" waves and only after the operator has picked the
option below.

### Wave C — eyes

* `vision_analyze` — the image is handed to the vision model we already run
  (Strata on llmhost1), and the answer comes back as text. The agent never
  gets a raw model call of its own; it gets a sandboxed tool that talks to
  one endpoint. *Red:* the tool refuses a file outside the workspace; the
  answer is bounded in size.

Retrieval of a URL the operator named is deliberately **not** in this wave: the
research capability is not this project's (v2.13.0), and retrieval is close
enough to it that it deserves its own decision, not a footnote.

### Wave D — planning, memory, recall, clarification

* `todo_list` — the task list lives with the session, is bounded in size, and
  is reported back in the session record.
* `memory` — a small, explicit, human-readable store the operator can read and
  delete. Not a hidden profile: the file is a file.
* `session_search` — a query over our own sessions, which we already keep.
* `clarify` — the agent can ask the operator a question through the channel it
  is talking on.

### Wave E — fewer round trips, and scale

* `execute_code` — Python inside the executor that may call *our* tools and
  nothing else, with a wall-clock and memory budget. The tool surface it can
  reach is exactly the fence the tools already have.
* `delegate_task` — sub-agents with their own context, each in the executor,
  each with the same jail.

### Wave F — the browser, if it is wanted at all

Our opinion, to be tested by the operator: a browser inside a jail without a
network is a browser that can read nothing but what we give it, and a browser
with a network is the largest attack surface in the product. If it goes in, it
goes in the executor, with its own profile directory in the workspace, no
credentials of ours, and its own allowlist. `computer_use` (control of a
desktop) is out of scope: it needs a desktop to control, and we do not have
one.

## What we are not going to build, and why

* **No fork, no copied code.** MIT would allow it. The concepts differ, and
  the concept is the product.
* **No `docker.sock` for the agent.** The agent may not drive the daemon. The
  executor is a service we run, not a container the agent spawns.
* **No `CAP_SYS_ADMIN`, no privileged init container, no `allow_private_urls`.**
  Their stand runs on a LAN with trusted agents; ours does not.
* **No unattended `allow_force_pushes`-style escapes.** Every new tool is
  fenced like the existing ones, and every fence has a mutation that must go
  red.

## Open questions for the operator

1. **Wave B — the separation.** Two options: (a) a separate executor service,
   which is a topology change and costs a release of its own; or (b) keep one
   container and accept that the agent's files and the executor share a
   filesystem. Option (a) is the structural version of "the workspace is
   evidence, not instructions"; option (b) leaves it a convention. We
   recommend (a), and we want the decision on the record before the work
   starts.
2. **Retrieval, not research.** May the agent fetch a URL it was given? The
   research capability stays out of this project (v2.13.0), so the question is
   narrower: a fetch that refuses private address space, has a size cap and a
   content-type allowlist, with no index to query and no way to discover
   addresses. If the answer is no, wave C is vision only, and the agent asks
   the operator for anything it cannot read.
3. **Wave F — the browser.** In, or not? If in, the executor hosts it and it
   gets no credentials of ours. Note that a browser without a network is a
   browser that can only read what we hand it, so this question is really the
   retrieval question again.
4. **Order.** We propose A, then C (eyes and ears are what an agent actually
   feels missing), then D, then B, then E. B is last among the must-haves
   because it is the only wave that changes how the product is deployed.

## How this document stays honest

The gaps above were measured, not read off a feature list. When a wave lands,
this document moves the item from "the gaps" to "shipped", with the release
that shipped it and the mutation that proves the fence is red. A gap that
cannot be made red is decoration, and the wave does not land without one.
