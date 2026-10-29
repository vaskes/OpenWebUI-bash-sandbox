# Handoff: the stdout budget and the binary door

**For:** the agent that maintains the bot (`vaskes/llama.cpp_telegram`).
**From:** the sandbox side (`vaskes/OpenWebUI-bash-sandbox`).
**Code against:** sandbox tag **`v2.14.11`** (the contract below was written
against `v2.7.0`; §6 lists what changed since, and the status-code table
in §3 is the current one).
**Written:** 2026-10-25. **Status codes last verified:** 2026-10-29 against
`v2.14.11`.

This is the contract between the two halves of the system. Everything in
it is measured or verified — the numbers are not aspirations, they are
what the sandbox actually does.

---

## 0. The short version

Two things changed on the sandbox side, and your side has to follow.

1. **stdout is now bounded, and it says when it was cut.** Before, the
   sandbox promised 1 MB and delivered a tail of somebody else's
   transcript, silently. Now the budget is honest and two different
   markers tell you what happened.
2. **Bytes have their own door.** `GET /files?path=` hands out the raw
   bytes of a file that exists on disk. Your `send_file` tool should pull
   the file through that door instead of asking the model to paste base64
   into a terminal pane — which is what it does today, and which is why
   files over a few hundred kilobytes never arrive.

What your side has to change:

| # | Change | Where |
|---|--------|-------|
| 1 | `send_file` pulls bytes through `/files`, not base64 through stdout | `telegram-bot/llm_dispatch.py` |
| 2 | The upload streams from a file object, not from `bytes` in RAM | `telegram-bot/llm_dispatch.py` |
| 3 | The delivered file is purged from the sandbox afterwards | new: `DELETE /files` |
| 4 | A truncated stdout is reported to the user, not swallowed | your truncation path |
| 5 | Your own tool-result truncation gets a marker | `telegram-bot/llm_iteration.py` |

---

## 1. The stdout contract

### 1.1 The budget

`AGENT_STDOUT_LIMIT` — how much **text one command** may return.
Default **512 KiB**, allowed range 32 KiB..1 MiB, `k`/`m`/`g` suffixes or
a plain byte count. A value outside the range is a **startup failure**,
not a clamp.

It is configured on the sandbox container (env var), not per request.

### 1.2 Two markers, and they mean different things

Both are plain text inside the `stdout` field of the exec response.

| Marker | Position | Meaning |
|--------|----------|---------|
| `[output truncated at N bytes]` | **end** of stdout | the sandbox cut the **tail**. The beginning is intact. |
| `[the beginning of this output no longer fits in the session scrollback — what follows is only its tail]` | **first line** of stdout | the **beginning** is gone. What you have is a tail — treat it as a fragment, not an answer. |

The second one is deliberately the *first* line: a client that reads only
the first line still learns that the result is incomplete. Do not strip
it and do not bury it.

### 1.3 stdout is this command's output — and only this command's

Before v2.6.0, `stdout` was the whole session transcript: the capture
took the pane's tail, and the pane's tail is every command the session
ever ran. A 200-byte command could come back carrying half a megabyte of
history.

Now the capture is bounded by how much the scrollback grew **during the
command**, and the result is cut at a marker printed before the command
ran. If you were compensating for the old behaviour (e.g. parsing
everything and hoping for your own marker), you can stop.

### 1.4 The response shape

`POST /sessions/{id}/exec` (agent) and `POST /bash` (proxy) answer:

```json
{"stdout": "...", "stderr": "", "exit_code": 0, "timed_out": false, "duration_ms": 123}
```

On timeout: `exit_code` is `null`, `timed_out` is `true`, and `stdout`
holds what was produced. On a proxy-level failure: `{"error": "..."}`.

### 1.5 Your own truncation is silent — fix that

`telegram-bot/llm_iteration.py:440` does `outcome.text[:MAX_TOOL_RESULT_CHARS]`
with `MAX_TOOL_RESULT_CHARS = 12000` (in `llm_dispatch.py:68`). That keeps
the head and drops the tail **with no marker at all** — the model sees a
result that looks complete and is not.

The sandbox now marks its own cuts. Yours should too: append something
like `[truncated at 12000 chars]` so the model (and the user) can tell a
complete answer from a fragment. A silent truncation is the same defect
the sandbox just fixed, one layer up.

---

## 2. The binary door

### 2.1 The endpoints

Published by the **proxy** (`openapi-proxy`, port 8090 in the bot's
stack), bearer token required (`OPENAPI_PROXY_TOKEN` / `AGENT_TOKEN`):

| Method | Path | What it does |
|--------|------|--------------|
| `GET` | `/files/meta?path=P` | JSON: `name`, `path`, `size`, `mtime`, `mime`, `limit` |
| `GET` | `/files?path=P` | the file's **raw bytes**; honours a single `Range` header (206 + `Content-Range`) |
| `DELETE` | `/files?path=P` | remove the file from the sandbox (204; 404 if there was nothing to remove) |

The path is resolved inside the sandbox workspace. A path outside the
workspace is a 400, a directory is a 400. Since **v2.14.1** the door no
longer follows a symlink: the file is opened relative to a descriptor of
the workspace, one component at a time, every component with
`O_NOFOLLOW`, so a component that turned into a symlink is refused with a
**409** instead of being followed out of the workspace. The agent's own
`/files` endpoints are not published to the outside — the proxy is the only
surface.

**All three endpoints answer the same way.** A file that is not there is
a **404**, a path that is refused is a **400**, a file that is over the
binary limit is a **413** (and the message names the real size), a path
that was replaced while it was being opened is a **409**, an unsatisfiable
`Range` is a **416**, and an unreachable sandbox agent is a **502**. The
error body is `{"detail": "..."}` — the message itself, not a JSON document
inside a JSON string. Do not look for an `error` key: there is none, and a
client that checks the status code is told the truth.

**A file the agent cannot read is a 403 on the download endpoint**
(`GET /files`), and it is a *decision*, not an accident. The agent opens
the file **before** it starts the response, so a file that its own
process may not read — one owned by root with mode 0600, or one with
mode 000 — is refused with a status and a message that says what to do
(`chmod 644`). It is not a 200 that promises N bytes and then delivers
none.

That distinction matters to a client: a **403** means "the file is
there, the agent is not allowed to read it — change the mode"; a
**truncated body** means the transport broke. Before v2.9.1 the door
answered the second case for the first situation, and a client saw a
short read and blamed the side-car for being unreachable. If you ever
see a short read again, treat it as a transport failure — and report
it as one — rather than as a permission problem.

The endpoint is **not rate limited**, on purpose: a file may arrive in
ranges, and a per-minute budget would break a legitimate transfer for a
reason that has nothing to do with the transfer.

**A note on the OpenAPI spec.** The proxy's `/openapi.json` is what Open
WebUI consumes as an external tool server, so `DELETE /files` appears in
the model's tool list there. It is a **client-lifecycle call**, not a model
tool: the model has a shell in the same workspace and could `rm` the file
itself, so the endpoint adds no capability — it adds a way for the *client*
to keep its promises. The spec stays complete on purpose; a route that
exists in the code and is missing from the spec is drift.

### 2.2 The delivery algorithm (what "the courier" does)

```
1. meta    GET  /files/meta?path=P        → size, name, mime, limit
2. check   size <= limit, else stop      → the 413 names the real size
3. pull    GET  /files?path=P             → STREAM, hash (sha256) while reading
4. deliver upload the stream to the channel (Telegram document / Open WebUI attachment)
5. purge   DELETE /files?path=P          → 204; the scratch copy is gone
6. report  tell the user: "sent <name> (<size>)"
```

Step 3 is a stream, not a buffer: read the response in chunks, feed them
to the hasher and to the upload. The proxy never buffers it either (it
has a 512 MB memory limit; buffering a 200 MB file would be an OOM kill
— that is why the relay is a stream).

Step 4 with **python-telegram-bot**: today the code does
`InputFile(payload, filename=name)` with `payload` being `bytes`
(`llm_dispatch.py:361`) — the whole file sits in RAM. Pass a **file
object** instead (spool the stream to a temp file in the bot container,
then `InputFile(open(path, "rb"), filename=name)`); PTB streams it. RAM
stays flat, and a retry after a Telegram 429 is a re-read of the temp
file instead of a re-download.

Step 5 is what makes the artifact live in exactly one place. The
sandbox is a scratchpad; the delivery is the copy that counts. If you
prefer to keep the file for follow-up turns, skip the purge — but then
it lives twice, and the workspace is the copy you have to remember to
clean up.

### 2.3 The pointer: `file_id`, not a sandbox path

After the upload, Telegram gives you a `file_id`. **That** is the
pointer:

* re-sending the file to another chat costs **zero bytes** — Telegram
  already holds it;
* it is not a URL, so nothing leaks by being pasted;
* it does not expire the way a sandbox path does.

Keep the `file_id` next to the chat/session state if you want a
follow-up turn ("change the colour in it") to re-send without pulling
the bytes again.

Do **not** hand out Telegram's
`https://api.telegram.org/file/bot<token>/<file_path>` link: the bot
token is in the URL, and the link expires after an hour.

### 2.4 Sizing: there are two limits, and the smaller one wins

| Limit | Value | Who sets it |
|-------|-------|-------------|
| Sandbox policy | `AGENT_BINARY_LIMIT`, default `2048k` (2 MiB), `0` = unlimited | the sandbox's env |
| Channel | Telegram `sendDocument`: **50 MB** via the public Bot API (2000 MB with a local `telegram-bot-api`) | the channel |

A sandbox limit above the channel's limit is a limit nobody can use:
raise the channel first, then the sandbox. A file over the sandbox limit
comes back as a **413** whose message names the real size — so the bot
can say "the file is 3 MB, the sandbox allows 2 MB" instead of failing
vaguely.

### 2.5 What the sandbox deliberately does not do

* **It does not stream a file that is not finished.** The door serves
  files that exist, completely, on disk. A download in flight, or a file
  still being written, has no URL here. The producer materialises it
  first:
  `curl -fsSL "$URL" -o out/report.pdf && sha256sum out/report.pdf && stat -c%s out/report.pdf`
  — one command, and the facts come back as ordinary stdout text.
* **There is no digest endpoint.** Hash the stream while you read it —
  that is free, and it is the verification that matters (the bytes you
  delivered are the bytes on disk). The producer's `sha256sum` output
  gives you a second, independent digest to compare against.
* **There are no handles, no capability URLs, and nothing published to
  the internet.** The sandbox stays on the internal network. If a user
  needs to fetch a file directly with a browser, that is a different
  feature (and it belongs on the bot's own domain, not the sandbox's).

---

## 3. What to remove on your side

* **The base64-through-the-pane path.** `_execute_send_file`
  (`llm_dispatch.py:266`) runs `base64 -w0 <path>` inside the sandbox and
  reads the payload back out of stdout between two markers. That path:
  - is bounded by the pane's scrollback (~400 KB, and it drops the
    **head** silently when it overflows — the payload then decodes into
    garbage or fails to decode at all);
  - pays 4/3 in size (base64);
  - puts the payload into the model's tool result, i.e. into the context;
  - and is bounded again by `MAX_SEND_FILE_BASE64_CHARS = 900_000`
    (`llm_dispatch.py:74`) — about 675 KB of real file.
* **`InputFile(payload)` with `bytes`** — use a file object, see 2.2.
* **The message "is empty, missing, or not a regular file"**
  (`llm_dispatch.py:333`) — it says the same thing for at least three
  different causes, including "the file was there but the pane ate the
  beginning of it". The new path can tell them apart.

---

## 4. How to verify (do this before you deploy)

Against a running sandbox (`TOKEN` = the shared bearer token):

```bash
# 1. the file's facts
curl -fsS -H "Authorization: Bearer $TOKEN" \
  'http://127.0.0.1:8090/files/meta?path=report.pdf'

# 2. the bytes, and their digest — must match the producer's sha256sum
curl -fsS -H "Authorization: Bearer $TOKEN" -o /tmp/report.pdf \
  'http://127.0.0.1:8090/files?path=report.pdf'
sha256sum /tmp/report.pdf

# 3. a range works (206, and the right slice)
curl -s -o /tmp/part.bin -w '%{http_code}\n' \
  -H "Authorization: Bearer $TOKEN" -H 'Range: bytes=3-8' \
  'http://127.0.0.1:8090/files?path=report.pdf'

# 4. the purge
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE \
  -H "Authorization: Bearer $TOKEN" \
  'http://127.0.0.1:8090/files?path=report.pdf'      # 204

# 5. and it is really gone
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $TOKEN" \
  'http://127.0.0.1:8090/files?path=report.pdf'      # 404
```

Then, in the bot:

* a file under the limit arrives in the chat **with the right bytes**
  (compare the digest you computed with the one the model printed);
* a file over the limit produces a clear message naming its real size,
  not a generic failure;
* a command whose output is too long produces a result that says it was
  cut — for you and for the user.

The sandbox side has its own checks; you can lean on them:

```bash
git clone --branch v2.14.11 https://github.com/vaskes/OpenWebUI-bash-sandbox.git
cd OpenWebUI-bash-sandbox && python3 -m unittest discover -s tests
```

The CI job `Deploy (docker build + boot + smoke)` builds both images,
boots them, runs a real command as uid 1000, pulls a file through the
proxy, checks the digest, checks a `Range`, checks the 413, and then
purges the file and checks that it is really gone from the workspace.

---

## 5. Where the truth lives

| Question | Answer |
|----------|--------|
| Which sandbox version | tag **`v2.14.11`** (`git checkout v2.14.11`) |
| Endpoint list | `README.md` → "API quick reference" (kept honest by a tripwire) |
| The delivery contract | `README.md` → "Moving files out of the sandbox" |
| The stdout contract | `README.md` → "Reading command output"; `agent_api.py` docstring |
| Security notes | `docs/SECURITY.md` → "The file door" |
| Why the numbers are what they are | `CHANGELOG.md` → 2.6.0 and 2.7.0 |

If the bot's behaviour and this document disagree, this document is
wrong and should be fixed here — the sandbox is the side that ships the
contract, and the tripwires here check that the contract in the code, the
README and the docstring say the same thing.

---

## 6. What changed after v2.7.0

The contract above was written against `v2.7.0`. The door's behaviour
changed since; the table in §3 is the current one. What a client must
know:

| Since | Change | What it means for a client |
|-------|--------|----------------------------|
| v2.9.1 | a file the agent cannot read is a **403**, not a 200 with a truncated body | treat 403 as "change the mode", not "the transport broke" |
| v2.13.0 | the MCP door (`POST /mcp`) appeared — a separate surface with its own read budget | see `docs/HANDOFF-mcp.md`; the HTTP door is untouched |
| v2.14.1 | the file door opens relative to a workspace descriptor, every component with `O_NOFOLLOW` | a path replaced mid-read is a **409**, and a symlinked component is never followed |
| v2.14.6 | the size limit is checked **before** the file is opened | a file over the limit is a **413** without the descriptor ever being opened |

The route list itself did not change: `POST /sessions/{id}/exec`,
`GET /files`, `GET /files/meta` and `DELETE /files` are the same four,
with the same methods and the same authorization contract. `/metrics`
appeared on both services (v2.12.1), and that is the only addition to the
published surface.
