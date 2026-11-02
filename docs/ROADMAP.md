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

**Revised again on 2026-11-02, after the operator settled the topology.** The
sandbox is one atomic service of one Docker, and the browser goes in *that*
container — not in a container of its own. The previous revision of this
document argued for the opposite, on the grounds that the browser's debugging
channel is reachable by the model. That argument was partly wrong and partly
right, and both halves were measured on 2026-11-02; the corrections are in
*Wave F* below, and the operator's decision is in *Decisions on the record*.

**What this document is.** The other sandbox gives its agent a wide surface: a
terminal in a separate container, a browser, file writes, planning, memory, code
execution, sub-agents, cron, a desktop. We give ours twelve MCP tools and a
jail. The question this document answers is *what to build ourselves* to close
the gaps that matter, and *what to refuse to build*.

**What this document is not.** No code was copied. The other sandbox is MIT
licensed, and a fork would have been the cheap way to get the surface — that is
precisely why it is the wrong way here. Their sandbox is a workstation for a
trusted operator's agent on a LAN; ours is a jail for an agent we do not trust.
The security concept is the product, and a fork would import a different one.
Everything below is to be written here, under our rules.

## Decisions on the record

Decisions that are not ours to make again.

* **The sandbox is one container.** 2026-11-02, operator. The sandbox is an
  atomic service of one Docker: the agent, the model, and the browser live in
  one container, and the executor is that container's own shell. A separate
  executor service and a separate browser container are both off the table.
  The operator's words: it looks like overdesign; the sandbox is an atomic
  service of one Docker, and it can and should be simpler and live in one
  container. The cost of that call is real and is measured below — it is not
  free, and this document says what it costs rather than hiding it behind the
  word "simpler".
* **The browser goes in.** 2026-11-01, operator. A universal sandbox needs its
  own browser and its own wrappers; the split that exists today is an artefact
  of the parent bot's setup, not a design. Playwright driving Chromium, in our
  own image, wrapped by our own MCP tools, written here — no code from the other
  sandbox. See wave F.
* **The research capability stays out.** v2.13.0. It is another MCP's job, on
  another host. The browser wave does not reopen it: we ship no search tool, no
  index and no engine to query. A browser can be pointed at a URL the operator
  names; that is retrieval, and retrieval is a different thing.
* **One agent, no sub-agents, no code execution as a tool.** 2026-11-03,
  operator. `execute_code` (Python calling our own tools programmatically) and
  `delegate_task` (a sub-agent with its own context) are out of scope for this
  project and are not built: this is a single-operator, single-agent sandbox. The
  operator's words: this is out of scope for our project — single operator,
  single agent. See wave E.

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

### Ours: 12 tools, and the jail around them

```
MCP      bash, read_file, file_info, delete_file, write_file, patch,
         search_files, vision_analyze, todo_list, memory, session_search,
         clarify
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

**2. The executor and the agent are the same container.** — **closed by decision
on 2026-11-02, not by build.** Our `exec` runs in tmux inside the agent's own
container, so whatever the model wrote into the workspace is sitting right
there for the executor, and whatever the executor wrote is sitting right there
for the model. Their design separates the two; ours does not, and the
separation is what makes "the workspace is evidence, not instructions" a
structural property instead of a convention.

The operator closed this gap by choosing the simpler topology: one container,
one uid, one filesystem. The price is paid in the one place it can be paid —
the model and its executor share a world — and the two things that shared
world could take from each other were measured on 2026-11-02, not assumed:

* the model's shell and the agent run as **the same uid (1000)**, so the model
  can read the agent's own environment out of `/proc/<pid>/environ` — measured:
  a same-uid sibling reads another process's `AGENT_TOKEN` without privilege
  (the agent's inbound token is therefore not a secret from the model, and the
  HTTP door's token is a door the model can already open);
* the model's shell can attach to a process of the same uid and take its file
  descriptors — measured: `PTRACE_ATTACH` succeeds and `pidfd_getfd` hands over
  the descriptor (see wave F, where this is the measured consequence that
  decides the browser's shape).

What the decision does **not** change: the file fence. The workspace is still
attacker-controlled, the agent's tools still refuse to hand it to an
interpreter, and the audit trail of our own tools is still ours. The rule
"the workspace is evidence, not instructions" becomes a **convention enforced
by our code**, not a property of the topology. That is the trade, and it is the
operator's to make.

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

**7. No code execution as a tool, no sub-agents.** — **closed by decision on
2026-11-03, not by build.** `execute_code` (Python calling our own tools
programmatically, fewer model round trips) and `delegate_task` (sub-agent with
its own context) are both missing, and both stay missing: this project is a
single-operator, single-agent sandbox, and a second agent inside it is not
something this project is for. The operator's words: this is out of scope for
our project — single operator, single agent. See wave E.

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

### Wave C — eyes — v2.23.0

The MCP surface grew one more tool: `vision_analyze`. It takes a picture
that is **already in the workspace**, hands it to the **operator's** own
vision endpoint, and returns the answer as text.

Two things are fixed, and neither is the model's to choose: the picture
is read through the file fence every other file operation uses, and the
endpoint is named by `AGENT_VISION_URL`. The door is closed while that
is empty — the refusal says so. The tool takes a path and **not a URL**,
because a door that fetches a URL the model names is a retrieval door
wearing a different hat, and retrieval is not ours (v2.13.0).

The picture, the question and the answer are each bounded, and a
truncated answer says that it is truncated.

*Red:* `tools/mutation-check-v2230.sh` breaks every fence above on
purpose and expects the named test to go red.

**This is the one tool that is not narrower than the shell.** It is a
capability the operator opens on purpose and closes again by emptying
the knob — so the sentence "the MCP door grants nothing the HTTP door
does not" now has exactly one exception, and the exception is named.

### Wave D — planning, memory, recall, clarification — v2.24.0

Four more tools on the MCP door: `todo_list`, `memory`, `session_search`
and `clarify`. The model's own state — a plan it can rewrite, notes it
can keep, a recall of what it ran, and a question it can put to the
operator — with the boundaries the rest of the project keeps.

* The **task list** is replaced, not merged, and bounded.
* The **memory** is a file (`.agent-memory.json`) the operator can read
  and delete — not a hidden profile of the operator.
* The **recall** searches the commands a session ran, with literal text
  and a bounded answer.
* The **question** goes back to the caller.

**The question needed a measurement.** The protocol's own way for a
server to ask the client something — the MCP elicitation — needs a
back-channel for a request the *server* starts, and the stateless
Streamable HTTP transport has none: the SDK answers "this transport
context has no back-channel for server-initiated requests". So the
channel is the caller: the question goes back in the tool's answer, and
the operator's answer comes with the next message. The stateless door
is a deliberate property of this deployment (see `docs/HANDOFF-mcp.md`),
and the question is shaped by it rather than by wishful thinking.

**A note is context, never permission.** The memory is the model's own
file, and the tool's description says what that means: only the
operator's words authorize an action. The test pins the sentence.

*Red:* `tools/mutation-check-v2240.sh` breaks every fence above on
purpose and expects the named test to go red.

### Wave F — the browser, in the same container — v2.25.0

The browser came **into this container**: Chromium and Playwright are in
the image, and three tools on the MCP door drive them — `browser_open`,
`browser_screenshot` and `browser_close`.

* The **hosts** the tool may fetch are the operator's
  (`AGENT_BROWSER_ALLOWED_HOSTS`), and the door is **closed** while that
  list is empty.
* A page that **redirects off the list** is refused, and its content is
  discarded. That check exists because of the measurement below: a route
  handler alone is not a fence.
* The **screenshot** lands in the workspace through the same fence every
  other write uses.
* The browser keeps **no credentials**: a fresh context per session, and
  nothing on disk but the tmpfs the browser itself uses.

*Red:* `tools/mutation-check-v2250.sh` breaks every fence above on
purpose and expects the named test to go red — 20 mutations, all caught.

**The measurement that shaped the implementation.** Chromium follows an
HTTP redirect inside its own network stack and never hands that
follow-up to the route handler: measured, a page on an allowed host
redirected to a host that was not on the list, and the browser walked
there while the handler never saw it. A route handler is therefore not a
fence on its own — so the door checks the host **again** after the
navigation, and a page that ended up elsewhere is refused. The
implementation follows the measurement, not the other way round.

**The cost, stated rather than hidden.** In the jail Chromium's own
sandbox is not available — the setuid helper is inert under
`no-new-privileges`, and the namespace backend needs user namespaces the
seccomp filter refuses — so the browser runs with `--no-sandbox` there,
and a renderer compromise in the jail is code execution as uid 1000: the
same uid the model's own shell runs as. Where the namespace backend *is*
available, the browser keeps its own sandbox, and the tool says which
posture it got.

## The waves

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

### Wave B — not built, by decision

**This wave was dropped on 2026-11-02.** The operator decided that the sandbox
is one atomic service of one Docker, and that the executor is that container's
own shell rather than a separate service reached over HTTP with its own token.
The separate-executor shape is therefore not on the roadmap, and nothing below
describes a container of its own.

The reasoning is worth keeping, because the shape it proposed was not
arbitrary: a separate executor would have given the model a filesystem it could
not see, and the agent's secrets a process the model could not read. Both of
those properties are gone by decision, and the cost was measured rather than
assumed — see gap 2, where the same-uid facts that replace them are recorded.

What remains of the idea, and it is not nothing:

* the agent's **own state** (`.agent-sessions.json`, `.agent-memory.json`)
  still lives in the workspace, which the model owns. Moving it out of the
  model's directory is a cheap change with the same effect on the state file,
  and it is worth doing on its own account — not because the executor splits.
* the model can read the agent's own token out of `/proc/<pid>/environ`,
  because both run as uid 1000. A separate executor would have fixed that for
  the executor's world only; the agent's token is the agent's own door, and the
  model's shell is already inside the agent's container.

*Red:* none — this wave does not land, so there is no fence to make red. The
property it proposed is instead recorded as a measured fact, and the tripwire
that guards the *shape* of the decision is `tests/test_roadmap_drift.py`
(§`TestTheOneContainerDecision`).

### Wave C — eyes

* `vision_analyze` — the image is handed to the vision model we already run
  (Strata on llmhost1), and the answer comes back as text. The agent never
  gets a raw model call of its own; it gets a sandboxed tool that talks to
  one endpoint. *Red:* the tool refuses a file outside the workspace; the
  answer is bounded in size.

Retrieval of a URL the operator named is deliberately **not** in this wave: the
research capability is not this project's (v2.13.0), and retrieval is close
enough to it that it deserves its own decision, not a footnote.

**Shipped in v2.23.0** — see *Shipped* above. The tool takes a path
inside the workspace and not a URL, the endpoint is the operator's, and
the door is closed while `AGENT_VISION_URL` is empty.

### Wave D — planning, memory, recall, clarification

* `todo_list` — the task list lives with the session, is bounded in size, and
  is reported back in the session record.
* `memory` — a small, explicit, human-readable store the operator can read and
  delete. Not a hidden profile: the file is a file.
* `session_search` — a query over our own sessions, which we already keep.
* `clarify` — the agent can ask the operator a question through the channel it
  is talking on.

**Shipped in v2.24.0** — see *Shipped* above. The recall needed one
thing the plan did not mention: a query over the sessions is only
interesting if the sessions remember what they ran, so the commands are
remembered (in memory, bounded, and gone with the session).

### Wave E — not built, by decision

**This wave was dropped on 2026-11-03.** The operator's words: this is out of
scope for our project — single operator, single agent. `execute_code` and
`delegate_task` are therefore not on the roadmap, and nothing below describes a
second agent or a Python layer over our tools.

The reasoning is short and it is about the project, not about the capability.
This is a **single-operator, single-agent** sandbox: one operator, one agent,
one conversation at a time, one workspace. A sub-agent with its own context is a
different product — a fleet of agents is what it is for, and this is not one.
`execute_code` is the same thing seen from the other side: a second way for the
model to reach the tools, where the model already has `bash`, the file tools and
the browser, each of them fenced and counted. What it would add is a *second
front door* to the same fenced surface, and a fence that has to be right twice
is worth less than one fence that is right once.

What remains of the idea, and it is not nothing:

* **fewer round trips** are still worth having, and they are still reachable
  without a second agent: the model's own shell can call our tools over HTTP,
  the way the operator's own bot does — that path is the operator's, not the
  model's, and the model's tools stay the model's;
* **scale** is not a question for this project. The sandbox is one operator's
  sandbox.

*Red:* none — this wave does not land, so there is no fence to make red. The
property it proposed is instead recorded as a decision, and the tripwire that
guards it is `tests/test_roadmap_drift.py` (§`TestTheSingleAgentDecision`).

### Wave F — the browser, in the same container

**Decided: it goes in.** The split that exists today is an artefact of the
parent bot carrying a browser and a search tool of its own; a universal
sandbox has to stand alone. So: Playwright driving Chromium, in our own image,
behind our own MCP wrappers, written here. No code from the other sandbox.

**It goes into the sandbox's own container — the same one the model lives in.**
This reverses the argument the previous revision of this document made. That
revision claimed two measured facts forced a container of its own:

> *A browser whose debugging port is reachable on localhost is reachable by the
> model: a single `curl` of the devtools endpoint is all it takes.*

**That claim was false for our own implementation, and the measurement is
what replaced it.** Playwright launches Chromium with `--remote-debugging-pipe`
— a socketpair, not a TCP port. Measured on 2026-11-02: the browser process
carries `--remote-debugging-pipe` in its argv, the port flag is absent, and
the process opens **zero** listening TCP sockets. There is no devtools
endpoint on localhost to curl. A reader who took the old paragraph at face
value would have built a container boundary against a port that does not
exist.

The second old claim survives: the agent's own token is readable out of
`/proc/<pid>/environ` (measured 2026-10-29, and re-measured on 2026-11-02 as a
same-uid read with no privilege needed). But in one container that is not a
reason for a separate browser container: the model's shell is *already* inside
the agent's container and *already* reads that token. Splitting the browser out
would not have hidden it.

**And the honest reason to know all this: the browser is not a fence against
the model, and one container makes that plainer rather than worse.** Measured
on 2026-11-02, as uid 1000 — the model's own uid in the jail:

* a same-uid sibling **can attach** to the browser's driver process
  (`PTRACE_ATTACH` succeeds) and **can duplicate its file descriptors**
  (`pidfd_getfd` hands over the socket);
* having done that, it **can inject a CDP command and read the answer** — the
  hijack returned a live `Target.getTargets` result.

So a model that wants to drive the browser without our wrapper can. It does not
need to attack anything: it runs in the same container, under the same uid,
with the same reach the browser has. The old document's conclusion — "the
policy has to live in the browser, not in the wrapper" — was aiming at a
boundary that does not exist in either topology. What the browser's own policy
*does* bound is the **damage a compromised page can do**, not the model. That
is a real and worth-while property, and it is the one wave F actually buys.

**Therefore the fences are ours to keep, in our code, and they are:**

* **the URL allowlist lives in the wrapper**, and the wrapper is what the
  operator talks to. It bounds what our tool will fetch on request. It is not a
  boundary against the model — the model can reach the open web with `curl`
  today, and the measured hijack above means it can reach the browser too.
* **no credentials of ours, ever.** The profile is a credential store the
  moment a session logs in anywhere. It lives in tmpfs, per session, and it
  never lives in the workspace. The v2.20.0 rule applies to it as much as to
  anything else: the workspace is attacker-controlled, and a browser profile is
  a set of files the browser *trusts*, so a planted profile is code the browser
  will run. The profile is ephemeral, and it is outside the workspace.
* **the size and time caps** on the answer, so a page cannot flood the model's
  context or the session record.

**Chromium's own sandbox is the thing to measure, and it was measured.**
Chromium has two sandbox backends and our posture allows neither in the real
jail:

* the **setuid** backend needs the `chrome_sandbox` helper to be setuid-root —
  it is shipped mode `0755`, and `no-new-privileges` makes the setuid bit
  inert anyway;
* the **namespace** backend needs unprivileged user namespaces. On the
  development host `unshare -Ur` succeeds as uid 1000
  (`unprivileged_userns_clone=1`), and under it Chromium's renderer lands in
  its own user namespace with a seccomp filter — measured, the renderer
  processes carry `seccomp=2` in a namespace separate from the browser's. On
  the **production** host the same command is `EPERM` — the seccomp filter is
  active (`docs/SECURITY.md`, §"The container boundary, as measured on the
  production host").

So on production Chromium will run with `--no-sandbox`, because with neither
backend available it refuses to start otherwise — measured: forcing the
namespace backend off yields *"Chromium sandboxing failed!"*. The consequence
has to be stated plainly: **a renderer compromise on production is code
execution as uid 1000 inside the jail.** That is the same uid the model's own
shell runs as, so it grants the attacker nothing the model does not already
have — and it grants it to whoever can get a malicious page rendered. That is
the real cost of wave F, and it is why the profile rules are not negotiable.

**What it costs in resources** (measured 2026-11-02, one page, uid 1000, as
shipped): Chromium adds **5–6 processes** (against the jail's `pids_limit` 256),
**~140–190 MB PSS** (~430 MB RSS summed over processes, which double-counts
shared pages) against `mem_limit` of 2 GB (dev) / 4 GB (production), a
**~1.2 s cold start** and a **~0.15–0.6 s warm navigation**, and **403 MB** of
browser in the image. The pids and memory fit; the cold start is what an MCP
call has to wait for, and a warm browser is therefore worth keeping between
calls.

**The image carries one Chromium, not two.** `playwright install chromium`
also pulls the *headless shell* — a second Chromium, 273 MB — which this image
does not use: the browser runs headless through the full build. The build
passes `--no-shell`, so the browser in the image is 403 MB instead of 681 MB.

**And the driver has to be told which Chromium it wants.** Measured in the
driver's own source: the executable name is
`options.headless ? "chromium-headless-shell" : "chromium"` when no channel
is given — so Playwright's headless mode reaches for the *shell*, and the
shell is exactly what `--no-shell` left out. The door therefore launches with
`channel="chromium"`, which is the full build in headless mode. Without it
the browser in the image does not exist, and the tool answers
*"Executable doesn't exist at /ms-playwright/chromium_headless_shell-…"* —
measured, in the CI job that builds the image and opens a page in it.

**And Chromium needs a writable home it did not ask for.** It takes its home
directory from the passwd entry (`getpwuid()`) and **ignores `HOME`**; in
this container that home is `/home/agent` on a read-only rootfs, so the
crashpad handler cannot create its database and the browser dies at launch
with `SIGTRAP` and *"chrome_crashpad_handler: --database is required"*.
The fix is Chromium's own: writable, pre-created `XDG_CONFIG_HOME` and
`XDG_CACHE_HOME` on the tmpfs.

**The browser does not inherit the agent's environment.** Playwright's `env`
*replaces* the environment instead of adding to it, and the door uses that
on purpose: the agent's token is in the agent's environment, and a same-uid
sibling can read `/proc/<pid>/environ` — so a browser that inherited it
would carry the agent's credential in a file the model can read.

**The tools, in our style** (`browser_` prefix, ours, not theirs):

* `browser_open` — navigate to a URL and return the rendered text, bounded.
* `browser_screenshot` — a PNG of the current page, into the workspace through
  the same fence as wave A's write.
* `browser_close` — drop the context and the profile.

**What the browser does not change.** It does not give the model reach it did
not have: the jail's egress was already unrestricted, and that is measured, not
assumed. What it adds is a **renderer for content the model did not write** —
and with the sandbox unavailable on production, a renderer that can be made to
execute code in the jail. That second half is why the credential rules are not
negotiable, and why the profile is per-session and outside the workspace.

**And what it does not bring back.** The research capability that v2.13.0
removed stays removed. We ship no search tool, no index and no engine to query.
A browser pointed at a URL the operator names is retrieval, and retrieval is
the subject of question 2 — not a research capability in disguise.

**Shipped in v2.25.0** — see *Shipped* above. The implementation follows the
measurements rather than the plan: the allowlist is checked before the browser
is reached **and again after the navigation**, because Chromium follows a
redirect inside its own network stack and the route handler never sees the
follow-up. The door is closed while `AGENT_BROWSER_ALLOWED_HOSTS` is empty,
the screenshot goes through wave A's fence, and the browser keeps no
credentials — a fresh context per session, and nothing on disk but the tmpfs
the browser itself uses.

## What we are not going to build, and why

* **No fork, no copied code.** MIT would allow it. The concepts differ, and
  the concept is the product.
* **No `docker.sock` for the agent.** The agent may not drive the daemon, and
  it does not spawn containers of its own — not for the executor, not for the
  browser.
* **No second container.** One atomic service of one Docker, by the operator's
  decision of 2026-11-02. The executor is the sandbox's own shell and the
  browser is a process inside the sandbox's own container.
* **No `CAP_SYS_ADMIN`, no privileged init container, no `allow_private_urls`.**
  Their stand runs on a LAN with trusted agents; ours does not.
* **No unattended `allow_force_pushes`-style escapes.** Every new tool is
  fenced like the existing ones, and every fence has a mutation that must go
  red.
* **No credentials of ours in a browser profile, ever.** Not a cookie, not a
  token, not a saved password — not even for one page.
* **No desktop control.** `computer_use` needs a desktop, and there is none.

## Open questions for the operator

1. **The bound on reach.** The jail's reach is unrestricted today, and that is
   measured. The browser makes the question sharper, not new: does the whole
   jail's egress stay as it is, or does the stack narrow it? In one container
   the browser and the model share the network, so an allowlist that covers
   only the browser would narrow the *tool* and not the *jail* — and the model
   can reach the browser without the wrapper anyway (measured). If it stays,
   then wave F's allowlist is a policy about one tool and not a boundary, and
   this document has to say so rather than imply otherwise. It says so above.
2. **Order.** There is no wave left to order: A, C, D and F are shipped, and B
   and E are not built, by decision. What the order question was about — F
   before E — is settled by the fact that E does not come.
3. **The agent's own state file.** Moving `.agent-sessions.json` and
   `.agent-memory.json` out of the model's directory is cheap and does not
   need a second container. Whether it is worth a release of its own is the
   operator's call — it was left in place here because it was not asked for.

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

The topology is pinned too, because a decision that is not pinned becomes a
suggestion: `tests/test_roadmap_drift.py` checks that the document still
records the one-container decision, that wave B still reads as *not built*,
and that the browser wave still says the browser lives in the same container.
The same script mutates each of those on purpose.

And the second revision of this file was wrong in the other direction — it
claimed a debugging port that our own implementation does not open. That is
why the browser wave now carries its measurements inline: the port claim, the
same-uid hijack, the sandbox backends, the resource cost. A plan built on a
guessed fact is a plan built on a wrong fact, and the operator has to be able
to check the number rather than trust the paragraph.
