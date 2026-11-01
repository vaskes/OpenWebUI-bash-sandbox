# Roadmap: the gaps against the other sandbox

**Measured 2026-11-01** on the Hermes stand (`nousresearch/hermes-agent:main-desktop`,
workstation) and on this repository. Every number below is a measurement, not a
summary of somebody else's documentation.

**Revised the same day, after the first draft was found to be wrong.** Two things
changed. The operator decided that the browser goes in (see *Decisions on the
record*), and the first draft of this document claimed that the jail has no way
to the internet. That claim was false, and false in the direction that matters:
the jail has **unrestricted** outbound access, measured from inside the container
on 2026-10-29 — the operator's own SSH host, the Gitea instance and the network's
router were all reachable (`docs/SECURITY.md`, §1 and *"The egress the jail has,
measured from inside the sandbox"*). The gap is not the network. The gap is the
absence of a **bound** on what the network may be used for.

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

## Decisions on the record

Decisions that are not ours to make again.

* **The browser goes in.** 2026-11-01, operator. A universal sandbox needs its
  own browser and its own wrappers; the split that exists today is an artefact
  of the parent bot's setup, not a design. Playwright driving Chromium, in our
  own image, wrapped by our own MCP tools, written here — no code from the other
  sandbox. See wave F.
* **The research capability stays out.** v2.13.0. It is another MCP's job, on
  another host. The browser wave does not reopen it: we ship no search tool, no
  index and no engine to query. A browser can be pointed at a URL the operator
  names; that is retrieval, and retrieval is a different thing.

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
egress   unrestricted — measured from inside the sandbox on 2026-10-29: the
         operator's SSH host, the Gitea instance and the router were all
         reachable; the image ships curl and requests/httpx, so the model
         can already reach whatever the jail can reach
image    bash tmux jq ripgrep fd-find tree htop, numpy/matplotlib/pandas/
         sqlalchemy/openai/requests/httpx/pillow/sympy/pytest/tqdm + wheelhouse
```

## The gaps, ordered by how much they cost us

**1. The agent cannot write or search files through the tools it was given.**
— **shipped in v2.22.0**; see *Shipped* below. The gap was this: the HTTP side
has `POST /files` and `DELETE /files`, but the MCP surface — the part the model
actually holds — offered no write and no search. The model therefore wrote
files through `bash` (`cat > …`), which is exactly the path that bypasses the
file fence: the audit trail of the file tools was empty while the model was
busy writing. This was the cheapest gap to close and the most embarrassing one
to have.

**2. The executor and the agent are the same container.** Our `exec` runs in
tmux inside the agent's own container. Whatever the model wrote into the
workspace is sitting right there for the executor, and whatever the executor
wrote is sitting right there for the model. Their design separates the two;
ours does not, and the separation is what makes "the workspace is evidence, not
instructions" a structural property instead of a convention.

**3. The jail has the internet, and nothing bounds what the internet is used
for.** This gap was written down wrong the first time. The jail's outbound
access is **unrestricted**: a plain bridge network with no `internal: true`, a
container image that ships `curl` and `requests`/`httpx`, and a measurement
from inside the sandbox on 2026-10-29 that reached the operator's SSH host, the
Gitea instance and the router. The model can already fetch anything the jail
can fetch — through `bash`, which is the one path that leaves no trace in the
session record. What is missing is not reach and not a way to the internet; it
is a **bound**: a retrieval tool with an operator's allowlist, private address
space refused, a size cap, a content-type allowlist, and a line in the session
record. The research capability that v2.13.0 removed is still not ours, and it
does not come back with wave F. The question worth asking is therefore not
whether the agent may fetch a URL it was given — it already can — but whether
the stack should keep that reach unbounded, and where the bound belongs. That
is question 2 below.

**4. No eyes.** No `vision_analyze`. The model cannot read a screenshot, a
figure or a photo — even though our own stack runs a vision model (Strata) and
the image has `pillow`.

**5. No planning, memory, recall or clarification.** `todo_list`, `memory`,
`session_search`, `clarify` are all absent. We already store sessions, so
recall is mostly a query we have not written; the rest is missing outright.

**6. No skills.** `skills_list`, `skill_view`, `skill_manage`. We have a
`CONTRIBUTING.md` and a `docs/` tree, but no mechanism that loads a recipe into a
session when the task calls for it.

**7. No code execution as a tool, no sub-agents.** `execute_code` (Python
calling our own tools programmatically, fewer model round trips) and
`delegate_task` (sub-agent with its own context) are both missing.

**8. No cron from inside the agent.** Scheduling exists on the platform side,
but the agent cannot schedule its own work.

**9. No browser, no desktop control.** `browser_exec` (Python over CDP),
`browser_cdp`, `browser_dialog`, `browser_vault`, `computer_use`. The browser
is decided: it goes in, in wave F. `computer_use` (control of a desktop) is out
of scope: it needs a desktop to control, and we do not have one.

**10. No background process management as a tool.** tmux sessions exist and
the agent drives them through `bash`; `process_manage` would make that
first-class and auditable.

## Shipped

A wave that landed, with the release that shipped it and the mutation that
proves its fence is red.

### Wave A — the agent writes and searches through the fence — v2.22.0

The MCP surface grew three tools: `write_file`, `patch` and `search_files`.
They are inside the workspace, bounded, counted, and refused with a reason —
and each one is a **narrower** path than the shell it stands in for.

*Red:* `tools/mutation-check-v2220.sh` breaks every fence above on purpose
and expects the named test to go red.

**The first version of this plan was wrong about what already existed**, and
the correction is worth more than the wave: it said "the HTTP side of all three
already exists and is already tested". Measuring the routes instead of reading
the document showed that the HTTP door has `POST /files` — a write, with a
*different* policy, since delivery never overwrites — and neither a patch nor
a search. So wave A was not a thin layer over code we own: the **fence** was
code we own, and the three operations on top of it were written for this wave.

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

Why first: the *fence* is code we own — the file door's own containment
checks, its own limits, its own atomic write — so the three operations land on
a perimeter that is already measured. And it closes the "the model writes
through bash" hole, which is the one that makes our own audit trail
incomplete.

**Shipped in v2.22.0** — see *Shipped* above. The claim this section used to
make about the HTTP side was false; the correction is recorded there.

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
option below. Wave F depends on the same shape — a service of its own, in a
container of its own — so B should land before F rather than beside it.

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

### Wave F — the browser, in the sandbox

**Decided: it goes in.** The split that exists today is an artefact of the
parent bot carrying a browser and a search tool of its own; a universal
sandbox has to stand alone. So: Playwright driving Chromium, in our own image,
behind our own MCP wrappers, written here. No code from the other sandbox.

**It needs a container of its own, and that is forced by two measured facts
about our own stack — not by taste.**

* The model's shell runs in the same container, in the same uid, as everything
  else in it. A browser whose debugging port is reachable on localhost is
  reachable by the model: `curl http://127.0.0.1:9222/json` is all it takes to
  drive the browser without our wrapper and without our policy.
* The agent's own token is readable out of `/proc/<pid>/environ` of the agent's
  process (measured 2026-10-29). A door authenticated by a token the agent
  holds is therefore not a door the model is stopped by.

A separate container with its own network is the only arrangement in which the
browser is reachable by our wrapper and by nothing else. This is the same
shape as wave B, and it is why B should land first.

**The policy has to live in the browser, not in the wrapper.** If the
allowlist, the size caps and the credential rules are enforced only by our MCP
tool, the model bypasses all of them by talking to the browser directly. So
the browser service enforces them itself: our wrapper is a convenience, the
fence is the service. *Red:* a URL outside the operator's allowlist is refused
by the browser service itself, not by the tool that called it.

**No credentials of ours, ever.** The profile is a credential store the moment
a session logs in anywhere. It lives in tmpfs, per session, and it never lives
in the workspace. The v2.20.0 rule applies to it as much as to anything else:
the workspace is attacker-controlled, and a browser profile is a set of files the
browser *trusts*, so a planted profile is code the browser will run. The
profile is ephemeral, and it is outside the workspace.

**Chromium's own sandbox is the thing to measure, not to assume.** A renderer
compromise is the browser's worst case, and Chromium's answer to it is its own
sandbox — which under `cap_drop ALL`, `no-new-privileges` and a read-only rootfs
may not be available at all, in which case the browser runs with
`--no-sandbox` and a renderer bug becomes code execution in the browser's
container. That is the reason the browser is a container of its own with
nothing in it worth stealing, and the reason these have to be measured before
the wave lands rather than guessed at:

* does Chromium start under our posture, and does it need `--no-sandbox`?
* what does it cost in image size, in resident memory, and in processes — the
  jail's budget is `pids_limit` 256 and 2 GB (4 GB in production)?
* how long is a cold start, and does an MCP call have to wait for it?

*Red:* the browser's own policy refuses an address outside the allowlist; a
page cannot write outside the workspace; the profile directory is not inside
the workspace; no cookie from any session outlives the session; the browser
container holds none of our credentials.

**The tools, in our style** (`browser_` prefix, ours, not theirs):

* `browser_open` — navigate to a URL and return the rendered text, bounded.
* `browser_screenshot` — a PNG of the current page, into the workspace through
  the same fence as wave A's write.
* `browser_close` — drop the context and the profile.

**What the browser does not change.** It does not give the model reach it did
not have: the jail's egress was already unrestricted, and that is measured, not
assumed. What it adds is a **renderer** for content the model did not write,
and — if we ever let it be one — a **logged-in client**. The second is why the
credential rules are not negotiable, and why the browser gets its own egress
allowlist even though the model keeps its unrestricted `curl`: the allowlist
bounds the *blast radius of a compromised renderer*, not the model's reach.

**And what it does not bring back.** The research capability that v2.13.0
removed stays removed. We ship no search tool, no index and no engine to query.
A browser pointed at a URL the operator names is retrieval, and retrieval is
the subject of question 2 — not a research capability in disguise.

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
* **No credentials of ours in a browser profile, ever.** Not a cookie, not a
  token, not a saved password — not even for one page.
* **No desktop control.** `computer_use` needs a desktop, and there is none.

## Open questions for the operator

1. **Wave B — the separation.** Two options: (a) a separate executor service,
   which is a topology change and costs a release of its own; or (b) keep one
   container and accept that the agent's files and the executor share a
   filesystem. Option (a) is the structural version of "the workspace is
   evidence, not instructions"; option (b) leaves it a convention. We
   recommend (a), and we want the decision on the record before the work
   starts. Wave F needs the same shape, so this answer also decides where the
   browser lives.
2. **The bound on reach.** The jail's reach is unrestricted today, and that is
   measured. The browser makes the question sharper, not new: does the browser
   service get its own egress allowlist — narrowing the blast radius of a
   compromised renderer — while the model keeps its unrestricted `curl`? And
   does the jail's own unrestricted egress stay, or does it become the thing
   we narrow? If it stays, then wave F's allowlist is a policy about one tool
   and not a boundary, and this document has to say so rather than imply
   otherwise.
3. **Order.** We propose A, then C (eyes and ears are what an agent actually
   feels missing), then D, then B, then F, then E. B comes before F because
   the browser needs a container of its own and building that shape twice would
   be waste; F comes before E because the browser is the wave the operator has
   already asked for. The operator may reorder.

## How this document stays honest

The gaps above were measured, not read off a feature list. When a wave lands,
this document moves the item from "the gaps" to "shipped", with the release
that shipped it and the mutation that proves the fence is red. A gap that
cannot be made red is decoration, and the wave does not land without one.

The claims this document makes about *our own* stack are checked by a
tripwire, because the first version of this file was wrong about the network:
the egress line above is compared against the compose files and the image, and
the browser wave is compared against the decision it records. A mutation
script breaks each of them on purpose and expects the named test to go red.
