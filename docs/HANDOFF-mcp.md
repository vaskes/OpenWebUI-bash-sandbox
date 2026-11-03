# Handoff — the sandbox as an MCP server

This is the contract for whoever drives the sandbox over the Model
Context Protocol: a telegram harness, an Open WebUI instance, a
llama.cpp-style caller, anything that speaks MCP.

The point of the document is that **the MCP door grants nothing the
HTTP door does not**. The containment is the same containment, and the
bearer token is the same gate. Nothing is reachable over MCP that is not
reachable over HTTP.

Three of the tools — `write_file`, `patch`, `search_files` — have no
HTTP twin, and that is deliberate. The HTTP door's own `bash` door
already allows everything they do; what these three add is a **narrower
path** than the shell: inside the workspace, bounded, counted, and
refused with a reason. A model that writes through `bash` leaves the
file door's own accounting empty, and the fence it walks past is the
fence these tools enforce.

There is **one** exception, and it is named: `vision_analyze` hands a
picture to the operator's own vision endpoint. That is a capability the
shell does not have, so it is the operator's to grant — the endpoint is
named by `AGENT_VISION_URL`, the door is closed while that is empty, and
the tool takes a path inside the workspace and **not a URL**.

The browser tools are the same kind of exception, and for the same
reason. `browser_open`, `browser_screenshot` and `browser_close` drive a
browser the shell does not have — Chromium, in this container — so what
they may reach is the operator's to grant: the hosts are named by
`AGENT_BROWSER_ALLOWED_HOSTS`, and the door is closed while that is
empty. `browser_open` takes a URL, because a browser that cannot be
told where to go is not a browser; what it may fetch is the operator's
list, not the model's reach.

---

## Connecting

| | |
|---|---|
| Transport | MCP Streamable HTTP (the current spec revision) |
| Endpoint | `POST /mcp` on the **proxy** (published surface) or on the agent (internal network) |
| Auth | `Authorization: Bearer <token>` — the same token as the HTTP API |
| Sessions | the **transport** is stateless: the server keeps no session, every request stands on its own. The **sandbox** is not: the shell lives in a tmux session inside the agent, and a restart of the agent takes those sessions with it |
| Versions | the client gets the protocol version it offered, when the handshake knows it |

A handshake in plain JSON-RPC:

```bash
curl -sS -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
           "protocolVersion":"2025-06-18","capabilities":{},
           "clientInfo":{"name":"my-harness","version":"1"}}}' \
     http://127.0.0.1:8090/mcp
```

The answer carries `protocolVersion`, the server's capabilities and its
`serverInfo` (`openwebui-bash-sandbox`).

Two things a client has to get right:

* **`Accept`** must list both `application/json` and `text/event-stream`.
  The server answers JSON, but the client is required to accept both.
* **`Host`** — only if the operator turned the check on. Set
  `AGENT_MCP_ALLOWED_HOSTS` on the agent, and a request whose `Host`
  header is not on that list is answered with a **421**. By default the
  check is off (a containerised service is reached by its service name,
  and a list that does not contain that name would refuse every
  legitimate caller), and the bearer token is the guard.

With the reference SDK (`pip install mcp`):

```python
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
import httpx2

http_client = httpx2.AsyncClient(
    base_url="http://127.0.0.1:8090",
    headers={"Authorization": f"Bearer {TOKEN}"},
)
async with Client(
    streamable_http_client("http://127.0.0.1:8090/mcp", http_client=http_client)
) as client:
    tools = await client.list_tools()
    result = await client.call_tool("bash", {"command": "uname -a"})
```

---

## The tools

### `bash`

Runs a command in the sandbox workspace.

| Argument | Type | Meaning |
|---|---|---|
| `command` | string, **required** | the shell command |
| `session_id` | string | which shell to run it in; default `mcp` |
| `cwd` | string | the working directory, for a session that does not exist yet |
| `timeout` | integer | seconds to wait; the same 1..3600 range the HTTP API allows |

Returns `stdout`, `stderr`, `exit_code`, `timed_out`, `duration_ms`,
`command_started`, `session_id` and `cwd`.

**The session is the shell.** `cd` and `export` survive from one call
to the next, so a caller that wants two conversations apart passes a
different `session_id` — a telegram harness wants one session per chat.
`cwd` is used when the session is created; a session that already
exists keeps the directory it was created with, and the answer says
which one that was (`cwd` in the result). A session whose pane has
died — the container was restarted — comes back in the directory it
had: the registry remembers it, and the directory it was created with
is not silently replaced by the workspace root.

**The command's own text is the point.** `exit 3` ends the shell, the
pane dies with it, and the call waits out its timeout. Use
`sh -c 'exit 3'` to fail a command without killing the shell.

**A timeout does not stop the command.** When the command outruns its
budget the answer comes back with `timed_out: true` and the command
keeps running in the session — that is what a persistent session
means, and the only way to stop it is through the session. A caller
that wants the command gone has to say so (`pkill`, or a `^C` written
into the pane) in a second call.

**A busy session answers with a reason, not with the pane.** The session
is one shell, so while a command is still running the next call's input
waits in the terminal and the call runs out its timeout. The answer is
`stdout: ""` and a `stderr` that says the command did not start because
the session is busy — the command is queued and runs when the session is
free. Before v2.13.1 the answer carried the pane, and the pane held the
*previous* command's output: a model that read it quoted another
command's output as its own, and the notice about a lost beginning made
it look like a truncated result of the new command. The output of a
command that *did* run is cut at the command's own begin marker, so a
foreign session's output never reaches it.

**The answer says whether the command started.** Every exec answer
carries `command_started`: `true` when the command ran — its stdout and
its exit code are real — and `false` when it never started, because the
session was busy or the shell was stuck. Before v2.26.0 the two were
told apart only by the prose in `stderr`, and a caller that read the
fields could not: a queued command and a killed one both had an empty
stdout, no exit code and `timed_out: true`. A queued command is not a
failed command, and the field is what says so.

**The shell is still the shell.** `write_file`, `patch` and
`search_files` are the *checked* paths; `bash` can do all of it too, and
nothing stops it. What the tools add is the fence and the accounting,
not a restriction.

### `read_file`

Reads a file out of the workspace.

| Argument | Type | Meaning |
|---|---|---|
| `path` | string, **required** | the file, relative to the workspace or absolute inside it |

A text file comes back as text. Anything else comes back as an
embedded resource: base64 in the `blob` field, with the mime type. A
file over the read limit is an error that names its real size and the
limit.

**The mime type is guessed from the name**, not sniffed from the bytes —
there is no `libmagic` in the agent image. It is metadata for the
caller; the text-or-binary decision is made on the content (the bytes
are decoded as UTF-8, and a file that does not decode is served as a
blob), so a wrong mime never changes the bytes.

**The read has a budget of its own** (`AGENT_MCP_MAX_READ_BYTES`, 1 MiB
by default). The effective limit is the smallest of three: that
budget, the file policy the HTTP door applies (`AGENT_BINARY_LIMIT`) and
a hard ceiling of 64 MiB. The budget is the point: the HTTP door
serves a *file*, and the deployment lets it serve 50 MiB of one, but
here the bytes land in a **prompt** — and a text model asked to reason
about fifty megabytes has lost the thread. The HTTP door keeps its own
limit, untouched.

**The read holds the file in memory** and base64-encodes it on the way
out, so a binary file costs about a third more than its own size in
the answer. A file that grows while it is being read is refused with the
reason rather than read whole.

**A big file is read in pieces, not refused forever.** `read_file` has
no offset, so the way to read a slice of a large file is the shell:
`bash` with `head`, `tail` or `sed`.

### `file_info`

The cheap door: `name`, `path`, `size`, `mtime`, `mime` and the limit
that applies — which is the read limit above, not the HTTP door's file
policy. Use it before `read_file` when the size is the question.

### `delete_file`

Removes a file from the workspace. The workspace is a scratchpad, not
storage: once a file has been read out, the caller can remove it.

### `write_file`

Writes a text file into the workspace.

| Argument | Type | Meaning |
|---|---|---|
| `path` | string, **required** | the file, relative to the workspace or absolute inside it |
| `content` | string, **required** | the text to write |

Returns `name`, `path` and `size`.

**The write is atomic.** The bytes land in a temporary file in the
target's own directory and are moved onto the target with `os.replace`:
a reader never sees a half-written file under the target's name.

**The write replaces the file that is there.** This is the model's
scratchpad, not storage — and the model could delete the file with the
shell anyway. The *delivery* door (`POST /files`) keeps its
no-overwrite rule, because there the caller is handing a file in and
delivery does not remove.

**The parent directory is made if it is missing.** Otherwise the first
write of every task needs a shell command first — and the workaround is
the unfenced path.

**The write is bounded** by `AGENT_MCP_MAX_WRITE_BYTES` (1 MiB by
default; the ceiling is the transport's own 2 MiB body limit, because
the bytes travel in the request). A write over the budget is refused and
nothing is written — not even the directories.

**The path is checked before the bytes land.** A path outside the
workspace is refused, and so is a symlink that points out of it — as
the name itself and as a directory on the way. The write is made
relative to a descriptor of the target's directory, every component
opened with `O_NOFOLLOW`, so a directory that is swapped for a symlink
between the check and the write is refused too.

### `patch`

Applies a unified diff to a file in the workspace.

| Argument | Type | Meaning |
|---|---|---|
| `path` | string, **required** | the file to patch, inside the workspace |
| `diff` | string, **required** | a unified diff — the format `diff -u` and `git diff` produce |

Returns `name`, `path`, `size` and `applied`.

**The diff is applied here, not by a shell.** The new text is built in
memory and written once, at the end — and only if every hunk matched.
**A patch that does not apply cleanly is refused and changes nothing at
all.** That is the property the tool is worth.

**The diff must be about one file.** The `path` argument says which
one; a diff that carries a second file's header is refused.

**The hunk headers have to match their bodies.** A diff whose counts do
not line up — truncated, or written by hand — is refused with the
counts it found.

**A file that is not text cannot be patched.** The diff applies to
text, and a binary file is refused.

### `search_files`

Finds files in the workspace, or lines inside them.

| Argument | Type | Meaning |
|---|---|---|
| `pattern` | string, **required** | literal text (content) or a glob (name) |
| `path` | string | where to look; the workspace root by default |
| `kind` | string | `content` (the default) or `name` |
| `max_results` | integer | fewer matches than the budget — never more |

Returns `matches` (each with `path`, `line` and `text`), `files_scanned`,
`files_skipped`, `truncated`, `reason` and `limit`. The text of the
answer is one line per match: `path:line: text`.

**The pattern is literal text, not a regular expression.** A regular
expression can cost time proportional to the square of its input, and
the workspace is attacker-controlled. Use `bash` with `rg` for a regular
expression — that path is not fenced, and it is not counted either.

**The search never leaves the workspace.** The root it walks is
resolved and checked against the perimeter first, so a path outside it
is refused with a reason; a symlink is not followed at all, so a link
that points out is never read.

**The answer is bounded** by `AGENT_MCP_MAX_SEARCH_RESULTS` (100 by
default) and by a time budget of 30 seconds. A search that stops early
says so — `truncated` is true and `reason` says why. A file bigger than
the read budget is skipped rather than read, and counted in
`files_skipped`.

### `vision_analyze`

Looks at a picture that is already in the workspace, through the
operator's own vision endpoint, and returns the answer as text.

| Argument | Type | Meaning |
|---|---|---|
| `path` | string, **required** | the picture, inside the workspace |
| `prompt` | string | the question to ask about it; `Describe this image.` by default |

Returns `name`, `path`, `size`, `text`, `truncated` and `model`.

**The endpoint is the operator's, and only the operator's.** It is named
by `AGENT_VISION_URL`, and the model has no way to name another one:
the tool takes no URL, and there is no knob on the request that reaches
it. While `AGENT_VISION_URL` is empty the door is **closed**, and the
refusal says so.

**The picture is a file inside the workspace.** The path goes through the
same fence as every other file operation: a path that resolves outside
is refused, and a symlink that points out is not followed. A file that
is not a picture is refused by its type — the door looks at png, jpeg,
webp, gif and bmp files.

**There is no retrieval here.** A door that fetched a URL the model
named would be a retrieval door wearing a different hat, and retrieval is
not this project's (v2.13.0 — the research capability belongs to another
MCP, on another host). A URL-shaped path is a path: it is resolved
against the workspace, it is not there, and it is refused.

Three budgets and a timeout: `AGENT_VISION_MAX_BYTES` (8 MiB),
`AGENT_VISION_MAX_PROMPT_BYTES` (8 KiB) and `AGENT_VISION_MAX_ANSWER_BYTES`
(64 KiB), plus `AGENT_VISION_TIMEOUT` (60 s). The answer is truncated
when it is over its budget, and a truncated answer says that it is
truncated. The request and the refusal are counted
(`sandbox_agent_vision_requests_total`, `sandbox_agent_vision_refused_total`).

### `browser_open`

Opens a page in the sandbox's **own** browser — Chromium, in this
container, driven by Playwright — and returns the rendered text.

| Argument | Type | Meaning |
|---|---|---|
| `url` | string, **required** | the page; absolute, and http or https |
| `session_id` | string | the browser session; `mcp` by default |

Returns `url`, `title`, `text`, `truncated`, `session_id` and `sandbox`.

**The list of hosts is the operator's.** `AGENT_BROWSER_ALLOWED_HOSTS`
lists the hosts the tool may fetch — `example.com` exactly,
`*.example.com` for a domain and everything under it, and `*` for the
operator's explicit "any host". While the list is empty the door is
**closed**, and the refusal says so.

**A page that redirects off the list is refused.** The URL is checked
before the browser is reached, and the page is checked **again** after
it has loaded: Chromium follows an HTTP redirect inside its own network
stack and does not hand that follow-up to the route handler (measured
2026-11-02), so a route handler alone is not a fence. A page that ended
up somewhere else is refused and its content is discarded.

**What the list is, and what it is not.** It is a policy about **one
tool**, not a boundary: the jail's egress is unrestricted and measured,
so the model's own shell reaches the open web with `curl` anyway. What
it buys is that *our tool* does not become a second, unaudited door to
the web.

Budgets: `AGENT_BROWSER_MAX_TEXT_BYTES` (64 KiB) for the text,
`AGENT_BROWSER_TIMEOUT` (30 s) for a navigation, and
`AGENT_BROWSER_SETTLE_MS` (500 ms) for the page to settle. A truncated
answer says that it was truncated. The requests and the refusals are
counted (`sandbox_agent_browser_requests_total`,
`sandbox_agent_browser_refused_total`).

### `browser_screenshot`

Takes a PNG of the page that is open, and puts it in the workspace.

| Argument | Type | Meaning |
|---|---|---|
| `path` | string | the file, relative to the workspace; `browser.png` by default |
| `session_id` | string | the browser session; `mcp` by default |
| `full_page` | boolean | the whole page rather than the viewport; false by default |

Returns `name`, `path`, `size` and `session_id`.

**The picture goes through the same fence every other write uses.** A
path that resolves outside the workspace is refused, and a symlink that
points out is not followed. The picture is bounded by
`AGENT_BROWSER_MAX_SCREENSHOT_BYTES` (4 MiB), and over the budget
nothing is written.

### `browser_close`

Drops the browser context of a session: the page, its cookies and its
profile go, and the next page opens clean.

| Argument | Type | Meaning |
|---|---|---|
| `session_id` | string | the browser session; `mcp` by default |

Returns `closed` and `session_id`.

**The browser keeps no credentials.** Every session gets a fresh
context — no cookie, no token, no saved password — and nothing is
written into the workspace but the PNG the caller asked for. The
browser's own temporary files live on the tmpfs at `/tmp`, never in the
workspace.

### `todo_list`

The task list of a session: what the model is working through.

| Argument | Type | Meaning |
|---|---|---|
| `items` | list of strings | the new list; without it the list is returned |
| `session_id` | string | whose list; the caller's own session by default |

Returns `items`, `done`, `count` and `limit`.

**The list is replaced, not merged.** The model holds the whole plan,
so it rewrites the plan as the plan changes — a plan it cannot rewrite
is a plan it cannot finish. An item that starts with `[x] ` is done.

The list is bounded (`AGENT_TODO_MAX_ITEMS`, `AGENT_TODO_MAX_ITEM_BYTES`)
and it dies with the session. The session record (`GET /sessions`)
carries it.

**It is a plan, not an instruction.** Nothing in the list authorizes
anything: only the operator's words do.

### `memory`

The model's own notes, in a file the operator can read and delete.

| Argument | Type | Meaning |
|---|---|---|
| `action` | string | `list` (the default), `add`, `delete`, `clear` |
| `text` | string | the note to add |
| `index` | integer | which note to delete |

Returns `notes`, `count` and `limit`.

The memory is **a file** — `.agent-memory.json`, beside the session
registry — and it survives the agent's restart. It is bounded
(`AGENT_MEMORY_MAX_NOTES`, `AGENT_MEMORY_MAX_NOTE_BYTES`).

**These notes are context, never permission.** A note is something the
model wrote down; it does not authorize an action. Only the operator's
words do. The tool says so in its description, and the test pins it.

### `session_search`

Finds a command a session ran earlier.

| Argument | Type | Meaning |
|---|---|---|
| `query` | string, **required** | literal text |
| `session_id` | string | one session only |
| `max_results` | integer | fewer matches than the budget — never more |

Returns `matches` (each with `session_id`, `number` and `command`),
`sessions`, `truncated`, `reason` and `limit`. The text of the answer
is one line per match: `session#number: command`.

**The pattern is literal text, not a regular expression** — the same
rule as the file search, for the same reason.

The history is in memory and bounded
(`AGENT_SESSION_HISTORY_MAX_COMMANDS`): a session that dies takes its
history with it, and a restart of the agent starts the recall empty.

### `clarify`

Puts a question to the operator.

| Argument | Type | Meaning |
|---|---|---|
| `question` | string, **required** | the question |
| `session_id` | string | whose question; the caller's own session by default |

Returns `session_id`, `question` and `asked`. The session record
carries the question that is being waited for.

**The door has no channel of its own.** The protocol's own way for a
server to ask the client something — the MCP elicitation — needs a
back-channel for a request the *server* starts, and the stateless
Streamable HTTP transport has none. It was measured: the SDK answers
"this transport context has no back-channel for server-initiated
requests". So the channel is the caller: the question goes back in the
tool's answer, and the operator's answer comes with the next message.

---

## What a refusal looks like

A refusal is **not** a protocol error. The call succeeds, and the
result says the tool failed:

```json
{"isError": true, "content": [{"type": "text", "text": "Error executing tool read_file: path outside the workspace: /etc/passwd"}]}
```

The reason is in the text, because the text goes to the model. A
caller that wants to *react* to a refusal has to read it — there is no
status code in MCP, and inventing one in a tool's answer would be a
private protocol that only one caller understands.

The refusals that can come back:

| Refusal | Why |
|---|---|
| `path outside the workspace: …` | the path resolves outside the workspace |
| `not a regular file: …` | it is a directory, a fifo or a device |
| `file not found: …` | nothing there |
| `permission denied: …` | the agent's own process cannot read it (a root-owned 0600 file) |
| `the file is N bytes, which is over the limit of M bytes` | the read limit — the budget, the file policy or the ceiling, whichever is the smallest |
| `the file grew past the limit of M bytes while it was being read` | the file grew between the size check and the read |
| `the file was replaced while it was being opened: …` | the path became a symlink after the containment check |
| `the content is N bytes, over the write limit of M bytes` | the write budget |
| `the patch does not apply: …` | a hunk did not match, or the diff was malformed — and **nothing was changed** |
| `the file is not text — a patch applies to text` | a patch on a binary file |
| `the diff covers more than one file — …` | a diff with a second file in it |
| `the diff has no hunks — there is nothing to apply` | a diff with no hunk in it |
| `path outside the workspace: …` | a search or a write that named a path outside the perimeter |
| `unknown kind: …` | a search that asked for something other than `content` or `name` |
| `the pattern is empty` | a search with no pattern |
| `the vision door is closed: AGENT_VISION_URL is not set` | the operator has not opened the vision door |
| `… is not a picture (its type is …)` | the file is not an image the door will hand to the endpoint |
| `the picture is N bytes, over the limit of M bytes` | the picture budget |
| `the question is over the limit of M bytes` | the question budget |
| `the vision endpoint answered N: …` | the endpoint refused or failed |
| `the vision endpoint answered with something that is not JSON` | the endpoint is not the thing we think it is |
| `the vision endpoint answered with no text` | the endpoint said nothing |
| `the list has N items, over the limit of M` | the task list's budget |
| `the item is N bytes, over the limit of M bytes` | one item of the task list is too long |
| `the note is empty` | a note with nothing in it |
| `the note is N bytes, over the limit of M bytes` | one note of the memory is too long |
| `the memory already holds N notes, which is the limit of M` | the memory's budget |
| `there is no note N` | the memory has no note with that number |
| `unknown action: …` | a memory action the tool does not know |
| `the pattern is empty` | a recall with no pattern |
| `N matches is over the limit of M` | the recall's budget |
| `the question is empty` | a question with nothing in it |

---

## The boundary

* **One token, one boundary.** The token that opens the HTTP door opens
  the MCP door. There is no second, weaker credential for MCP.
* **The workspace is the perimeter.** Every path is resolved (symlinks
  included) and checked against the workspace root before it is
  touched. A symlink pointing out of the workspace is refused, not
  followed — and the file is opened *relative to a descriptor of the
  workspace*, one component at a time, every component with
  `O_NOFOLLOW`, so a file — or a **directory** of its path — that turns
  into a symlink *after* the check is refused too, with a 409. The
  file that is served is the file that was vetted.
* **The DNS-rebinding protection is available.** Set
  `AGENT_MCP_ALLOWED_HOSTS` and a request whose `Host` is not on the
  list is refused with a 421 before anything else happens. It is off by
  default; the bearer token is the guard either way.
* **The privilege mode applies here too.** With `AGENT_ALLOW_SUDO=1`,
  a command in the sandbox can become root inside the container — over
  MCP exactly as over HTTP. See `docs/SECURITY.md` §9.

---

## Known debt

* **The tools import the agent's functions from inside the function
  body.** The agent and the MCP server need each other, and the import
  is local to the call to keep the two modules from importing each
  other at the same time. It works and it is tested, but the seam is
  wider than it should be. The right fix is a shared module both sides
  import — a refactor, not a patch.
* **`read_file` does not stream.** The HTTP door streams a file out in
  chunks; the MCP tool holds it in memory and base64-encodes it. The
  budget and the ceiling keep the cost bounded, but a caller that wants
  more than the budget has to use the HTTP door, with its own, larger
  limit.

---

## The HTTP door, for comparison

| MCP tool | HTTP endpoint |
|---|---|
| `bash` | `POST /sessions/{id}/exec` (the proxy: `POST /bash`) |
| `read_file` | `GET /files?path=P` |
| `file_info` | `GET /files/meta?path=P` |
| `delete_file` | `DELETE /files?path=P` |
| `write_file` | no twin — the shell is the equivalent, and narrower |
| `patch` | no twin — the shell is the equivalent, and narrower |
| `search_files` | no twin — the shell is the equivalent, and narrower |
| `vision_analyze` | no twin — the endpoint is the operator's, and the shell has no eyes |
| `browser_open` | no twin — the browser is a capability the operator opened by naming the hosts |
| `browser_screenshot` | no twin — the picture lands in the workspace, and the shell can take one too |
| `browser_close` | no twin — the context is the browser's own |
| `todo_list` | no twin — the plan lives with the session |
| `memory` | no twin — the memory is a file of the model's own |
| `session_search` | no twin — the recall is over the history the sessions keep |
| `clarify` | no twin — the question goes back to the caller |

Both doors call the same functions in the same process, so the code
cannot drift apart — and the tests check that it does not. The budgets
are the one thing that differs on purpose; `read_file` above says how
and why.

The three tools without a twin are there because the model holds the
MCP surface, not the HTTP one: a model that writes through `bash` leaves
the file door's accounting empty. They are **narrower** than the shell
they stand in for — inside the workspace, bounded, counted — so the
MCP door is not a wider door than the HTTP one.

`vision_analyze` is the exception that proves the rule: it hands a
picture to an endpoint the operator chose, so it is **not** narrower
than the shell — it is a capability the operator opened on purpose,
closed again by emptying `AGENT_VISION_URL`.

The session tools — `todo_list`, `memory`, `session_search` and
`clarify` — are not narrower than the shell either, but for a
different reason: they keep state that the shell has no place for (a
plan, notes, a recall, a question). That state is the model's own,
it is bounded, and none of it is an instruction channel.
