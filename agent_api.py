#!/usr/bin/env python3
"""Sandbox Agent — persistent tmux sessions per chat, exposed via HTTP API.

Each HTTP session is backed by a tmux session with a stable name
``agent_<sanitized_session_id>``. The shell state (cwd, env vars, history,
running processes) persists across calls within the same session_id, so an
LLM can incrementally build up work in a sandboxed environment.

Security model
--------------
* ``cwd`` is constrained to a whitelist of real (symlink-resolved) paths
  rooted at :data:`WORKSPACE`. Any path outside the whitelist is rejected
  with 400 — prevents a caller from breaking out into ``/etc`` via
  symlinks in :data:`WORKSPACE`.
* Session state is persisted to :data:`SESSIONS_FILE` so a container
  restart does not silently lose the per-chat shell.
* Every command execution is logged with structured fields (session_id,
  command, exit_code, duration_ms) to stdout in JSON-friendly format.
* Resource caps (``mem_limit``, ``cpus``, ``pids_limit``) are applied at
  the Docker level — see ``docker-compose.yml``.

Public API
----------
GET  /healthz
    Liveness probe.
GET  /sessions
    List active sessions, with stale sessions reaped.
POST /sessions
    Create or attach to a session (cwd must be inside WORKSPACE).
DELETE /sessions/{session_id}
    Kill a session and remove it from the registry.
POST /sessions/{session_id}/exec
    Run a shell command in the session, return stdout and exit code.
GET  /sessions/{session_id}/capture?lines=N
    Tail the tmux pane directly (no shell wrapper — cheap polling target).
GET  /sessions/{session_id}/output?lines=N
    Alias of ``/capture`` kept for backwards compatibility.
GET  /files/meta?path=P
    Size, mtime, name and mime of a workspace file.
GET  /files?path=P
    The file's **raw bytes** — no base64, no terminal pane. Honours a
    single ``Range`` header. A file the agent's own process cannot read
    (created by root, say) is a 403, decided before the response starts —
    never a 200 with a truncated body.
DELETE /files?path=P
    Remove a file from the workspace. The sandbox is a scratchpad, not
    storage: once a file has been delivered, the client can remove it.
GET  /metrics
    Prometheus exposition (requests, refusals, active sessions).
POST /mcp
    The Model Context Protocol endpoint: the same sandbox, in the
    protocol an MCP caller speaks. One tool per capability — ``bash``,
    ``read_file``, ``file_info``, ``delete_file`` — and the same bearer
    token as every other route. See ``mcp_server.py``.
GET  /mcp
    The MCP event stream (part of the Streamable HTTP transport).
DELETE /mcp
    Terminate an MCP session (part of the Streamable HTTP transport).
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import mimetypes
import os
import re
import shutil
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.routing import Route as StarletteRoute

from agent_config import (
    BINARY_LIMIT_BYTES as _BINARY_LIMIT_BYTES,
)
from agent_config import (
    CAPTURE_DEPTH_LINES,
    HISTORY_LIMIT_LINES,
    PANE_HEIGHT,
    PANE_WIDTH,
    STDOUT_LIMIT_BYTES,
)
from agent_core import (
    CONTINUATION_PROMPT,
    SESSION_ID_MAX_LEN,
    __version__,
    build_exec_block,
    command_would_stick,
    is_a_continuation_prompt,
    is_a_shell_name,
    looks_like_a_bare_prompt,
    new_marker_token,
    parse_capability_status,
    redact_secrets,
    rootfs_is_read_only,
    safe_id,
    sudo_posture,
    tmux_name,
)
from body_limit import BodySizeLimit
from json_logging import configure_logging
from metrics import Metrics

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
#: The default timeout of the HTTP door, in seconds.
#:
#: It is shorter than the proxy's default (``proxy_core``) on purpose:
#: the agent's number applies to a caller that talks to the agent
#: directly and says nothing about how long it intends to wait, and the
#: shorter default is the one that gives a model its answer back. The
#: proxy asks for its own number explicitly, so the two defaults never
#: meet: what the proxy requests is what the agent runs.
DEFAULT_TIMEOUT = 30  # seconds
#: The hard cap on one command, in seconds.
#:
#: It was 600 (10 min) until 2026-10-27. The operator's ruling raised
#: it to an hour, because the limit he measured was the proxy's, and
#: the proxy could not ask for more than this. The two numbers are now
#: equal on purpose: a caller may ask for anything the machine accepts,
#: and the machine accepts an hour.
MAX_TIMEOUT = 3600  # 60 min hard cap
MAX_COMMAND_LEN = 1_000_000  # 1 MB hard cap on a single command
#: How much text one command may return. Configurable via
#: ``AGENT_STDOUT_LIMIT`` (32k..1024k, default 512k) — see agent_config.
#: The name is kept because callers and tests patch it.
MAX_OUTPUT_BYTES = STDOUT_LIMIT_BYTES
#: How far back a capture may reach. Derived from the stdout budget: a
#: capture that cannot cover the whole scrollback truncates what the pane
#: still holds, which is the same silent loss one layer up.
CAPTURE_PANE_LINES = CAPTURE_DEPTH_LINES
WORKSPACE = os.environ.get("AGENT_WORKSPACE", "/workspace")
SESSIONS_FILE = os.environ.get("AGENT_SESSIONS_FILE", "/workspace/.agent-sessions.json")
#: Largest file the binary endpoint will serve. 0 means no limit. See
#: agent_config.
BINARY_LIMIT_BYTES = _BINARY_LIMIT_BYTES
# Bearer token for the agent's HTTP API. The agent refuses all traffic
# if this is unset — fail-closed by design. The proxy and agent must
# share the same value (mirror via the same secret in your orchestrator).
AGENT_TOKEN: str = os.environ.get("AGENT_TOKEN", "")

logger = logging.getLogger("sandbox-agent")
# One JSON line per record, with the message escaped properly. The old
# format string interpolated the message into a JSON string, so a
# command containing a quote produced a line no parser could read.
configure_logging(os.environ.get("AGENT_LOG_LEVEL", "INFO"))

#: What the agent exposes on ``/metrics``. The gauge is read at scrape
#: time, so it reflects the sessions that exist right now.
METRICS = Metrics("sandbox_agent")
METRICS.counter("exec_total", "Commands the agent executed.")
METRICS.counter("exec_timeouts_total", "Commands that ran out of time.")
METRICS.counter(
    "exec_refused_total",
    "Commands refused before they were typed because they would stick the shell.",
)
METRICS.counter(
    "exec_stuck_resets_total",
    "Shells freed after they waited for the rest of an unfinished command.",
)
METRICS.gauge(
    "active_sessions",
    "Sessions the agent currently holds.",
    lambda: float(len(SESSIONS)),
)


# ---------------------------------------------------------------------------
# Persistent session registry
# ---------------------------------------------------------------------------
def _load_sessions() -> dict[str, dict[str, Any]]:
    """Load the session registry from disk; return empty dict on missing/corrupt.

    A corrupt or unreadable file is logged at ``error`` level (not
    ``warning``) because a missed load means the LLM will lose track of
    sessions that previously existed — silent data loss.
    """
    try:
        with open(SESSIONS_FILE, encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
        logger.error("sessions file root is not an object; starting fresh")
    except FileNotFoundError:  # noqa: B110 - first run, file doesn't exist
        pass
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("sessions file unreadable (%s) — possible data loss", exc)
    return {}


async def _save_sessions() -> None:
    """Atomically persist the session registry to disk off the event loop.

    Synchronous disk I/O would block every concurrent request — wrap in
    ``asyncio.to_thread`` so a slow host filesystem doesn't stall the API.
    Best-effort: a failed write is logged, not raised.
    """

    def _do() -> None:
        tmp = f"{SESSIONS_FILE}.tmp.{uuid.uuid4().hex[:8]}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(SESSIONS, f)
        os.replace(tmp, SESSIONS_FILE)

    try:
        await asyncio.to_thread(_do)
    except OSError as exc:
        logger.error("failed to persist sessions: %s", exc)


#: The session registry.
#:
#: One process, one registry — and that is a promise, not an
#: accident. The agent runs a single uvicorn worker (see the
#: Dockerfile); with more than one, every worker would hold its own
#: copy of this dict and its own view of which sessions exist. The
#: registry is written to disk on every change, so the file is the
#: shared truth — but two workers would still disagree about what is
#: alive. Adding a worker means adding a lock *and* a decision about
#: who owns the tmux server; it is not a flag to flip.
SESSIONS: dict[str, dict[str, Any]] = _load_sessions()


# ---------------------------------------------------------------------------
# Path-traversal protection
# ---------------------------------------------------------------------------
def _resolve_workspace_root() -> str:
    """Return the real (symlink-resolved) workspace root, falling back to the
    configured value if it does not exist yet (e.g. first boot)."""
    p = Path(WORKSPACE).resolve()
    return str(p) if p.exists() else WORKSPACE


WORKSPACE_REAL = _resolve_workspace_root()

#: The prefix every path the agent serves has to start with. The trailing
#: separator is part of the prefix on purpose: without it a sibling
#: directory whose name merely *starts with* the workspace's name
#: ("/workspace-evil") would pass the check, and the comparison would be
#: weaker than the ``commonpath`` check it replaces. With the separator,
#: and after the path has been resolved (no "..", no symlinks), the two
#: checks agree — and the prefix can be read by a human and by a static
#: analyser, which is worth more than a helper call.
WORKSPACE_PREFIX = WORKSPACE_REAL if WORKSPACE_REAL.endswith(os.sep) else WORKSPACE_REAL + os.sep


def _persisted_cwd(sid: str) -> str | None:
    """The directory a known session had, if it is still usable.

    A session that is known but not alive — the container restarted, or
    the pane died — comes back in the directory it had.  The lifespan
    handler promises exactly that ("the auto-create path recreates a
    tmux backend ... using the persisted cwd"), and the promise was not
    kept: the session came back in the workspace root, and the registry
    recorded the root as if that were where the session had always
    been.  A caller that had ``cd``-ed somewhere and came back to a
    dead session found its working directory silently changed.

    A directory that is gone (or is not inside the workspace any more)
    is not a directory to come back to: the session starts in the
    workspace root instead.
    """
    remembered = SESSIONS.get(sid, {}).get("cwd")
    if not remembered:
        return None
    try:
        return _safe_cwd(remembered)
    except HTTPException:
        return None


def _safe_cwd(candidate: str) -> str:
    """Resolve symlinks and confirm the path is rooted at the workspace.

    Raises :class:`HTTPException` (400) for any path that is not a directory
    inside :data:`WORKSPACE_REAL`. This blocks:

    * absolute paths outside the workspace (e.g. ``/etc``),
    * ``..`` traversal (e.g. ``/workspace/../etc``),
    * symlinks pointing outside the workspace.
    """
    real = os.path.realpath(candidate)
    if real == WORKSPACE_REAL:
        # The root itself: a constant comes back, so nothing the caller
        # supplied travels with it.
        return WORKSPACE_REAL
    if real.startswith(WORKSPACE_PREFIX):
        # The check on the filesystem comes *after* the containment
        # check, and inside it: the path is already known to be inside
        # the workspace, so the question that is left is only whether it
        # is a directory. (It also keeps the flow of the data obvious:
        # resolve, confirm the containment, then touch the file.)
        if not os.path.isdir(real):
            raise HTTPException(status_code=400, detail=f"cwd is not a directory: {candidate}")
        return real
    raise HTTPException(status_code=400, detail=f"cwd outside workspace: {candidate}")


# ---------------------------------------------------------------------------
# tmux plumbing
# ---------------------------------------------------------------------------
async def _tmux(*args: str) -> tuple[int, str, str]:
    """Run a tmux command asynchronously. Returns ``(returncode, stdout, stderr)``."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


async def tmux_session_exists(name: str) -> bool:
    """Return True if a tmux session with the given name currently exists."""
    rc, _, _ = await _tmux("has-session", "-t", name)
    return rc == 0


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
def _warn_about_the_privilege_mode(
    status: str | None = None,
    mountinfo: str | None = None,
) -> None:
    """Say something when sudo is installed — whether or not it works.

    The privilege mode (docs/SECURITY.md) has two halves: the image
    ships sudo, and the container's posture lets the setuid bit do its
    job. Turn on only one of them and the model gets an error it cannot
    interpret — *"sudo: the 'no new privileges' flag is set"* reads like
    a broken sandbox, not like a misconfiguration.

    A warning at startup is the difference between "the deployment is
    misconfigured" and "the sandbox is flaky".

    This is a plain function and it is called as one. It used to carry
    ``@asynccontextmanager``, which was wrong twice over: the decorator
    makes the *call* hand back a context manager, and the body only
    ran because the decorator's constructor called the function on the
    way there. ``async with`` on it raised ``TypeError: 'NoneType'
    object is not an async iterator`` — a plain function is not an
    async generator. The warning did print; it printed by accident, and
    a reader of the old code could not tell that from "it never runs".

    ``status`` and ``mountinfo`` are injectable for the tests; ``None``
    means "read it from /proc".
    """
    if shutil.which("sudo") is None:
        return
    if status is None:
        try:
            status = Path("/proc/self/status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.warning(
                "sudo is installed but /proc/self/status could not be read, "
                "so the privilege mode cannot be verified"
            )
            return
    no_new_privs, cap_bnd = parse_capability_status(status)
    verdict = sudo_posture(no_new_privs, cap_bnd)
    if verdict != "ok":
        logger.warning(
            "sudo is installed but the escalation would NOT work: %s — "
            "set AGENT_NO_NEW_PRIVILEGES=false and AGENT_CAP_DROP=SYS_ADMIN "
            "in the sandbox's environment and recreate the agent",
            verdict,
        )
    else:
        logger.warning(
            "the privilege mode is ON: a command in this sandbox can run "
            "`sudo su` and become root inside the container. "
            "PRIVATE, SECURED DEPLOYMENTS ONLY."
        )
    _warn_about_the_read_only_rootfs(mountinfo)


def _warn_about_the_read_only_rootfs(mountinfo: str | None = None) -> None:
    """Say something when the root shell would own a filesystem it cannot write.

    Escalation and usefulness are two different things. The privilege
    mode hands the model a root shell; in a read-only container that
    shell cannot install a package, edit ``/etc`` or write to ``/usr`` —
    ``apt-get`` needs all three. A model that tries ``apt-get install``
    gets *"Read-only file system"* and concludes the sandbox is broken,
    while the deployment is merely a half-root.

    A warning at startup is the difference between "the deployment is
    misconfigured" and "the sandbox is flaky".

    ``mountinfo`` is injectable for the tests; ``None`` means "read it
    from ``/proc/self/mountinfo``".
    """
    if mountinfo is None:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.warning(
                "/proc/self/mountinfo could not be read, so the root filesystem's "
                "writability cannot be verified"
            )
            return
    if rootfs_is_read_only(mountinfo):
        logger.warning(
            "the root filesystem is READ-ONLY: a root shell in this container "
            "cannot install packages (apt-get), edit /etc or write to /usr. "
            "Set AGENT_READ_ONLY=false in the sandbox's environment and "
            "recreate the agent if the model needs a usable root."
        )


async def lifespan(app: FastAPI):  # noqa: ARG001
    """Lifespan handler — startup/shutdown hooks.

    On **startup** we run a *reaper* pass: any session_id in
    :data:`SESSIONS` whose underlying tmux session has died is removed
    from the in-memory registry and the on-disk file is rewritten. This
    keeps stale entries from accumulating when the container is killed
    abruptly and the tmux server takes the sessions with it.

    On startup we do **not** kill tmux sessions either — the persisted
    registry is the source of truth and existing sessions will be
    re-attached on the first call to ``/sessions/{id}/exec`` (the
    auto-create path recreates a tmux backend if the session_id is known
    but the tmux process has died, using the persisted cwd).

    On shutdown we also **do not** kill tmux sessions. They are owned by
    the ``tmux`` server process, not by us — if the container is killed
    abruptly the kernel reaps them; if it shut down cleanly the next
    process can ``attach`` again. Killing on shutdown was the v2.1
    behaviour and it broke persistence across restarts.
    """
    reaped = await _reap_dead_sessions()
    _log_reaped_sessions(reaped)
    _warn_about_the_privilege_mode()
    # The MCP transport runs in a task group of its own, and it refuses
    # requests until that group exists. A mounted application does not
    # get a lifespan of its own, so the agent opens it.
    async with _mcp_lifespan():
        try:
            yield
        finally:
            # Best-effort flush so an in-flight session add doesn't get lost
            # on a clean restart. Skip if the loop is already shutting down.
            if SESSIONS:
                try:
                    await _save_sessions()
                except Exception as exc:  # noqa: BLE001
                    logger.error("final sessions flush failed: %s", exc)


def _log_reaped_sessions(reaped: list[str]) -> None:
    """Log the sessions the startup reaper dropped.

    The count and the ids are two different things: the count is a
    number, the ids are a list. Formatting the list with ``%d`` raised a
    TypeError inside the logging machinery, which swallowed the message
    and printed a formatting error instead of the line that says which
    sessions were dropped.

    The list is truncated: a machine that lost a thousand sessions
    would otherwise print a line a thousand ids long, and the log line
    that nobody can read is the same as no line at all. The count in
    the same line stays the true count.
    """
    if reaped:
        shown = ", ".join(reaped[:10])
        if len(reaped) > 10:
            shown += f", ... (+{len(reaped) - 10} more)"
        logger.info(
            "startup reaper dropped %d stale session(s): %s",
            len(reaped),
            shown,
        )


async def _reap_dead_sessions() -> list[str]:
    """Drop registry entries whose tmux backend no longer exists.

    Returns the list of session_ids that were removed. Single
    ``tmux list-sessions -F '#{session_name}'`` is O(1) round-trips
    regardless of registry size — the LLM can build up hundreds of
    chat sessions and we don't want startup to take seconds.
    """
    rc, out, _ = await _tmux("list-sessions", "-F", "#{session_name}")
    alive: set[str] = set()
    if rc == 0:
        alive = {line.strip() for line in out.splitlines() if line.strip()}
    dead: list[str] = []
    for sid, info in list(SESSIONS.items()):
        if info.get("tmux_name") not in alive:
            SESSIONS.pop(sid, None)
            dead.append(sid)
    if dead:
        await _save_sessions()
    return dead


app = FastAPI(
    title="Sandbox Agent",
    version=__version__,
    description=__doc__,
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request body size limit — defense in depth
# ---------------------------------------------------------------------------
# Reject any request whose body exceeds :data:`MAX_BODY_BYTES` with
# HTTP 413. The Pydantic ``max_length`` on the command field caps the
# *command* at 1 MB, but a hostile client could send a body that
# *parses* to a small command and yet weighs megabytes of padding.
# Without this guard the body lands in memory before Pydantic sees
# it. 2 MB is a generous ceiling (command is capped at 1 MB; the rest
# is JSON overhead).
#
# The guard counts the bytes as they arrive, not just the ones the
# client declared: a chunked body has no length to read, and reading
# only the header leaves exactly the case the limit exists for open.
# See :mod:`body_limit` for why it is an ASGI middleware.
MAX_BODY_BYTES = 2 * 1024 * 1024

app.add_middleware(BodySizeLimit, limit=MAX_BODY_BYTES)


# ---------------------------------------------------------------------------
# Security headers — defense in depth
# ---------------------------------------------------------------------------
# The agent is bearer-token-authed and serves a tiny JSON surface, so most
# of the OWASP header set is irrelevant. We add the two that matter:
#   * X-Content-Type-Options: nosniff — stops the browser from MIME-sniffing
#     a JSON response into something executable if an attacker tricks a
#     user into opening the URL directly.
#   * Cache-Control: no-store — bearer-token responses must not be cached
#     by an intermediate proxy.
# X-Frame-Options / CSP / HSTS are intentionally omitted: this service
# has no UI and is not loaded in a browser.
@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
_bearer = HTTPBearer(auto_error=False)


def _require_token(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008
) -> str:
    """Bearer-token gate for every endpoint except ``/healthz``.

    The agent refuses to start serving if :data:`AGENT_TOKEN` is empty —
    fail-closed prevents an accidental unauthenticated deployment.
    Constant-time compare defeats timing attacks.
    """
    if not AGENT_TOKEN:
        logger.error("auth disabled but AGENT_TOKEN is empty — refusing all requests")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="agent not configured"
        )
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not _token_ok(creds.credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return creds.credentials


def _token_ok(credentials: str | None) -> bool:
    """True when ``credentials`` are the agent's token.

    One comparison, two doors. The HTTP gate and the MCP gate both come
    here, so the two cannot drift apart — a token that opens the HTTP
    door opens the MCP door, and nothing else does.

    Constant-time compare defeats timing attacks. An empty token means
    "not configured", and then nothing matches: the agent is fail-closed
    and the MCP door is closed with it.
    """
    if not AGENT_TOKEN or not credentials:
        return False
    import hmac as _hmac

    return _hmac.compare_digest(credentials, AGENT_TOKEN)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Liveness probe (no auth — required for k8s/docker healthchecks).

    Returns the minimum information needed for a probe to determine
    liveness. The workspace path and active-session count are *not*
    exposed here because the endpoint is unauthenticated — leaking
    the workspace path to an unauthenticated prober is a minor info
    leak that costs us nothing to remove. Operators who need the
    full diagnostic should hit :func:`list_sessions` with a bearer
    token instead.
    """
    return {"status": "ok"}


@app.get("/metrics", dependencies=[Depends(_require_token)])
async def metrics() -> Response:
    """Prometheus exposition.

    Authenticated like every other endpoint: the numbers are harmless,
    but an unauthenticated endpoint on a reachable port is a habit
    worth not having. Prometheus reads the token from a file
    (``bearer_token_file``) — see ``docker-compose.monitoring.yml``.
    """
    return Response(
        content=METRICS.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/sessions", dependencies=[Depends(_require_token)])
async def list_sessions() -> dict[str, Any]:
    """List active sessions, reaping any whose tmux backend has died.

    Implementation note: a single ``tmux list-sessions -F '#{session_name}'``
    is O(1) round-trips regardless of how many sessions we know about.
    A naive O(n) loop of ``tmux has-session`` calls would be slow once
    the LLM has built up hundreds of chat sessions.
    """
    rc, out, _ = await _tmux("list-sessions", "-F", "#{session_name}")
    alive: set[str] = set()
    if rc == 0:
        alive = {line.strip() for line in out.splitlines() if line.strip()}
    dead: list[str] = []
    for sid, info in SESSIONS.items():
        if info["tmux_name"] not in alive:
            dead.append(sid)
    for sid in dead:
        SESSIONS.pop(sid, None)
    if dead:
        await _save_sessions()
    return {
        "sessions": [
            {
                "session_id": sid,
                "tmux_name": info["tmux_name"],
                "cwd": info.get("cwd"),
                "created": info["created"],
            }
            for sid, info in SESSIONS.items()
        ]
    }


class CreateSession(BaseModel):
    """Request body for ``POST /sessions``."""

    session_id: str = Field(default="default", max_length=SESSION_ID_MAX_LEN)
    cwd: str | None = Field(
        default=None, description="Initial working directory. Must be inside the workspace."
    )


@app.post("/sessions", dependencies=[Depends(_require_token)])
async def create_session(body: CreateSession) -> dict[str, Any]:
    """Create a new tmux-backed session or attach to an existing one.

    The ``cwd`` field, if provided, is symlink-resolved and must lie inside
    :data:`WORKSPACE_REAL`. This is the only path-traversal protection in
    the service — never loosen it without an explicit threat-model update.
    """
    sid = safe_id(body.session_id)
    name = tmux_name(sid)
    requested = _safe_cwd(body.cwd) if body.cwd else None
    if await tmux_session_exists(name):
        # The pane is alive. Two cases, and they used to be one:
        #
        # * the registry knows the session — nothing to do;
        # * the registry does not know it (the sessions file is rebuilt
        #   after a restart and it can be stale) — the session is
        #   **adopted**, not replaced. The old code created a new window
        #   in the requested directory and killed the old one, so the
        #   shell state the user had built up (a ``cd``, an ``export``, a
        #   running build) was silently destroyed, and the registry then
        #   recorded the requested directory instead of the pane's real
        #   one.
        live = await _pane_cwd(name)
        if requested and live and requested != live:
            # The directory of a running session is not a parameter the
            # caller can set from the outside. Saying so is the only
            # honest answer: the alternative is running the command
            # somewhere the caller did not ask for, without a word.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"the session {sid} is running in {live}; its directory is not a parameter "
                    f"that can be changed from the outside. Use a new session_id to run "
                    f"in {requested}."
                ),
            )
        if sid not in SESSIONS:
            adopted = live or requested or WORKSPACE_REAL
            SESSIONS[sid] = {"tmux_name": name, "created": time.time(), "cwd": adopted}
            await _save_sessions()
            logger.info("session.adopt sid=%s cwd=%s", sid, adopted)
        return {
            "session_id": sid,
            "tmux_name": name,
            "existed": True,
            "cwd": SESSIONS.get(sid, {}).get("cwd"),
        }
    cwd = requested or _persisted_cwd(sid) or WORKSPACE_REAL
    rc, _, err = 0, "", ""
    for _attempt in range(3):
        rc, _, err = await _tmux(
            "new-session",
            "-d",
            "-s",
            name,
            "-c",
            cwd,
            "-x",
            str(PANE_WIDTH),
            "-y",
            str(PANE_HEIGHT),
        )
        if rc == 0:
            break
        # A concurrent request may have created the same session in the
        # meantime; that is a success, not a failure.
        if await tmux_session_exists(name):
            rc = 0
            break
        await asyncio.sleep(0.3)
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"tmux new-session failed: {err}")

    # The scrollback depth is what makes the stdout budget deliverable,
    # and in tmux it is a session option that applies **only to panes
    # created after it is set**. The pane above was created before the
    # option could be set — the option lives on the server, and a server
    # that has just started refuses commands for a moment (and refuses
    # them for good if it has no sessions, because it exits when empty).
    #
    # So the option is set on the session, a new pane is created to
    # inherit it, and the first pane is dropped. The new pane is the
    # session's current window, so every later command goes to it.
    await _tmux("set-option", "-t", name, "history-limit", str(HISTORY_LIMIT_LINES))
    rc_window, _, _ = await _tmux("new-window", "-t", name, "-c", cwd)
    if rc_window == 0:
        # The original pane was window 0; the new one is the current
        # window and the session's only window once 0 is gone.
        await _tmux("kill-window", "-t", f"{name}:0")
    else:
        logger.warning("session.window_replace_failed sid=%s", sid)
    # Cheap insurance: the scrollback depth is what makes the stdout
    # budget deliverable, so a pane that did not inherit it is worth
    # shouting about. It is not fatal — output that does not fit is
    # reported as truncated rather than silently shortened.
    actual = await _pane_history_limit(name)
    if actual and actual != HISTORY_LIMIT_LINES:
        logger.warning(
            "session.scrollback_mismatch sid=%s pane=%s expected=%s",
            sid,
            actual,
            HISTORY_LIMIT_LINES,
        )
    SESSIONS[sid] = {"tmux_name": name, "created": time.time(), "cwd": cwd}
    await _save_sessions()
    logger.info("session.create sid=%s cwd=%s", sid, cwd)
    return {"session_id": sid, "tmux_name": name, "existed": False, "cwd": cwd}


@app.delete("/sessions/{session_id}", dependencies=[Depends(_require_token)])
async def delete_session(session_id: str) -> dict[str, str]:
    """Kill a tmux session and remove it from the registry. Idempotent."""
    sid = safe_id(session_id)
    name = tmux_name(sid)
    await _tmux("kill-session", "-t", name)
    SESSIONS.pop(sid, None)
    await _save_sessions()
    logger.info("session.delete sid=%s", sid)
    return {"deleted": sid}


class ExecRequest(BaseModel):
    """Request body for ``POST /sessions/{id}/exec``."""

    command: str = Field(
        ..., min_length=1, max_length=MAX_COMMAND_LEN, description="Shell command to execute."
    )
    timeout: int = Field(
        default=DEFAULT_TIMEOUT,
        ge=1,
        le=MAX_TIMEOUT,
        description="Max seconds to wait for completion.",
    )


@app.post("/sessions/{session_id}/exec", dependencies=[Depends(_require_token)])
async def exec_cmd(session_id: str, body: ExecRequest) -> dict[str, Any]:
    """Run a shell command in the session, return captured stdout and exit code.

    A unique marker is appended to the command so we know when the shell has
    finished producing output and can read the exit code from ``$?``. The
    marker line is stripped from the returned stdout.

    On timeout, the partial output is returned with ``exit_code=None`` and
    ``timed_out=True`` so the caller can decide whether to retry.
    """
    METRICS.inc("exec_total")
    sid = safe_id(session_id)
    name = tmux_name(sid)

    # C1 (sandbox-recommendations): log a preview of the command itself
    # in addition to its length, so post-incident audit can reconstruct
    # what the LLM actually ran. ``cmd_preview`` is truncated to 200 chars
    # so a runaway 1 MB command does not flood the log; the
    # ``cmd_len=N`` field preserves the original length for inspection.
    #
    # The preview is redacted before it is logged. A command that carries
    # a token — ``export API_KEY=…``, ``curl -H "Authorization: Bearer …"``
    # — would otherwise write that token into the log file, where it
    # stays readable by everything that can read the logs. The names
    # survive the redaction, so the log still says what the command did.
    cmd_preview = redact_secrets(
        body.command
        if len(body.command) <= 200
        else body.command[:200] + f"... (+{len(body.command) - 200} bytes)"
    )

    # A command whose text is not a complete shell command leaves the
    # session's shell waiting for the rest of the line, and the protocol
    # that follows is swallowed as part of it. The session is then stuck:
    # every later command is queued behind the unfinished one, and the
    # only thing that frees the session is a restart of the container.
    # Refuse before the command is typed — a stuck shell cannot be
    # unf-stuck by the caller.
    stuck_reason = await asyncio.to_thread(command_would_stick, body.command)
    if stuck_reason:
        METRICS.inc("exec_refused_total")
        logger.warning(
            "exec.refused sid=%s reason=%s cmd=%r",
            sid,
            stuck_reason,
            cmd_preview,
        )
        return {
            "stdout": "",
            "stderr": f"the command was not run: it {stuck_reason}",
            "exit_code": None,
            "timed_out": False,
            "duration_ms": 0,
            "refused": True,
        }

    if not await tmux_session_exists(name):
        # Auto-create — this is what makes the API forgiving for clients that
        # don't explicitly create a session first.  No directory is named
        # here: a session that is known comes back in the directory it
        # had, and a session that is not known starts in the workspace
        # root.
        await create_session(CreateSession(session_id=sid))

    marker = f"__AGENT_DONE_{new_marker_token()}__"
    # A second marker, printed BEFORE the command runs. It is the proof
    # that the capture still contains the beginning of the output. When the
    # pane overflows, tmux drops the OLDEST lines — the beginning — and
    # hands back a fragment that looks like a whole result. Without this
    # marker there is no way to tell that apart from a short command, so
    # the caller would read the tail of a file and believe it read the
    # file. With it, the loss is detected and reported.
    begin_marker = f"__AGENT_BEGIN_{new_marker_token()}__"
    # The variable that carries the exit code from the command to the
    # completion marker. It is named after a random token so that the
    # shell's echo of the bookkeeping line can be recognised and cut away
    # from the output — otherwise the caller receives the internals of
    # the protocol along with the command's own output.
    ec_var = f"__AGENT_EC_{new_marker_token()}__"
    # The command runs inside a brace group whose stdin is /dev/null, so
    # it cannot read the protocol lines that follow it in the terminal's
    # input buffer. Without that, a command that reads stdin — `read x`,
    # `apt-get` — swallows the completion marker and the caller gets a
    # timeout instead of an answer.
    full_cmd, grouped = build_exec_block(body.command, begin_marker, ec_var, marker)
    # ``send-keys -l`` writes the command as a literal string. Without
    # ``-l`` tmux interprets special tokens (``C-c``, ``Enter``, ``Tab``,
    # arrow keys, etc.) which can silently corrupt or terminate the
    # command — e.g. ``git commit -m "Press Enter to continue"`` would
    # fire the Enter mid-message. Two-step write avoids the issue
    # regardless of the command content.
    # Snapshot the scrollback depth *before* the command runs. The delta
    # after it finishes is exactly how many lines this command pushed
    # into the history, and that is what bounds the capture. Without it
    # the "stdout" of a command is the whole session transcript: the
    # scrollback is shared by every command of the session, so a plain
    # "capture the tail" hands back the previous commands' output as
    # well — which is how a 200-byte command could come back carrying
    # half a megabyte of somebody else's history.
    history_before = await _history_size(name)

    await _tmux("send-keys", "-t", name, "-l", full_cmd)
    await _tmux("send-keys", "-t", name, "Enter")

    start = time.time()
    output = ""
    # The completion marker has to be matched in its PRINTED form, as a
    # whole line: ``__AGENT_DONE_xxxxxxxx__=0``. A plain substring search
    # matches the marker inside the ECHO of the command that prints it —
    # the shell reflects the typed line into the pane, and that echo is
    # visible long before the command has produced anything. Matching the
    # echo meant the poll could stop on a pane that held a fraction of the
    # output: the capture was taken while tmux was still filling the
    # scrollback, and the caller got a truncated stdout with no marker and
    # no error. Because tmux processes the pty stream in order, seeing the
    # PRINTED marker means everything before it was already absorbed.
    printed_marker = re.compile(rf"(?m)^{re.escape(marker)}=(-?\d+)\r?$")
    # The poll only needs to see the tail, where the completion marker is.
    # Capturing the whole scrollback every 300 ms would copy megabytes per
    # poll for no reason, so the full capture happens once, at the end.
    while time.time() - start < body.timeout:
        rc, out, _ = await _tmux("capture-pane", "-t", name, "-p", "-S", f"-{_POLL_LINES}")
        if rc == 0 and printed_marker.search(out):
            break
        await asyncio.sleep(0.3)
    else:
        # Timed out — return whatever we have so the model can decide.
        capture = await _capture_command_output(name, history_before)
        output, _, head_survived = _extract_command_output(
            capture, begin_marker, marker, ec_var, grouped
        )
        if not head_survived:
            # The begin marker is not in the capture. Two very different
            # situations produce that, and they must not be confused:
            #
            #   * the command never started — the shell was busy running
            #     something else, so the input is still queued. The pane
            #     holds the PREVIOUS command's output, and handing it
            #     back as this command's stdout is a lie: the caller
            #     reads another command's output as its own.
            #   * the command's own output was so long that it pushed
            #     the marker out of the scrollback.
            #
            # The full scrollback tells them apart: the marker is still
            # in there — together with this command's complete output —
            # unless the output overflowed the history.
            full = await _capture_full_pane(name)
            full_output, _, full_head_survived = _extract_command_output(
                full, begin_marker, marker, ec_var, grouped
            )
            if full_head_survived:
                # The command did run — the bounded capture was simply
                # too small to hold its beginning.
                output = full_output
                head_survived = True
            elif not await _scrollback_is_full(name):
                # The marker was never printed and the history did not
                # overflow: the shell never got to the command. The pane
                # belongs to whoever ran before — do not hand it over.
                #
                # Two states look exactly the same from here, and they
                # need different answers:
                #
                #   * the shell is running another command — the session
                #     is busy, and this command waits in the terminal's
                #     input buffer;
                #   * the shell is waiting for the rest of an unfinished
                #     command — the session is stuck, and it stays stuck
                #     until somebody interrupts the shell.
                #
                # A pane whose last line is nothing but a prompt is the
                # second case: the shell is at a prompt, so nothing is
                # running, and interrupting it cannot kill anybody's
                # work. The first case shows the command's echo or its
                # output as the last line, and is left alone.
                if await _reset_a_stuck_shell(name):
                    duration_ms = int((time.time() - start) * 1000)
                    METRICS.inc("exec_timeouts_total")
                    METRICS.inc("exec_stuck_resets_total")
                    logger.warning(
                        "exec.stuck_reset sid=%s duration_ms=%d cmd_len=%d cmd=%r",
                        sid,
                        duration_ms,
                        len(body.command),
                        cmd_preview,
                    )
                    return {
                        "stdout": "",
                        "stderr": _STUCK_NOTICE,
                        "exit_code": None,
                        "timed_out": True,
                        "duration_ms": duration_ms,
                    }
                duration_ms = int((time.time() - start) * 1000)
                METRICS.inc("exec_timeouts_total")
                logger.warning(
                    "exec.not_started sid=%s duration_ms=%d cmd_len=%d cmd=%r",
                    sid,
                    duration_ms,
                    len(body.command),
                    cmd_preview,
                )
                return {
                    "stdout": "",
                    "stderr": _BUSY_NOTICE,
                    "exit_code": None,
                    "timed_out": True,
                    "duration_ms": duration_ms,
                }
        if not head_survived and output.strip():
            # The pane overflowed and the beginning of the output is
            # gone. Say so, loudly and first, instead of handing back a
            # fragment that reads like a complete result.
            output = _HEAD_LOST_NOTICE + output
        output = _truncate_output(output)
        duration_ms = int((time.time() - start) * 1000)
        METRICS.inc("exec_timeouts_total")
        logger.warning(
            "exec.timeout sid=%s duration_ms=%d cmd_len=%d cmd=%r",
            sid,
            duration_ms,
            len(body.command),
            cmd_preview,
        )
        return {
            "stdout": output,
            "stderr": "",
            "exit_code": None,
            "timed_out": True,
            "duration_ms": duration_ms,
        }

    # The poll saw the completion marker in the tail. Now take the full
    # capture — this is where the beginning of the output lives, and the
    # only way to tell whether it survived the scrollback.
    capture = await _capture_command_output(name, history_before)
    output, exit_code, head_survived = _extract_command_output(
        capture, begin_marker, marker, ec_var, grouped
    )
    if not head_survived and output.strip():
        # The bounded capture is sized by the *growth* of the scrollback
        # during the command, and that growth is a bad measure when the
        # scrollback was already nearly full: the command's output took
        # the place of the old lines instead of adding to them, so the
        # growth is a fraction of what the command printed — and the
        # capture stops short of the beginning.
        #
        # The whole output is in the scrollback. So the full scrollback
        # is read — the same fallback the timeout path takes — and the
        # answer is the whole output rather than a fragment with a
        # notice in front of it.
        full = await _capture_full_pane(name)
        full_output, full_exit_code, full_head_survived = _extract_command_output(
            full, begin_marker, marker, ec_var, grouped
        )
        if full_head_survived:
            output, exit_code, head_survived = full_output, full_exit_code, True
    if not head_survived and output.strip():
        # The pane overflowed and the beginning of the output is gone.
        # Say so, loudly and first, instead of handing back a fragment
        # that reads like a complete result.
        output = _HEAD_LOST_NOTICE + output
    output = _truncate_output(output)
    duration_ms = int((time.time() - start) * 1000)
    logger.info(
        "exec sid=%s exit=%s duration_ms=%d cmd_len=%d cmd=%r",
        sid,
        exit_code,
        duration_ms,
        len(body.command),
        cmd_preview,
    )
    return {
        "stdout": output,
        "stderr": "",
        "exit_code": exit_code,
        "timed_out": False,
        "duration_ms": duration_ms,
    }


#: How many lines the completion poll looks at. The completion marker is
#: printed last, so it is always within the tail — the poll does not need
#: the whole scrollback, and copying megabytes every 300 ms would be
#: pointless work.
_POLL_LINES = 64

#: Prepended to the output when the beginning of it did not survive the
#: scrollback. Loud, and first, so a caller that only reads the first
#: line of the result still learns that the result is a fragment.
_HEAD_LOST_NOTICE = (
    "[the beginning of this output no longer fits in the session "
    "scrollback — what follows is only its tail]\n"
)

#: Returned in ``stderr`` when the command never ran because the session
#: was busy with another one. The stdout stays empty on purpose: the pane
#: still holds the *previous* command's output, and handing that back as
#: this command's stdout is a lie the caller cannot detect. A model that
#: reads it quotes the other command's output as if it were its own.
#:
#: The notice names the way out. "The session is busy" on its own leaves
#: the caller (a model, most of the time) with nothing to do but guess,
#: and the guess it usually makes — retry the same session — lands on the
#: same refusal.
_BUSY_NOTICE = (
    "the command did not start: the session is busy running another command, "
    "so this one is queued and will run once the session is free. To run it "
    "now, use a different session_id."
)

#: Returned in ``stderr`` when the shell was waiting for the rest of an
#: unfinished command and has been interrupted. The command did not run
#: and cannot run: the shell had swallowed it.
_STUCK_NOTICE = (
    "the command did not start: the shell was waiting for the rest of an "
    "unfinished command (an unterminated quote, an unclosed brace or an "
    "unclosed here-document) and has been reset. The command was not run "
    "— send it again."
)


async def _history_size(name: str) -> int:
    """How many lines the session's scrollback holds right now.

    The value is the yardstick for "how much did *this* command print".
    tmux's ``capture-pane -S -N`` takes N lines of history plus the
    visible pane, so the difference between the depth before and after a
    command is exactly the number of lines that command pushed out of
    the visible area — and therefore the number of lines its output
    occupies, once the visible pane's own share is added back.
    """
    rc, out, _ = await _tmux("display-message", "-p", "-t", name, "#{history_size}")
    if rc != 0:
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


async def _pane_history_limit(name: str) -> int:
    """The scrollback depth the pane was created with. 0 when unknown."""
    rc, out, _ = await _tmux("display-message", "-p", "-t", name, "#{history_limit}")
    if rc != 0:
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


async def _pane_cwd(name: str) -> str | None:
    """The directory the pane is in right now. None when it cannot be read.

    The registry remembers the directory a session was *created* in; the
    shell moves (``cd``) and the registry does not follow. The live path
    is the only honest answer to "where will this command run" — and the
    only one that lets a caller be told the truth instead of being
    quietly given a different directory.
    """
    rc, out, _ = await _tmux("display-message", "-p", "-t", name, "#{pane_current_path}")
    if rc != 0:
        return None
    path = out.strip()
    if path.endswith(" (deleted)"):
        # tmux marks a pane whose directory is gone. The mark is about
        # the directory, not about the path: the path is still the path
        # the shell is in, and comparing it with a requested directory
        # has to work.
        path = path[: -len(" (deleted)")]
    if not path.startswith("/"):
        # tmux answers with a path or with nothing. Anything else is
        # not a directory, and treating it as one would make the
        # comparison below meaningless.
        return None
    return path or None


async def _capture_command_output(name: str, history_before: int) -> str:
    """Capture the pane, bounded to the lines this command produced.

    ``capture-pane -S -N`` returns N lines of scrollback *plus* the
    visible pane. N is the growth of the scrollback since the command
    started, so the capture covers the command's own output and nothing
    older — with the exception of the previous command's tail, which is
    still sitting in the visible pane; that part is cut away by the
    begin marker in :func:`_extract_command_output`.
    """
    history_after = await _history_size(name)
    depth = max(1, min(history_after - history_before, CAPTURE_DEPTH_LINES))
    _, out, _ = await _tmux("capture-pane", "-t", name, "-p", "-S", f"-{depth}")
    return out


async def _capture_full_pane(name: str) -> str:
    """Capture the whole scrollback plus the visible pane.

    ``-S -`` means "start at the beginning of the history". The capture
    is bounded by the pane's history limit, so it cannot grow without
    end — but it is bigger than the bounded capture, and it is read only
    on the slow path (a command that did not finish), never on the hot
    path.
    """
    _, out, _ = await _tmux("capture-pane", "-t", name, "-p", "-S", "-")
    return out


async def _pane_tail(name: str, lines: int = 20) -> list[str]:
    """The last non-empty lines of a pane.

    Bounded on purpose: the question this answers — "is the shell at a
    prompt?" — is about the end of the pane, and the whole scrollback is
    a needless copy.
    """
    rc, out, _ = await _tmux("capture-pane", "-t", name, "-p", "-S", f"-{lines}")
    if rc != 0:
        return []
    return [line for line in out.splitlines() if line.strip()]


async def _pane_is_waiting_for_input(name: str) -> bool:
    """Is the shell sitting there, waiting for the rest of a line?

    Two things have to hold.  tmux must report the *shell* as the
    foreground process — anything else means a command is running.  And
    the pane must end with the shell's **continuation prompt**: the
    marker the agent's protocol installs (``PS2``), or the shell's own
    default (``>``), which is what a shell shows when it was waiting
    before the protocol ever ran.

    The old rule was "the last line looks like a prompt", and it was
    wrong: ``while true; do echo 'tick$'; done`` runs in the shell
    itself (a builtin loop forks nothing, so tmux reports the shell),
    and its output ends with ``$`` — so a *working* command was
    interrupted and the caller was told the shell had been waiting.
    The marker cannot be printed by accident, and that is the whole
    point of it.
    """
    rc, current, _ = await _tmux("display-message", "-p", "-t", name, "#{pane_current_command}")
    if rc != 0 or not is_a_shell_name(current or ""):
        return False
    tail = await _pane_tail(name)
    if not tail:
        return False
    return is_a_continuation_prompt(tail[-1])


async def _pane_is_idle(name: str) -> bool:
    """Is the shell back at a prompt, with nothing running?

    The loose question, and the right one *after* an interrupt: has the
    shell come back to a prompt?  Here any prompt will do — the point
    is not whether the shell is waiting for input, but whether the
    interrupt freed it.
    """
    rc, current, _ = await _tmux("display-message", "-p", "-t", name, "#{pane_current_command}")
    if rc != 0 or not is_a_shell_name(current or ""):
        return False
    tail = await _pane_tail(name)
    if not tail:
        return False
    return looks_like_a_bare_prompt(tail[-1])


async def _reset_a_stuck_shell(name: str) -> bool:
    """Free a shell that waits for the rest of an unfinished command.

    The shell is interrupted, which makes it forget the line it is
    reading. That is only safe when nothing is running, and the caller
    checks that first — the pane must end with the shell's continuation
    prompt, and the shell must be the foreground process.

    Returns True when the shell is back at a prompt afterwards: the
    session is usable again. Returns False when the shell was busy
    after all, and nothing was touched.
    """
    if not await _pane_is_waiting_for_input(name):
        return False
    await _tmux("send-keys", "-t", name, "C-c")
    await asyncio.sleep(0.2)
    return await _pane_is_idle(name)


async def _scrollback_is_full(name: str) -> bool:
    """Is the scrollback at its limit, so that old lines are being dropped?

    This is the tiebreaker between two failures that look the same from
    the outside: a command that never started (the shell was busy) and a
    command whose output was so long that it pushed the begin marker out
    of the history. Only the second one can overflow the history, so
    "the history is full" tells the two apart.
    """
    limit = await _pane_history_limit(name)
    if limit <= 0:
        return False
    return await _history_size(name) >= limit


def _extract_command_output(
    capture: str, begin_marker: str, done_marker: str, ec_var: str, grouped: bool = False
) -> tuple[str, int | None, bool]:
    """Cut this command's output out of a pane capture.

    Three markers play a role here and none of them may be matched
    loosely.

    ``done_marker`` is printed as ``__AGENT_DONE_xxxxxxxx__=0`` on a line
    of its own. The *echo* of the command that prints it contains the
    same string inside a longer line, so the marker is matched in its
    printed form only — a loose match fires on the echo, which is
    visible in the pane long before the command has produced anything.

    ``ec_var`` names the variable that carries the exit code. The shell
    echoes that bookkeeping line, and everything after it is protocol
    bookkeeping rather than command output, so the region is cut there.

    ``begin_marker`` is printed before the command runs. Everything
    before it in the capture belongs to earlier commands or to the echo
    of the command line, so the capture is cut at the marker. If the
    marker is gone, the pane overflowed and only the tail of the output
    is left — which has to be reported, not passed off as a whole
    result.

    ``grouped`` says the command ran inside a brace group. Two things
    change then. The shell parses the whole group before it runs any of
    it, so the command's output arrives *after* the line that closes
    the group — and that line carries the bookkeeping. The bookkeeping
    line is therefore dropped on its own, with the output kept, and the
    shell's redraw of the line that opens the group is cut away too.
    What is left is exactly what the caller got before the group
    existed.

    Returns ``(output, exit_code, head_survived)``.
    """
    exit_code: int | None = None
    region = capture
    # The last printed marker wins: a command that prints a marker of
    # its own first must not be able to confuse the parser.
    done_hits = list(re.finditer(rf"(?m)^{re.escape(done_marker)}=(-?\d+)\r?$", capture))
    if done_hits:
        last = done_hits[-1]
        exit_code = int(last.group(1))
        region = capture[: last.start()]

    # Cut the protocol bookkeeping: the shell echoes the line that
    # captures the exit code, and the echo of the line that prints the
    # completion marker follows it — both are noise for the caller.
    #
    # The FIRST occurrence is the right cut point, and the ORDER of the
    # two cuts matters. The terminal echoes the raw typed text at the
    # top of the pane, without the prompt — that block sits above the
    # printed begin marker and is removed by the cut below. What is
    # left is the shell's own redraw of each line, and the first of
    # those is the bookkeeping line. Cutting the bookkeeping first
    # would cut the raw echo instead and leave both the bookkeeping
    # and the beginning of the output in the result.
    head_survived = True
    begin_hit = re.search(rf"(?m)^{re.escape(begin_marker)}\r?$", region)
    if begin_hit:
        region = region[begin_hit.end() :]
    else:
        head_survived = False

    ec_hits = list(re.finditer(rf"(?m)^.*?{re.escape(ec_var)}.*$", region))
    if ec_hits:
        if grouped:
            # The bookkeeping sits on the line that closes the group, and
            # the shell parses the whole group before running any of it —
            # so the command's output arrives *after* that line. Drop the
            # line itself and keep what follows it. Cutting the other way
            # (dropping everything from the line on) would take the
            # output with it.
            hit = ec_hits[0]
            end = region.find("\n", hit.end())
            region = region[: hit.start()] + (region[end + 1 :] if end != -1 else "")
        else:
            region = region[: ec_hits[0].start()]

    if grouped:
        region = _strip_group_open(region)

    region = _strip_continuation_prompts(region)

    return region.strip("\n"), exit_code, head_survived


def _strip_continuation_prompts(region: str) -> str:
    """Remove the lines where the shell printed its continuation prompt.

    The protocol installs its own ``PS2`` (a marker), and the shell
    prints it when it reads a line that is not a complete command.  The
    marker is there to make the waiting state *visible*; it is still
    the shell's output, and the caller never saw it before, so it is
    cut away like the rest of the protocol's bookkeeping.

    Two shapes reach the caller:

    * the **bare** marker — the shell is waiting for the rest of a line
      and has printed nothing else.  This is the case the marker was
      installed for, and the line is cut;
    * the marker **in front of the redrawn line** — every line of a
      multi-line command is a continuation, so every one of them comes
      back with the marker in front of it.  The line carries the text
      of the command the caller has just sent, and the caller knows it
      already; keeping the line would put a protocol token in front of
      the caller's own command in the model's context.

    So the marker is cut and the **redraw stays**: the stdout contract
    includes the shell's echo of the command line, and the echo is the
    caller's own text.  What the caller does not get is the protocol's
    token in front of it.
    """
    if CONTINUATION_PROMPT not in region:
        return region
    lines = region.split("\n")
    kept: list[str] = []
    for line in lines:
        if line.startswith(CONTINUATION_PROMPT):
            rest = line[len(CONTINUATION_PROMPT) :]
            if not rest.strip():
                # The bare marker: the shell is waiting, and the line
                # is nothing but protocol.
                continue
            line = rest
        kept.append(line)
    return "\n".join(kept)


def _strip_group_open(region: str) -> str:
    """Remove the shell's redraw of the line that opens the brace group.

    The group is opened by a line of its own, so the first line of the
    region is that line — the prompt followed by a ``{``. It is
    protocol, not output: without this cut the caller's stdout would
    grow a line it never had before.

    Only a line that ends in ``{`` is removed. If the pane scrolled and
    the line is gone, the first line is the redraw of the command
    itself, and it stays.
    """
    lines = region.split("\n")
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index < len(lines) and lines[index].rstrip().endswith("{"):
        del lines[index]
    return "\n".join(lines)


def _truncate_output(s: str) -> str:
    """Bound the size of returned stdout to avoid OOM in callers.

    Truncates at the *byte* level, not character level, so we never return
    more than :data:`MAX_OUTPUT_BYTES` of UTF-8 data — the proxy and
    Open WebUI would otherwise blow up on ``find /`` or similar.
    """
    encoded = s.encode("utf-8", "replace")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return s
    truncated = encoded[:MAX_OUTPUT_BYTES].decode("utf-8", "replace")
    return truncated + f"\n\n[output truncated at {MAX_OUTPUT_BYTES} bytes]"


@app.get("/sessions/{session_id}/capture", dependencies=[Depends(_require_token)])
async def capture_pane(
    session_id: str, lines: int = Query(default=100, ge=1, le=10000)
) -> dict[str, str]:
    """Return the last ``lines`` lines of the session's tmux scrollback.

    Dedicated endpoint — does not spawn a shell, so it's safe to call from
    a high-frequency WebSocket poll loop. The old ``/output`` alias below
    is kept for backwards compatibility.
    """
    sid = safe_id(session_id)
    name = tmux_name(sid)
    if not await tmux_session_exists(name):
        raise HTTPException(status_code=404, detail=f"session not found: {sid}")
    # Cap at CAPTURE_PANE_LINES regardless of what the caller asks for
    # so a single WebSocket poll can't OOM the container.
    # The second bound is the stdout budget expressed in lines: a line of
    # a 200-column pane is 200 bytes, so this many lines cannot exceed
    # the budget. Without it, /capture would be a door around the limit
    # that /exec honours — a client could pull three times the budget
    # through here and nothing would say so.
    effective = min(lines, CAPTURE_PANE_LINES, max(1, MAX_OUTPUT_BYTES // PANE_WIDTH))
    _, out, _ = await _tmux("capture-pane", "-t", name, "-p", "-S", f"-{effective}")
    return {"output": out}


@app.get("/sessions/{session_id}/output", dependencies=[Depends(_require_token)])
async def tail_output(
    session_id: str, lines: int = Query(default=100, ge=1, le=10000)
) -> dict[str, str]:
    """Alias of :func:`capture_pane` kept for backwards compatibility."""
    return await capture_pane(session_id, lines=lines)


# ---------------------------------------------------------------------------
# Binary transfers
# ---------------------------------------------------------------------------
# Text and bytes take different roads on purpose.
#
# stdout travels through a terminal pane: it is text, it is wrapped, it is
# bounded by the scrollback, and it is only as large as the budget allows.
# A binary cannot go that way — base64 through a pane costs 4/3 in size and
# loses the beginning of the payload when the pane overflows, silently.
#
# So bytes get their own door. The file is read from disk and streamed out
# as-is: no encoding, no pane, no scrollback, no size ceiling from the
# transport. The only limit is policy (AGENT_BINARY_LIMIT), and the client
# can pull a file of any size in ranges.
#
# The path is resolved inside the workspace, with symlinks resolved first,
# so a file that points outside is refused rather than served.

_BINARY_CHUNK = 64 * 1024


def _safe_file(candidate: str) -> str:
    """Resolve a client-supplied path to a regular file inside the workspace.

    The model knows the *logical* workspace path (``/workspace/out.bin``),
    while the real root may be somewhere else — the workspace is a bind
    mount, and the container's view of it is symlinked. So a logical
    absolute path is re-anchored on the real root, and a path that is
    already absolute is checked against the perimeter as it stands.

    Symlinks are resolved before the check, so a link pointing out of the
    workspace is refused rather than followed. Only regular files are
    served: a directory, a fifo or a device is not a file to hand out.

    The answer is the resolved path **as a string**, and the callers open
    exactly that string. Two things depend on it: the containment check
    below is what makes the path safe, so a caller that re-derived a path
    from it would lose the guarantee; and a string is what the check
    itself is written against, which is the form a reviewer (and a static
    analyser) can read.
    """
    raw = str(candidate or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="path is required")

    logical = os.path.abspath(WORKSPACE)
    if os.path.isabs(raw):
        if raw == logical:
            raise HTTPException(status_code=400, detail="path is a directory, not a file")
        if raw.startswith(logical + os.sep):
            # A logical path inside the workspace: re-anchor it on the
            # real root, which may live somewhere else entirely.
            raw = raw[len(logical) + 1 :]
    # A relative path, or an absolute one outside the logical workspace:
    # both are joined onto the real root. ``os.path.join`` keeps an
    # absolute second argument as it is, so "/etc/passwd" stays
    # "/etc/passwd" and is refused by the check below instead of being
    # quietly re-anchored inside the workspace.
    raw = os.path.join(WORKSPACE_REAL, raw)

    try:
        resolved = os.path.realpath(raw, strict=True)
    except OSError as exc:
        # A symlink loop is an OSError (ELOOP) like any other, and
        # ``strict=True`` raises it here — there is no separate
        # RuntimeError to catch.
        raise HTTPException(status_code=404, detail=f"file not found: {candidate}") from exc

    if resolved == WORKSPACE_REAL:
        raise HTTPException(status_code=400, detail="path is a directory, not a file")
    if resolved.startswith(WORKSPACE_PREFIX):
        if not os.path.isfile(resolved):
            raise HTTPException(status_code=400, detail=f"not a regular file: {candidate}")
        return resolved
    raise HTTPException(status_code=400, detail=f"path outside the workspace: {candidate}")


def _content_disposition(name: str) -> str:
    """Build a Content-Disposition that survives a non-ASCII file name."""
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in name)
    quoted = urllib.parse.quote(name, safe="")
    return f"attachment; filename=\"{ascii_name or 'file'}\"; filename*=UTF-8''{quoted}"


def _parse_byte_range(header: str, size: int) -> tuple[int, int]:
    """Parse a single ``bytes=start-end`` range. Returns ``(start, length)``."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or (not match.group(1) and not match.group(2)):
        raise HTTPException(status_code=416, detail=f"unsupported range: {header}")
    raw_start, raw_end = match.group(1), match.group(2)
    if raw_start:
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1
    else:
        # A suffix range: the last N bytes.
        length = int(raw_end)
        if length == 0:
            raise HTTPException(status_code=416, detail=f"unsatisfiable range: {header}")
        start = max(0, size - length)
        end = size - 1
    if start >= size or start > end:
        raise HTTPException(status_code=416, detail=f"unsatisfiable range: {header}")
    return start, min(end, size - 1) - start + 1


def _open_inside_workspace(resolved: str) -> int:
    """Open a vetted path without following a symlink in any of its parts.

    :func:`_safe_file` vets a path by resolving it: the answer is a
    string inside the workspace, and the check happens at one moment in
    time. Between that moment and the open, a process inside the
    sandbox can replace a **directory** of the path with a symlink that
    points out. ``O_NOFOLLOW`` does not help there — it guards the last
    component only, and the kernel follows the substituted directory on
    the way to it. The file that comes out is then a file outside the
    workspace, served under a promise that it is inside.

    So the file is opened **relative to a directory descriptor** of the
    workspace, one component at a time, and every component is opened
    with ``O_NOFOLLOW``: a component that is a symlink fails the open
    instead of being followed. The file that comes out is the file the
    check vetted — by construction, not by timing.

    The workspace is writable by the agent, so the race is real: a model
    can start a background process that swaps a directory while a read
    is in flight.

    The constants are named directly, not looked up with a default: a
    guard that silently disappears when a lookup comes up empty is not a
    guard. Every platform this container runs on has them.
    """
    if not resolved.startswith(WORKSPACE_PREFIX):
        raise HTTPException(status_code=400, detail=f"path outside the workspace: {resolved}")
    relative = resolved[len(WORKSPACE_PREFIX) :]
    parts = [part for part in relative.split(os.sep) if part]
    if not parts:
        raise HTTPException(status_code=400, detail=f"path is a directory, not a file: {resolved}")

    # The root is opened first and pinned: from here on the walk cannot
    # be moved out from under the file, whatever happens to the names
    # along the way.
    dir_fd = os.open(WORKSPACE_REAL, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = next_fd
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def _path_size(name: str) -> int | None:
    """The size the *path* reports, or ``None`` when it cannot be read.

    Used for the policy check only.  The size the answer promises
    always comes from the descriptor the bytes are read from — that is
    what makes the answer and the body agree.
    """
    try:
        return os.stat(name).st_size
    except OSError:
        return None


def _open_for_download(name: str):
    """Open the file for reading, turning a refusal into a status code.

    The download endpoint must know whether it can read the file *before*
    it starts the response. Once the 200 and the ``Content-Length`` are on
    the wire, a failure to open can only truncate the body: the client
    asked for N bytes, got 0, and sees a broken connection rather than a
    permission problem. That is not a cosmetic difference — a client that
    reads "the side-car is unreachable" blames the wrong component, and
    an operator spends the afternoon looking at the network instead of at
    the file's mode.

    So the open happens here, before any header is sent, and a refusal
    becomes a status code with a message that says what to do about it.

    The name is the string that :func:`_safe_file` resolved and checked;
    it is opened through :func:`_open_inside_workspace`, so that the file
    that is served is the file that was vetted — even if a directory of
    the path was swapped for a symlink in between.
    """
    try:
        fd = _open_inside_workspace(name)
        return os.fdopen(fd, "rb")
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail=(
                f"permission denied: the sandbox agent cannot read {os.path.basename(name)}. "
                "The file's owner or mode does not allow it (a root-owned 0600 "
                "file, for example); make it readable, e.g. chmod 644."
            ),
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"file not found: {name}") from exc
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            # ELOOP: a component is a symlink and O_NOFOLLOW refused it.
            # ENOTDIR: a component that was a directory at the check is
            # not one any more (a symlink to a directory looks exactly
            # like that to the kernel when O_DIRECTORY is asked for).
            # Both mean the same thing to the caller: the path was
            # replaced between the check and the open.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"the file was replaced while it was being opened: {name}. "
                    "It — or one of its directories — is a symlink now; the "
                    "sandbox refuses to follow one."
                ),
            ) from exc
        raise HTTPException(status_code=400, detail=f"cannot read {name}: {exc}") from exc


def _iter_file(handle, start: int, length: int):
    """Yield the file's bytes in chunks. The file is never fully in memory."""
    try:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(_BINARY_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        # The handle was opened before the response started, so it has to be
        # closed here — including when the client disconnects mid-stream.
        handle.close()


@app.get("/files/meta", dependencies=[Depends(_require_token)])
async def file_meta(path: str = Query(..., description="Path of the file in the sandbox")) -> dict:
    """Metadata for a file in the workspace — size, name, mtime.

    A client that wants to move a file needs its size *before* it starts
    moving it: to check the policy limit without downloading a gigabyte
    first. This is the cheap door; :func:`download_file` is the expensive
    one.
    """
    target = _safe_file(path)
    stat = os.stat(target)
    guessed, _ = mimetypes.guess_type(target)
    return {
        "name": os.path.basename(target),
        "path": target,
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
        "mime": guessed or "application/octet-stream",
        "limit": BINARY_LIMIT_BYTES,
    }


@app.get("/files", dependencies=[Depends(_require_token)])
async def download_file(
    request: Request,
    path: str = Query(..., description="Path of the file in the sandbox"),
) -> Response:
    """Stream a file out of the sandbox, byte for byte.

    No base64, no pane, no scrollback: the bytes are read from disk and
    written to the socket as they are read, so the agent's memory use does
    not depend on the file's size. A ``Range`` header is honoured, so a
    large file can be pulled in pieces and resumed.

    The size limit is policy, not transport: a file over
    ``AGENT_BINARY_LIMIT`` is refused with its real size in the message,
    and the limit is raised by configuration rather than by the client.

    A file the agent's own process cannot read is refused with a 403
    *before* the response starts, so the client gets a status it can act
    on instead of a promise of N bytes followed by silence.
    """
    target = _safe_file(path)

    # The size has to come from the *descriptor* the bytes come from.
    #
    # Taking it from the path means the answer can describe a file that
    # is not the one being served: the path is measured once and opened
    # once, and anything that changes the file in between — a writer, a
    # rename, a swap — leaves the answer promising N bytes and the body
    # carrying M.  The MCP door takes the size from the descriptor
    # already; this is the same rule on the HTTP door.
    #
    # The open happens first, then the measurement, and both are before
    # any header goes out: a file the agent cannot read is still a 403
    # the client can act on.
    # The limit is a *policy*, and a policy is checked before the file
    # is opened: a file over the limit is refused with 413 whether or
    # not the agent could read it.  The size here is the one the path
    # reports — a policy does not have to be exact to the byte, and
    # checking it first is what keeps "no permission" and "too big"
    # from being confused for one another.
    policy_size = _path_size(target)
    if BINARY_LIMIT_BYTES and policy_size is not None and policy_size > BINARY_LIMIT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"file is {policy_size} bytes, over the binary limit of "
                f"{BINARY_LIMIT_BYTES} bytes"
            ),
        )

    handle = _open_for_download(target)
    try:
        size = os.fstat(handle.fileno()).st_size
    except OSError:
        handle.close()
        raise

    if BINARY_LIMIT_BYTES and size > BINARY_LIMIT_BYTES:
        # The file grew past the limit between the policy check and the
        # open. The limit still applies, and the answer names the size
        # the bytes would have had.
        handle.close()
        raise HTTPException(
            status_code=413,
            detail=(f"file is {size} bytes, over the binary limit of {BINARY_LIMIT_BYTES} bytes"),
        )

    try:
        start = 0
        length = size
        status_code = 200
        range_header = request.headers.get("range")
        if range_header:
            start, length = _parse_byte_range(range_header, size)
            status_code = 206
    except Exception:
        # An unsatisfiable range is a 416, and the descriptor opened
        # above is not handed to the response — so it has to be closed
        # here.  CPython would close it when the frame unwound, but
        # "the file is closed on every path" is a property, not a
        # detail of the garbage collector.
        handle.close()
        raise

    file_name = os.path.basename(target)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": "application/octet-stream",
        "Content-Disposition": _content_disposition(file_name),
        # HTTP header values are latin-1, and a file name may be anything.
        # Content-Disposition already carries the name in both a sanitised
        # ASCII form and RFC 5987 form; this one is percent-encoded so a
        # client can read it back without parsing the disposition.
        "X-File-Name": urllib.parse.quote(file_name, safe=""),
        "X-File-Size": str(size),
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{start + length - 1}/{size}"

    return StreamingResponse(
        _iter_file(handle, start, length),
        status_code=status_code,
        headers=headers,
        media_type="application/octet-stream",
    )


@app.delete("/files", dependencies=[Depends(_require_token)])
async def delete_file(
    path: str = Query(..., description="Path of the file in the sandbox"),
) -> Response:
    """Remove a file from the sandbox workspace.

    The sandbox is a **scratchpad**, not storage. Once a client has pulled
    a file out and delivered it, the artifact exists twice — in the
    workspace and wherever it was delivered — until somebody removes one
    of the copies. This endpoint is how the sandbox copy is removed, so
    the delivery can be the last one standing.

    The path goes through the same resolution as the download endpoint:
    a path outside the workspace, a symlink pointing out of it, or
    anything that is not a regular file is refused. A file that is not
    there is a 404 — "nothing to delete" is not the same as "deleted".

    Deleting through a symlink removes the file the link points at (the
    link itself stays, dangling). The target is always inside the
    workspace, because that is what the resolution checks.
    """
    target = _safe_file(path)
    try:
        os.remove(target)
    except FileNotFoundError as exc:
        # The file vanished between the resolution and the unlink. The
        # caller's intent — "make sure this file is gone" — is satisfied.
        raise HTTPException(status_code=404, detail=f"file not found: {path}") from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"could not delete {path}: {exc}") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "agent_api:app",
        host="0.0.0.0",  # noqa: S104 - dev server binds all interfaces (same as FastAPI docs)
        port=int(os.environ.get("AGENT_PORT", "8082")),
        log_level=os.environ.get("AGENT_LOG_LEVEL", "info").lower(),
        # Graceful shutdown — give in-flight requests 25s to finish.
        timeout_graceful_shutdown=25,
    )


# ---------------------------------------------------------------------------
# The MCP door
# ---------------------------------------------------------------------------
# The same sandbox, in the protocol the caller speaks. The tools are the
# same tools the HTTP API exposes — this is a face, not a second
# implementation — and the gate is the same token.
#
# The module is imported here, and not at the top of the file, because
# it reaches back into this module for the tools' behaviour. The import
# inside the tools is local, so the two modules never import each other
# at the same time.
from mcp_server import build_app as _build_mcp_app  # noqa: E402
from mcp_server import lifespan as _mcp_lifespan  # noqa: E402
from mcp_server import set_version as _set_mcp_version  # noqa: E402

_set_mcp_version(__version__)

#: The MCP endpoint. It is a route and not a mount on purpose: a mount
#: at "/mcp" answers "/mcp" with a redirect to "/mcp/", and a redirect
#: on every call is a round trip the caller pays for nothing — some
#: clients do not follow it for POST at all.
_mcp_asgi = _build_mcp_app(_token_ok)
app.router.routes.append(
    StarletteRoute("/mcp", endpoint=_mcp_asgi, methods=["GET", "POST", "DELETE"])
)
