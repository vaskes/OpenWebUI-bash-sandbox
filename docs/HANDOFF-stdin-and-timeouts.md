# Handoff: the command's stdin and the timeout race

This is the sidecar half of the "stdin eats the marker / timeout race"
handoff. The bot side has its own half; this document is everything the
bot side needs to know about **this** repository.

- **Tag:** `v2.14.8` (this contract was written against `v2.10.2`; the
  behaviour it describes has not changed since — see the note below)
- **Agent version (`agent_core.__version__`):** `2.14.8`
- **Proxy version (`proxy_core.__version__`):** `2.14.8`
- **The API surface did not change.** No route was added, removed or
  renamed *by the work this document describes*.
  `POST /sessions/{id}/exec` and the authorization contract are
  exactly as before. Later releases added two things that are not part
  of this contract: the MCP door (`POST /mcp`, see
  `docs/HANDOFF-mcp.md`) and the metrics endpoint (`GET /metrics`, added
  in v2.12.1 on both services). Neither touches the exec contract.

---

## 1. What was wrong, in one paragraph each

**A command that reads stdin ate the completion marker.** The agent
types the command and the two bookkeeping lines that follow it into the
same terminal, so the rest of the block is still sitting in the input
buffer when the command starts. A command that reads stdin — `read x`,
`apt-get` — reads those lines instead of waiting, the completion marker
never appears, and the caller waits out the whole limit for an answer
that never comes. Before v2.10.2: `read x` produced
`{"error": "sandbox-agent request failed: "}` after 60 seconds, and
`apt-get install -y cowsay` installed the package and *lost the answer*.

**The proxy and the agent used the same number.** The proxy asked the
agent for a 60-second command and gave up waiting after 60 seconds too.
The agent's answer — "this timed out", with the output it managed to
produce — arrived at the very moment the proxy stopped listening, so it
was discarded and the caller saw a transport error with an **empty
reason**. The empty reason is the part that hurts: it sends the
operator to the network instead of to the clock.

**The startup reaper logged a list with `%d`.** That raised a TypeError
inside the logging machinery, which swallowed the message it was trying
to print. The reaper now says how many sessions it dropped and which
ones.

## 2. What changed

| Area | Before | After |
|------|--------|-------|
| The command's stdin | the terminal — it could read the protocol | `/dev/null` — it reads EOF |
| The exec limit | 60 s, hard-coded in the proxy | `AGENT_EXEC_TIMEOUT`, default 60 s, range 5..3600 |
| The proxy's wait | 60 s — the same as the limit | limit + 15 s, always strictly larger |
| A timeout's error | `"sandbox-agent request failed: "` (empty) | names the timeout and the numbers |
| A transport error's error | `"sandbox-agent request failed: {exc}"` | `"...: {exc!r}"` — the exception class is in the text |

The command now runs inside a brace group whose stdin is `/dev/null`.
The group is transparent to the caller: the stdout is byte-for-byte what
it was before, including the shell's echo of the command line.

**One gap, stated plainly.** A command that contains a brace bash would
read as a group delimiter — `echo }`, `echo {` — is run *unwrapped*,
exactly as it was before v2.10.2. Wrapping it would change what the
command means, and a command that means something else is worse than a
command that waits. Such a command can still eat the protocol, and it
still times out. The scan is conservative on purpose: it reports only
braces it is sure about (`echo '}'`, `echo a}b`, `find -exec {} \;` and
`${HOME}` are all grouped normally).

## 3. The stdout contract did not change

This is the part the bot side has to be able to rely on:

- `stdout` is still the command's output, with the shell's echo of the
  command line at the top — exactly as before, byte for byte.
- The protocol is still invisible: no marker, no bookkeeping line, no
  group line, no `/dev/null`.
- The truncation marker (`[output truncated at N bytes]`) and the
  "the beginning no longer fits in the scrollback" notice are
  unchanged.
- The response fields are unchanged: `stdout`, `stderr`, `exit_code`,
  `timed_out`, `duration_ms`.
- On a timeout: `exit_code` is `null` and `timed_out` is `true`, and
  the partial output comes back. That is the answer the bot used to
  lose.

The **door's** status semantics did not change either: `/files` still
answers 200 / 206 / 400 / 403 / 404 / 413, `/files/meta` relays the
upstream status, and `DELETE /files` still answers 204 / 400 / 401 /
404.

## 4. The new knob

The exec limit is now a knob on the **proxy** service. The exact lines
added to `docker-compose.production.yml`:

```yaml
      # The longest a single command may run. The proxy waits longer than this,
      # so the answer that says "this timed out" survives the trip.
      - AGENT_EXEC_TIMEOUT=${AGENT_EXEC_TIMEOUT:-60}
```

- **Name:** `AGENT_EXEC_TIMEOUT`
- **Default:** `60`
- **Unit:** seconds
- **Range:** 5..3600. A value outside it is a **startup failure**, not a
  clamp.
- **Meaning:** the longest a single command may run before the agent
  gives up on it and answers `timed_out: true` with the output it
  produced. The proxy then waits `AGENT_EXEC_TIMEOUT + 15` seconds for
  that answer.

The same knob is in `docker-compose.yml` and `docker-compose.ci.yml`,
and it is documented in `docs/CONFIGURATION.md` (`AGENT_EXEC_TIMEOUT`)
and in `.env.example`.

**Why the proxy, and not the agent.** The limit is *chosen* by the
proxy (it asks the agent for it) and *enforced* by the agent (its
`ExecRequest.timeout` field). The proxy's own hard cap on that field is
3600 seconds, which is the same ceiling as the knob's range — the proxy
can never ask for a timeout the agent would refuse.

## 5. What the bot side may want to do with this

- The bot's tool description no longer needs a warning about stdin. A
  command that reads stdin now gets EOF instead of eating the answer.
- The bot can stop treating an empty error reason as "the side-car is
  unreachable". The proxy now says what happened: a timeout names the
  timeout, a transport error names the exception.
- If the bot wants a longer command than 60 seconds, it can ask the
  operator to raise `AGENT_EXEC_TIMEOUT` — the knob is documented and
  quoted above. Raising it in the bot's own deployment is the
  operator's call, not the bot's.

## 6. How it was verified

- `tests/test_stdin_handoff.py` — 24 cases, of which the live ones run
  against a real tmux: `read x` answers at once with `exit_code: 1` and
  `timed_out: false`; a pipeline, a heredoc, a `cd` and a command with a
  stray brace all still work; the stdout does not grow a group line.
- `tests/test_exec_timeout_race.py` — 10 cases: the proxy waits longer
  than the limit, the wait is the limit plus the grace, the limit is
  configurable, an out-of-range value is refused, and a timeout and a
  transport error both name the cause.
- `tests/test_agent_logging.py` — 2 cases: the reaper's line says how
  many sessions it dropped and which ones.
- The CI `deploy` job runs `read x` through the proxy and requires the
  answer to arrive in under 20 seconds, with `timed_out: false` and
  `exit_code: 1`, and checks that neither `{` nor `/dev/null` leaked
  into the stdout.
- The CI `privilege` job installs a package through the agent's own API
  and requires the answer to come back with the install's output.
