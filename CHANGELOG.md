# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.11.1] - 2026-10-26

The code-scanning alerts that survived v2.11.0, closed by changing the
code rather than by silencing the analyser. Ten alerts, two causes: the
redaction patterns could be made to run in quadratic time, and the
containment check on the file door was invisible to the analyser.

### Fixed

- **The redaction patterns could be made slow on purpose.** The
  patterns that find a secret in a command — `NAME=value`, `--flag
  value`, `name: value` — used greedy stars. A greedy star gives
  characters back when the rest of the pattern does not match, so a
  command made of 100 000 "A"s is retried at every position of the run
  and the cost of the match becomes quadratic in the length of the
  command. The command text comes from the model, so the model could
  choose the size of the input.

  The quantifiers are possessive now (`*+`, Python 3.11+): they never
  give anything back, so there is nothing to retry. A possessive
  quantifier has to *mean* the same thing as the greedy one, and the
  two are compared over a corpus in the tests — a redaction that
  quietly stops matching is worse than a slow one.

- **The containment check on the file door was written in a form nobody
  could check.** The check itself was correct: the path is resolved
  with `realpath` and compared with `commonpath`. But it was invisible
  to the static analyser, which reported every filesystem call behind
  it as unguarded — six alerts that all pointed at the same place and
  all said, in effect, "we cannot see that this is safe".

  The check is now a `startswith` comparison against the workspace root
  *with* the separator, which the analyser follows and a reviewer reads
  in one line. The separator matters: `startswith("/workspace")` cannot
  tell the workspace from a sibling directory called
  `/workspace-evil`, and a sibling directory is a real place on the
  disk where a file can sit. With the separator, after the resolution,
  the check is equivalent to the `commonpath` one it replaces — not
  weaker — and the sibling is refused.

  The filesystem calls in the file door are now made with the resolved
  path as a string, and the open happens on exactly that string, so
  the file that is served is the file that was vetted.

### Added

- The sibling-directory case, the logical-path case and the shape of the
  guard are pinned by `tests/test_path_containment.py`; the possessive
  quantifiers are pinned by the tests in `tests/test_review_v2110.py`.
  Every one of them was checked against a mutation: a tripwire that
  cannot be made red is a decoration.

### Notes

- The alerts that GitHub reports are closed by the code change, not by
  a suppression. The CI CodeQL job runs on every push, so a reappearance
  is a regression, not a false positive to argue about.

## [2.12.1] - 2026-10-27

The MCP release went out to the polygon and to production, and two
reviews looked at it afterwards. One of them had no access to the
repository and therefore said nothing about the code; the other found
six things worth acting on. Four were real defects and are fixed here,
one was a documentation gap, and one is debt that is written down
instead of being fixed in a patch release.

The fourth real defect was found by this session, not by the reviewer:
the proxy's MCP relay gave up before the agent could finish, so a long
command that the agent would have answered came back as a transport
error.

### Fixed

- **The file door could serve a symlink it had just refused.** The
  containment check resolved the path and vetted it, and the open
  happened a moment later — and between those two moments the file
  could be replaced by a symlink pointing out of the workspace. The
  workspace is writable, so the race was real. The open now carries
  `O_NOFOLLOW`: if the path turned into a symlink in the meantime, the
  open fails and the caller gets a 409 that says exactly that.

- **A body without a length was not counted.** The size limit read the
  `Content-Length` header, so a request that arrived with
  `Transfer-Encoding: chunked` — and therefore with no length to read
  — was measured by nothing. The limiter counts the bytes as they
  arrive now, in both services, and refuses the request with a 413 when
  the count passes the limit. A legitimate chunked client still works:
  the limit is a limit, not a ban on streaming.

  The limiter is a pure ASGI middleware on purpose. A
  `@app.middleware("http")` function cannot do this: Starlette hands
  the downstream application its own wrapped receive channel, so a
  middleware that wrapped `request` would count bytes the application
  never sees. It lives in `body_limit.py` and is copied into both
  images — two copies of the same security rule would be two rules.

- **One MCP read could cost the machine everything.** `read_file` read
  the whole file and base64-encoded it, and the size limit it applied
  was the configured binary limit — which is `0` ("no limit") in some
  deployments, including the one where the operator raised it for
  Telegram delivery. The read has a ceiling of its own now (64 MiB), and
  the ceiling is applied *during* the read: the read asks for one byte
  more than the limit allows, so a file that grows between the `stat`
  and the read is refused with the reason instead of being read whole.

- **The proxy's MCP relay gave up before the agent could answer.** The
  proxy waits 75 seconds for the agent — enough for the exec door,
  whose limit the proxy itself sets, and far too little for the MCP
  door, where the *model* sets the timeout and the agent honours it up
  to 600 seconds. A long command that the agent was still computing
  came back to the caller as a transport error. The relay has its own
  timeout now, and it is above the agent's own cap: the agent always
  answers within that cap, and the relay's job is to be there when it
  does. A tripwire compares the two numbers, so a drift is caught
  rather than discovered in production.

- **A failing tool left no trace.** The refusal reached the model with
  its reason, but the operator got nothing in the log. Every failure
  path in the MCP server now logs a traceback as well.

- **The reaper printed every dropped session id.** A machine that lost
  a thousand sessions produced a log line a thousand ids long, which
  is the same as no line at all. The list is truncated to ten; the
  count in the same line stays the truth.

### Documentation

- The MCP server's own instructions now say that the shell is
  **persistent and shared**, and that a caller should use its own
  `session_id`. Two callers that share the default session share a
  `cwd`, an environment and a history — the model has to know that.
- `docs/HANDOFF-mcp.md` says what a timeout leaves behind (the command
  keeps running in tmux — that is what a persistent session means),
  that `read_file` holds the file in memory and base64-encodes it, and
  that the lazy local imports are known debt.

### Known debt

- The MCP tools import the agent's functions from inside the function
  body, to avoid a circular import. It works and it is tested, but the
  seam is wider than it should be. Moving the shared behaviour into a
  module both sides import is the right fix; it is not a patch release,
  and a broad refactor of a deployed repository is riskier than
  writing the seam down.

## [2.12.0] - 2026-10-27

The sandbox is now also a **Model Context Protocol server**. Same
sandbox, same tools, another protocol — so a telegram harness, an
Open WebUI instance or a llama.cpp-style caller can all drive it
without any of them knowing that the sandbox was ever an OpenAPI
service.

### Added

- **`POST /mcp`** on the agent, relayed by the proxy. The MCP
  Streamable HTTP transport, stateless: the server keeps no session,
  so there is nothing to hijack, nothing to lose on a restart, and no
  session affinity to keep when the endpoint sits behind a proxy.

- **Four tools**, one per capability, each calling the same functions
  the HTTP API calls in the same process — so the two doors cannot
  drift apart:

  | Tool | What it does | HTTP equivalent |
  |---|---|---|
  | `bash` | run a command; stdout, exit code, timing | `POST /sessions/{id}/exec` |
  | `read_file` | take a file out (text as text, binary as a base64 blob) | `GET /files?path=P` |
  | `file_info` | name, size, mime, mtime — without reading | `GET /files/meta?path=P` |
  | `delete_file` | remove a file from the workspace | `DELETE /files?path=P` |

  There is no write tool: the shell is the write path, and a command
  that writes a file is the way to write one.

- **The protocol version is negotiated.** A client that offers
  `2025-06-18` — what most of the world speaks today — gets that
  version back; a client that offers the newest revision gets a
  version it can actually use. The server is not a client for one
  generation of callers only.

- **`AGENT_MCP_ALLOWED_HOSTS`** on the agent: the `Host` header values
  the endpoint answers for. Setting it turns the protection against
  DNS rebinding on — a request with any other `Host` is refused with a
  421. Empty = the check is off, which is the default and the SDK's
  own default for a service that is not bound to the loopback.

- **`docs/HANDOFF-mcp.md`** — the contract for whoever drives the
  sandbox over MCP: the transport, the tool schemas, the shape of a
  refusal, and the boundary.

### Security

- **One token, one comparison.** The MCP gate and the HTTP gate call
  the same `_token_ok()`, so a token that opens one door opens the
  other and nothing else does. The MCP SDK's OAuth machinery is not
  used: the sandbox has one trust boundary and one secret in it.
- **The DNS-rebinding protection is available and off by default.** A
  request whose `Host` is not on `AGENT_MCP_ALLOWED_HOSTS` is refused
  with a 421 before anything else happens — when the operator sets the
  list. The default is off, because a containerised service is reached
  by its service name, and a list that does not contain that name
  refuses every legitimate caller. The bearer token is the guard
  either way, and the agent says so once at start-up.
- **A refusal is not a protocol error.** The call succeeds and the
  result carries `isError: true` with the reason in the text — the
  text is what the model reads, so the reason has to be in it.
- The privilege mode applies here too: over MCP exactly as over HTTP.

### Notes

- The MCP SDK is a runtime dependency of the **agent** only, so it
  lives in `requirements-agent.txt`; the proxy image stays free of
  packages nothing in it imports.
- The endpoint is a route and not a mount on purpose: a mount answers
  `/mcp` with a redirect to `/mcp/`, and some clients do not follow a
  redirect for POST — the handshake would simply never happen.
- 29 new tests, including one that drives the sandbox through the
  **reference MCP client** — the claim "any MCP caller" is a claim
  about the wire, and it is checked against the client the ecosystem
  actually uses.

## [2.11.0] - 2026-10-26

The Qwen review of the whole repository, worked through finding by
finding: what was real is fixed, what was already true is written down,
and what was wrong is left alone — with the reason.

### Fixed

- **The `ssh` shortcut took a key it could not protect.** The
  identity file was checked in the proxy — "is it a regular file
  inside the workspace?" — while the file itself was opened later, by
  `ssh`, in the sandbox. Between the two there is a round-trip, and a
  model with a second session could swap the checked file for a
  symlink. The old code said so in its own docstring: "the TOCTOU
  window ... is exploitable".

  A path rule cannot be made race-free there: the only check that
  cannot be raced is the permission check the shell performs when it
  opens the file. So `-i` is now refused outright, with a message that
  says why. Nothing is lost: a plain `ssh -i ...` inside the sandbox
  was always possible, and the shortcut was never a boundary.

- **The command preview in the log carried secrets.** Every executed
  command is logged with a 200-character preview of its text, so that
  an operator can reconstruct what the model ran. A command carrying a
  credential — `export API_KEY=…`, `curl -H "Authorization: Bearer …"`,
  `mysql --password …`, `sshpass -p …`, a pasted private key — wrote
  that credential into the log file.

  The preview is redacted now: the values of assignments, flags and
  `name: value` pairs whose name looks secret, the value of an
  `Authorization` header, the value of `sshpass -p` and PEM private-key
  blocks. The names stay, so the log still says what the command was
  doing. The redaction matches *shapes*, not meaning — the log is
  still sensitive, and docs/SECURITY.md says so.

- **The protocol markers were 32 bits wide.** The marker names the
  bookkeeping line that tells the agent the command is over, and a
  command that prints a line of its own must not be able to
  impersonate it. 16 hex characters (64 bits) instead of 8.

- **A token with a quote broke the Open WebUI tool connection.** The
  compose file interpolates the token into the `TOOL_SERVER_CONNECTIONS`
  JSON, and Open WebUI parses that document. A quote or a backslash in
  the token made it unparseable — and the failure was quiet: the UI
  came up without the sandbox tool and said nothing about why. The
  token is now checked (the deploy gate `make check-env` refuses one
  that would break the JSON, and the proxy logs the problem at
  start-up for the stacks that do not run the gate).

- **The process limit was a constant.** `pids_limit: 256` is plenty
  for a shell and too few for a build that fans out; when the limit is
  hit, even legitimate commands fail with "Resource temporarily
  unavailable". It is a knob now (`AGENT_PIDS_LIMIT`), with the safe
  default kept: the operator who hits the wall raises it, everybody
  else keeps the fork-bomb protection.

- **The log lines were JSON only when the message had no quotes.**
  Both services logged JSON by interpolating the message into a JSON
  string — and the agent logs the text of every command, which is
  full of quotes. The busiest log in the system was the one that
  produced lines no parser could read. A shared formatter escapes the
  message properly now.

  The access log was the other half: uvicorn installs an access log of
  its own, in plain text, when the server starts, and a container
  whose stdout is half JSON is a log that half of the queries silently
  miss. The access log is taken over as well — the client address, the
  method, the path, the HTTP version and the status code stay fields
  instead of being buried in a sentence, so a log query can count a
  status code instead of matching it out of a line of text.

- **The proxy image installed curl for one healthcheck.** The probe
  asks Python now, and the image installs nothing from the
  distribution at all. The sandbox image keeps curl: the model's shell
  uses it.

### Added

- **A Prometheus exposition on both services.** `/metrics`, bearer
  token required like every other endpoint: requests, rate-limit
  refusals, upstream timeouts and upstream errors on the proxy;
  commands, timeouts and active sessions on the agent.

- **A monitoring example.** `docker-compose.monitoring.yml` brings up
  Prometheus, Grafana and cAdvisor as a separate compose project, with
  the datasource and the dashboard provisioned from files. A tripwire
  renders the dashboard and compares every metric it asks for against
  what the services actually expose — an example that asks for a
  metric nobody publishes is worse than no example.

  The example was broken when it was written and only a live run
  showed it: Prometheus reads the bearer token as the user it runs as
  (65534 = nobody), and the instructions said "chmod 600" on a
  root-owned file — so every scrape failed with "unable to read
  authorization credentials" and the targets stayed down with no hint
  of why. The instructions now hand the file to that user, and a
  tripwire keeps them saying so.

- **The single-operator warning moved to the top of the README.** The
  stack is single-operator by design (one bearer token, one tmux
  server, one workspace). That belongs above the fold, not in
  Limitations where nobody reads before deploying.

### Changed

- The proxy image no longer carries a package manager.
- The `ssh` shortcut refuses `-i` (see above).
- `docs/SECURITY.md` now says what the SSH allowlist is: a guardrail
  on the naive path, not a boundary. Inside the sandbox the model has
  a real shell, and what protects the operator's key is the
  permissions of its read-only mount.

### Reviewed and deliberately not changed

- **M1 — "the path check is fragile".** It is `realpath` +
  `commonpath` against the workspace root already; the review's
  suggestion (a string `startswith`) is *weaker* than what is there.
- **H2 — "tmux sessions leak on SIGTERM".** They die with the
  container, and killing them on shutdown was the v2.1 behaviour that
  broke persistence across restarts.
- **"the secrets in `.env.example` are not documented".** The template
  has said `openssl rand -hex 32` since v2.4.2.
- **"the path validation is duplicated between the services".** The
  proxy's copy went away together with the `-i` flag.

## [2.10.2] - 2026-10-26

A command can no longer eat the protocol, and the timeout answer survives
the trip.

### Fixed

- **A command that reads stdin ate the completion marker.** The agent
  types the command and the bookkeeping lines that follow it into the
  same terminal, so the rest of the block was still sitting in the input
  buffer when the command started. A command that reads stdin — `read x`,
  `apt-get` — read those lines instead of waiting, the completion marker
  never appeared, and the caller waited out the whole limit for an
  answer that never came. The package got installed and the answer was
  lost.

  The command now runs inside a brace group whose stdin is
  `/dev/null`. It reads EOF, the bookkeeping stays in the buffer where
  the command cannot reach it, and the answer comes back — with the
  output intact.

  The group is transparent to the caller: the stdout is byte-for-byte
  what it was before, including the shell's echo of the command line.
  Two shell details make that true, and both are load-bearing. The
  shell parses a whole group before running any of it, so the output
  arrives *after* the line that closes the group — the bookkeeping
  therefore sits on that line, and the extraction drops that line
  instead of everything from it. And a continuation line is redrawn
  with `PS2` instead of `PS1`, so the block sets `PS2="$PS1"` and the
  redraw looks exactly like the one the command had without the group.

  A command that contains a brace bash would read as a group delimiter
  (`echo }`) is run unwrapped, exactly as before: wrapping it would
  change what it means, and a command that means something else is worse
  than a command that waits. The scan reports only braces it is sure
  about — `echo '}'`, `echo a}b`, `find -exec {} \;` and `${HOME}` are
  all grouped normally.

- **The proxy and the agent used the same number.** The proxy asked the
  agent for a 60-second command and gave up waiting after 60 seconds
  too, so the answer that said "this timed out" arrived at the very
  moment the proxy stopped listening. The caller got a transport error
  with an empty reason, and an empty reason sends the operator to the
  network instead of to the clock.

  The proxy now waits `AGENT_EXEC_TIMEOUT + 15` seconds — always
  strictly longer than the limit it asked for — and a timeout names
  itself: `"sandbox-agent request timed out after 75s (exec limit
  60s): ReadTimeout('')"`.

- **A transport error had no cause in it either.** `str(exc)` of an
  httpx error is often empty. The reason is now `repr(exc)`, so the
  exception class is in the text: `ConnectError("[Errno 111]
  Connection refused")`.

- **The startup reaper formatted a list of session ids with `%d`.** That
  raised a TypeError inside the logging machinery, which swallowed the
  message and printed a formatting error instead of the line that says
  which sessions were dropped. It now says how many and which.

### Added

- **The exec limit is a knob.** `AGENT_EXEC_TIMEOUT` (5..600 seconds,
  default 60) on the proxy service, in all three compose files,
  `.env.example` and `docs/CONFIGURATION.md`. A value outside the range
  is a startup failure, not a clamp.

- **`docs/HANDOFF-stdin-and-timeouts.md`** — the sidecar half of the
  handoff: what changed, what did not, the knob, and the exact compose
  lines the bot side quotes.

### Changed

- A command that reads stdin now reads EOF. `cat` with no argument
  returns empty instead of hanging, and an interactive program exits
  instead of taking over the pane. That is the right answer for a
  one-shot tool, and it is a change: before, those commands ran until
  the exec limit expired.

Version 2.10.2. README test count 300 -> 336.

## [2.10.1] - 2026-10-25

The documentation counts its settings.

### Fixed

- **The privilege mode promised "three settings" above a table with
  four.** v2.10.0 added `AGENT_READ_ONLY` to the table in
  `docs/SECURITY.md` section 9 and left the sentence above it alone. A
  reader counting the rows got four and a sentence that said three.
  The prose now says four, and says which three are the escalation
  and which one decides whether the root is *useful*.

- **The same ambiguity, smaller, in two more places.**
  `docs/CONFIGURATION.md` said "Set all three" and `.env.example` said
  "All three settings are needed". Both now say "for the escalation",
  because the fourth setting belongs to the same feature and the old
  wording read as "there are three in total".

### Added

- **A tripwire on the count.** `tests/test_privilege_mode.py` parses the
  privilege table, compares its row count with the number in the prose,
  and checks that every knob the table names is *interpolated* by a
  compose file — a mention in a comment is not a switch.

  Verified by mutation, all three of them red: adding a fifth row to
  the table, writing "three settings" again, and dropping the
  interpolation from the compose files.

Version 2.10.1. README test count 298 -> 300.

## [2.10.0] - 2026-10-25

A root shell that cannot write is a half-root.

### Fixed

- **`apt` did not work for the agent, and the sudo implementation was
  never the reason.** The privilege mode hands a command a root shell
  inside the container, and the root shell could not install a package:
  `apt-get` needs three writable paths — `/var/lib/apt/lists` for the
  index, `/var/lib/dpkg` for the package database and `/usr` for the
  packages themselves — and the deployment's `read_only: true` root
  filesystem refuses all three. The measured failure, as root, in the
  stock posture:

  ```
  # apt-get update
  E: List directory /var/lib/apt/lists/partial is missing. - Acquire (30: Read-only file system)
  # touch /usr/local/probe
  Read-only file system
  ```

  The `tb-sandbox-apt` volume at `/var/cache/apt` — the download cache,
  the smallest of the three — was the only writable apt path, and it
  made the failure look like a sudo problem instead of a filesystem
  problem.

  The privilege mode now has a **fourth setting**,
  `AGENT_READ_ONLY` (default `true`, so the stock posture does not
  change). With `AGENT_READ_ONLY=false` the container's root
  filesystem is writable and the privileged root can actually install
  packages:

  ```
  AGENT_ALLOW_SUDO=1
  AGENT_NO_NEW_PRIVILEGES=false
  AGENT_CAP_DROP=SYS_ADMIN
  AGENT_READ_ONLY=false
  ```

  The writable layer is the container's own: it is discarded when the
  container is recreated, and it does not move the boundary. The
  container's namespaces, capabilities, devices and mounts are exactly
  what they were, and `CAP_SYS_ADMIN` is still absent.

### Added

- **`agent_core.rootfs_is_read_only()`** — parses `/proc/self/mountinfo`
  and reports whether the root filesystem is mounted read-only. A mount
  table that cannot be parsed counts as read-only: an unknown state
  must never be read as "the root may write here".
- **A startup warning for the hollow root.** The agent already announced
  a privilege mode that could not escalate. It now also announces one
  that *can* escalate but owns a filesystem it cannot change — the
  operator sees it before the model does, instead of the model
  reporting "the sandbox is broken".
- **A `privilege` CI job.** It builds the agent image with `sudo` and
  checks the matrix that matters: the escalation works and the root
  filesystem is read-only → `apt-get` is refused with *"Read-only file
  system"*; `AGENT_READ_ONLY=false` → `apt-get update` and
  `apt-get install` both succeed and the package lands on the PATH.
- **`tests/test_read_only_rootfs.py`** — 11 cases: the detection, the
  warning, and the knob in all three stacks (with the default pinned to
  the stock posture).

### Changed

- **`docs/SECURITY.md`** — the privilege mode's table carries the
  fourth setting, and the claim that "the root shell can write anywhere
  in the container" is now true only when `AGENT_READ_ONLY=false`
  says so. Section 2 no longer lists `/home/agent` as a named volume
  (it stopped being one in v2.7.1) and no longer claims the model can
  never be root.
- **`docs/CONFIGURATION.md`** — the knob, and a writable-surface table
  that says what a read-only root filesystem actually blocks.
- **`docs/ARCHITECTURE.md`** — the volume at `/var/cache/apt` is the
  *download cache*, not installed packages: the packages land in `/usr`,
  which is not a volume.
- **`.env.example`** — the fourth setting, next to the other three.

Version 2.10.0. README test count 286 -> 298.

## [2.9.1] - 2026-10-25

The file door tells the truth about permissions.

### Fixed

- **An unreadable file is a 403, not a truncated 200.** The download
  endpoint opened the file *lazily*, inside the response generator. A
  file the agent's own process cannot read — one created by root with
  `umask 077`, which the privilege mode makes easy — could not be
  opened, but the `200` and the `Content-Length` had already gone out.
  The client received 0 of the promised bytes: `httpx` raised
  `RemoteProtocolError`, and the bot reported "the sandbox side-car is
  unreachable" — blaming the network instead of the file's mode.

  The file is now opened **before** the response starts, so a refusal
  is a status code with a message that says what to do (`chmod 644`).
  The proxy relays it like every other error status.

  Reproduced on the live range first: `HTTP/1.1 200 OK`,
  `content-length: 6`, and `curl: (18) end of response with 6 bytes
  missing`. After the fix the same request answers `403` with
  `permission denied: the sandbox agent cannot read root-only.bin`.

### Added

- **`tests/test_binary_endpoint.py`** — two cases: an unreadable file is
  a 403 that names the problem, with and without a `Range` header. Both
  were verified by mutation: restoring the lazy open turns them red.
- **The deploy CI job** now creates an unreadable file two ways — mode
  000 on a file the agent owns, and a root-owned 0600 file borrowed
  through the volume — and expects a 403 from the shipped artifact.

### Changed

- **`docs/HANDOFF-binary-transport.md`** — the error contract now says
  what a 403 means, and what a client should do with a short read: treat
  it as a transport failure and report it as one, not as a permission
  problem.
- **`docs/SECURITY.md`** — the file door section records why the refusal
  is decided before the response starts.

Version 2.9.1. README test count 284 -> 286.

## [2.9.0] - 2026-10-25

The base images move forward, and the CI matrix follows them.

The agent now builds on `ubuntu:26.04` and the proxy on `python:3.14-slim`.
Both images carry python 3.14 — the agent's `python3` comes from the
Ubuntu release's apt package, the proxy's from the slim image. The CI
matrix used to stop at 3.12, so the bump on its own would have shipped a
runtime nobody tested. It now runs 3.11 → 3.14, and a tripwire keeps the
two in step: an image that ships a python the matrix does not cover
fails the build.

Every number in that paragraph was measured in the images themselves,
not read off a release note. `ubuntu:26.04` → python 3.14.4, uid 1000
still `ubuntu`. `python:3.14-slim` → python 3.14.9, the `proxy` group
still at GID 13. Both pip prefix layouts are unchanged — `/install/local`
on Ubuntu, `/install/{bin,lib}` on the slim image — so the two
Dockerfiles still differ on purpose, and both copies are still right.

### Changed

- **Base images** — `Dockerfile.sandbox` builds on `ubuntu:26.04`,
  `Dockerfile.openapi-proxy` on `python:3.14-slim`. The uid-1000
  contract, the pip prefix layouts and the privilege mode are untouched.
- **CI** — the matrix runs python 3.11, 3.12, 3.13 and 3.14 on the
  `ubuntu-26.04` runner image; `actions/checkout` v7,
  `actions/setup-python` v7, `codecov/codecov-action` v7 and
  `gitleaks/gitleaks-action` v3.
- **Dependency floors** — `requirements.txt` declares the versions a
  fresh install resolves to (uvicorn 0.54.0, pydantic 2.14.0, httpx
  0.28.1), `requirements-dev.txt` the versions the suite runs on (pytest
  9.1.1, pytest-asyncio 1.4.0). The ceilings stay open: these are
  floors, not pins.
- **Dependabot** — pip and GitHub Actions updates arrive as one grouped
  PR each instead of one branch per package.

### Added

- **`tests/test_runtime_python_is_covered_by_ci.py`** — the python
  version each image ships must be in the CI matrix. An unknown Ubuntu
  tag fails the test instead of being skipped.

## [2.8.0] - 2026-10-25

The privilege mode: the agent's shell can become root inside the sandbox.

Off by default. A stock image has **no `sudo`** and no setuid binary
that could help, the container runs with an **empty capability bounding
set** and **no-new-privileges** on — a command cannot become root,
because there is nothing to become root *with*. When the operator turns
the mode on, a command in the sandbox can run `sudo su` and get a root
shell *inside the container*.

> **This is for private deployments in secured environments only.** The
> sandbox runs code whose input may be hostile, and the privilege mode
> gives that code root inside the container. The container stays the
> boundary — nothing here reaches the host — but inside it the model is
> root.

### Added

- **`AGENT_ALLOW_SUDO`** — a build argument for the agent image. `1` = the
  image ships `sudo` and a `NOPASSWD` rule for the agent's user (the
  username is resolved from uid 1000 at build time, so it does not matter
  what the base image calls it). The rule is validated with `visudo -c`:
  a broken sudoers file would break sudo for everybody, root included.
  `0` = the default, and then the image carries no setuid-root path at
  all.
- **`AGENT_NO_NEW_PRIVILEGES` and `AGENT_CAP_DROP`** — the runtime half of
  the switch, in all three compose files. The setuid bit is inert while
  the container's no-new-privileges flag is on, and `su` cannot change
  the user while the capability bounding set is empty, so both have to
  give way. The defaults are the stock posture — `true` and `ALL` — so a
  deployment that touches nothing keeps the posture it has today.
- **A startup warning.** When `sudo` is present, the agent says so: either
  that the mode is on, or exactly which of the two runtime settings still
  blocks it. A half-configured deployment is otherwise indistinguishable
  from a broken sandbox — the model would report *"sudo: the 'no new
  privileges' flag is set"* and the operator would go looking for the
  wrong thing.
- `parse_capability_status` and `sudo_posture` in `agent_core` — the pure
  part of that check, unit-tested.
- **`Defaults !use_pty` for sudo**, and it is load-bearing. Sudo runs the
  command in a pseudo-terminal of its own by default; when that pty is
  torn down the terminal is reset, and the reset wipes the completion
  marker the exec protocol depends on. The command succeeds, its output
  is in the pane, and the exec still times out and reports nothing —
  a failure that is invisible in a manual test and shows up only as
  "the sandbox hangs on sudo". With `!use_pty` the command runs in the
  same terminal and the marker survives.
- **The documentation:** `docs/SECURITY.md` section 9 ("The privilege
  mode"), the configuration reference, the README and `.env.example` all
  carry the warning, and a tripwire fails the build if one of them stops
  carrying it.

### Changed

- Version 2.8.0; README test count 259 → 279.

## [2.7.1] - 2026-10-25

The meta door told the truth about the file and lied about the status.

### Fixed

- **`GET /files/meta` now relays the upstream status** instead of
  flattening every failure into a 200 with an `error` key. A file that
  is not there is a 404, a path outside the workspace is a 400, and an
  unreachable agent is a 502 — the same answer the download and purge
  endpoints already gave. The old shape was a trap for exactly the
  client this release was written for: the courier asks the meta
  endpoint for the size of a file it is about to pull, and a 200 says
  "here are the facts" even when the facts are missing — a client that
  trusts the status code is told the file is there, and a client that
  reads `size` gets nothing to compare against the limit.
- **The error body is no longer JSON inside a JSON string.** The meta
  endpoint wrapped the agent's `{"detail": "..."}` into
  `{"error": "{\"detail\": ...}"}`; it now passes the message through
  the same unwrapping the other endpoints use, so the client reads
  `{"detail": "file not found: report.pdf"}`.

### Added

- **A tripwire for the class**: `TestProxyFileEndpointsAgree` walks all
  three file endpoints and demands the same status for the same upstream
  answer. Flattening one of them again turns the test red — the failure
  mode that produced this release could not have survived it.
- The status codes of the three file endpoints are now written down in
  `docs/HANDOFF-binary-transport.md`, so the contract the bot codes
  against includes the failure case and not only the happy one.

### Fixed

- **No stack mounts a volume over the agent's code any more.** The dev
  and CI stacks both carried `sandbox-agent-home:/home/agent:rw` and
  `ci-agent-home:/home/agent:rw`. The application's code lives in
  `/home/agent` (`WORKDIR` is there so the import path resolves), and
  Docker seeds a named volume from the image on first use and never
  re-syncs it — so a volume at that path shadows the code with the copy
  from the first run, and every later image rebuild becomes a no-op.
  This is the defect that froze the production stack in v2.4.x; it was
  fixed there, and left in place in the other two stacks, where a fresh
  volume per run hides it. The CI stack now behaves like the production
  one: the code comes from the image, and only the workspace is a
  volume.
- **The tripwire that should have caught it now can.** The old check
  looked for a line *ending* in `/home/agent` — but the offending line
  ends in `:rw`, so it matched nothing and guarded nothing. It is
  replaced by `TestNoStackShadowsTheAgentCode`, which parses the volume
  specification of every compose file, derives the code directory from
  the Dockerfile instead of hard-coding it, and is mutation-checked
  against the exact line that shipped.

## [2.7.0] - 2026-10-25

The delivery side of the binary door, and a contract the bot can code against.

v2.6.0 gave the sandbox a way to hand out bytes. It did not say what
happens to the file afterwards, so the artifact ended up living in two
places at once — in the workspace and wherever the client delivered it —
and the client had no documented way to ask for the workspace copy to go
away. This release closes that loop and writes down the contract the
client codes against.

### Added

- **`DELETE /files?path=`** on the agent, relayed by the proxy. The
  sandbox is a scratchpad, not a file host: once a client has delivered a
  file, it removes the copy it made. The path goes through the same
  resolution as the download endpoint — outside the workspace, a symlink
  pointing out of it, or anything that is not a regular file is refused;
  a file that is not there is a 404, because "nothing to delete" is not
  the same as "deleted". The proxy relays the upstream status instead of
  flattening it, so a missing file stays a 404 and an unreachable agent
  is a 502.
- **`docs/HANDOFF-binary-transport.md`** — the contract for the client
  that delivers files out of this sandbox: what to call, in what order,
  what the sandbox deliberately does not do, how to size a transfer
  against the channel's own limit, and how to verify a delivery. Written
  for the agent that maintains the bot, which is where the courier lives.
- **`tests/test_api_docs_cover_routes.py`** — every route the agent and
  the proxy serve must be named in the agent's `Public API` docstring and
  in the README's API tables, with the same method and the same path.
  v2.6.0 shipped `/files` and `/files/meta` and documented them in the
  README but not in the docstring; a route that exists in the code and in
  neither document is a route nobody knows about.

### Changed

- **`README.md`** now states the client's delivery contract: pull as a
  stream, hash while reading, deliver, then purge. It also states what
  the sandbox does **not** do — it does not stream a file that is not
  finished, it is not a file host, and it is not a security boundary —
  and it notes the channel-side limit that bounds any transfer:
  Telegram's `sendDocument` accepts 50 MB through the public Bot API, so
  a sandbox limit above that is a limit nobody can use.

### Fixed

- **`agent_api.py`** — the file endpoints were missing from the module's
  `Public API` docstring, the one place a maintainer looks first.

## [2.6.0] - 2026-10-25

Two budgets and a door for bytes.

The stdout limit was a number in a file that no one could keep honest:
the tmux scrollback behind it held ~400 KB, the capture depth was sized
for 1 MB, and the completion marker was matched against the *echo* of the
command that printed it — so the poll could stop before the command had
produced anything. The result was a stdout that was sometimes a tail of
the output, sometimes the whole session transcript, and never reliably
the same twice.

The binary door is the other half: an agent that produced a file had
exactly one way to get it out — base64 through the shell, at 4/3 the
size, into a pane that drops its oldest lines without saying so.

### Fixed

- **The completion marker was matched in the command's *echo*, not in its
  printed form.** The shell reflects the typed line into the pane, so the
  marker string was visible long before the command had run. The poll
  stopped on that echo and the caller received a truncated stdout with no
  marker and no error. The marker is now matched in its printed form, as
  a whole line — a match that fires on the echo is a match that fires
  early.
- **`stdout` of a command was the whole session transcript.** The capture
  took the pane's tail, and the pane's tail is every command the session
  ever ran. A 200-byte command could come back carrying half a megabyte of
  somebody else's history. The capture is now bounded by how much the
  scrollback grew during the command, and the result is cut at a marker
  printed before it ran.
- **A lost beginning was invisible.** When a pane overflows it drops its
  *oldest* lines, so the caller used to receive the tail of the output
  dressed up as a whole answer. A begin marker printed before the command
  proves the capture still holds the start of the output; when it is
  gone, the result says so in its first line.
- **The protocol's own bookkeeping was part of the output.** The shell
  echoes the line that captures the exit code and the line that prints
  the completion marker; both are now cut out of the result.

### Added

- **`AGENT_STDOUT_LIMIT`** — how much text one command may return in one
  exec. Default `512k`, allowed range `32k`..`1024k`, `k`/`m`/`g` suffixes
  or a plain byte count. A value outside the range is a **startup
  failure**, not a clamp: a limit nobody can trust is worse than a
  container that refuses to boot.
- **`AGENT_BINARY_LIMIT`** — how large a file the binary endpoint will
  serve. Default `2048k`, `0` means unlimited. Binary never travels
  through stdout, so this is a policy cap and not a transport limit.
- **`GET /files?path=`** — streams a workspace file out as **raw bytes**.
  No base64, no terminal pane, no scrollback: the bytes are read from
  disk and written to the socket as they are read, so the agent's memory
  use does not depend on the file's size. A single `Range` header is
  honoured (206 + `Content-Range`), so a file that is too large for one
  request can be pulled in pieces. The path is resolved inside the
  workspace with symlinks resolved first; a path that points outside is
  refused.
- **`GET /files/meta?path=`** — size, name, mtime and mime of a
  workspace file, so a client can check the policy limit *before* it
  starts moving a gigabyte.
- **`GET /files` and `GET /files/meta` on the proxy**, relayed as a
  stream. The upstream status is relayed too: a file over the limit is a
  413 with its real size in the message, a missing file is a 404 — not a
  500. The file door is deliberately **not** rate limited, because a
  chunked transfer would otherwise fail for a reason that has nothing to
  do with the transfer.
- **`tests/test_exec_budget.py`** — the tripwires for the above: the
  completion marker must match the printed form, a lost beginning must be
  reported, and — against a real tmux — a ~400 KB output must come back
  whole on every run, not most of the time.
- **`tests/test_binary_proxy.py`** — the relay must stream, must forward
  the client's `Range`, and must not flatten 404 or 413 into a 500.
- **CI `deploy` job: the binary round trip.** A file created in the
  sandbox is pulled back out through the proxy and compared by sha256;
  the headers a client needs to plan a transfer are asserted, a range
  request must answer 206 with the right slice, and an oversized file
  must answer 413 naming its real size. A unit test with a mocked
  transport cannot prove that the two services agree on the bytes.
- **CI installs tmux**, so the live tripwires actually run. The runner
  image has no tmux, so every test that needed a real one skipped — and
  the tests that need a real one are the ones that matter: a ~400 KB
  command output must come back whole on *every* run, and a session must
  survive a cold tmux server. A gate that silently stops guarding the
  thing it was written for reads green and guards nothing.

### Changed

- **The stdout default is `512k`, not 1 MB.** The old `MAX_OUTPUT_BYTES`
  of 1 000 000 was never achievable: the scrollback behind it held
  ~400 000 characters, so the advertised limit was a promise the system
  could not keep. The limit is now derived from the scrollback, which is
  derived from the limit.
- **The tmux scrollback is sized from the stdout budget** instead of
  being left at tmux's default of 2000 lines. It is sized for lines of
  64 bytes rather than full-width ones, so an ordinary text file — not
  just a wide one — can be returned whole.
- **The `history-limit` is set at session creation.** It is a session option
  in tmux and it applies only to panes created *after* it is set, so
  setting it afterwards did nothing. The session's first pane is therefore
  replaced: the option is set on the session, a new pane is created to
  inherit it, and the first one is dropped. A server that has just started
  also refuses commands for a moment, and a server with no sessions exits
  — so the depth could not simply be set on the server before the session
  existed, and a `\;` chain of all three commands failed about one cold
  start in twenty.
- **`/sessions/{id}/capture` is bounded by the same budget** as `/exec`.
  It was a door around the limit: a client could pull three times the
  budget through it, and nothing would say so.

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

- **`docker-compose.production.yml` — a volume at `/home/agent` froze the
  agent code on first deploy.** The application code lives in `/home/agent`
  (it is the WORKDIR so the import path resolves), so a named volume mounted
  there shadows `agent_api.py` / `agent_core.py` with the copy Docker seeds
  from the image on first use — and never re-syncs. Every later image rebuild
  was silently a no-op. Removed; the writable paths the agent needs are
  mounted separately. The header now also documents `up -d --build`, since
  neither service pins an image and plain `up -d` reuses the existing tag.

### Changed

- `requirements.txt` keeps the existing `fastapi>=0.110,<1.0` range. The dead
  kwarg was removed from the application rather than pinned around.
- `docker-compose.production.yml` is the llmhost1 telegram-bot side-car
  deployment: loopback-only proxy bind, per-service resource caps, and `tb-`
  prefixed names. Derived from the bot branch and corrected before shipping.

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
