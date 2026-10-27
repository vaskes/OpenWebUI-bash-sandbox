# Handoff — the sandbox as an MCP server

This is the contract for whoever drives the sandbox over the Model
Context Protocol: a telegram harness, an Open WebUI instance, a
llama.cpp-style caller, anything that speaks MCP.

The point of the document is that **the MCP door grants exactly what
the HTTP door grants**. The tools are the same capabilities, the
containment is the same containment, and the bearer token is the same
gate. Nothing is reachable over MCP that is not reachable over HTTP.

---

## Connecting

| | |
|---|---|
| Transport | MCP Streamable HTTP (the current spec revision) |
| Endpoint | `POST /mcp` on the **proxy** (published surface) or on the agent (internal network) |
| Auth | `Authorization: Bearer <token>` — the same token as the HTTP API |
| Sessions | stateless: the server keeps no session, every request stands on its own |
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
| `timeout` | integer | seconds to wait; the same 1..600 range the HTTP API allows |

Returns `stdout`, `stderr`, `exit_code`, `timed_out`, `duration_ms`,
`session_id` and `cwd`.

**The session is the shell.** `cd` and `export` survive from one call
to the next, so a caller that wants two conversations apart passes a
different `session_id` — a telegram harness wants one session per chat.
`cwd` is used when the session is created; a session that already
exists keeps the directory it was created with, and the answer says
which one that was (`cwd` in the result).

**The command's own text is the point.** `exit 3` ends the shell, the
pane dies with it, and the call waits out its timeout. Use
`sh -c 'exit 3'` to fail a command without killing the shell.

**A timeout does not stop the command.** When the command outruns its
budget the answer comes back with `timed_out: true` and the command
keeps running in the session — that is what a persistent session
means, and the only way to stop it is through the session. A caller
that wants the command gone has to say so (`pkill`, or a `^C` written
into the pane) in a second call.

**There is no write tool.** The shell is the write path: a command that
writes a file is the way to write one.

### `read_file`

Reads a file out of the workspace.

| Argument | Type | Meaning |
|---|---|---|
| `path` | string, **required** | the file, relative to the workspace or absolute inside it |

A text file comes back as text. Anything else comes back as an
embedded resource: base64 in the `blob` field, with the mime type. A
file over the read limit is an error that names its real size and the
limit.

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

---

## The boundary

* **One token, one boundary.** The token that opens the HTTP door opens
  the MCP door. There is no second, weaker credential for MCP.
* **The workspace is the perimeter.** Every path is resolved (symlinks
  included) and checked against the workspace root before it is
  touched. A symlink pointing out of the workspace is refused, not
  followed — and the file is opened with `O_NOFOLLOW`, so a file that
  turns into a symlink *after* the check is refused too, with a 409.
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

Both doors call the same functions in the same process, so the code
cannot drift apart — and the tests check that it does not. The budgets
are the one thing that differs on purpose; `read_file` above says how
and why.
