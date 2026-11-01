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
`session_id` and `cwd`.

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

Both doors call the same functions in the same process, so the code
cannot drift apart — and the tests check that it does not. The budgets
are the one thing that differs on purpose; `read_file` above says how
and why.

The three tools without a twin are there because the model holds the
MCP surface, not the HTTP one: a model that writes through `bash` leaves
the file door's accounting empty. They are **narrower** than the shell
they stand in for — inside the workspace, bounded, counted — so the
MCP door is not a wider door than the HTTP one.
