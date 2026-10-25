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
    single ``Range`` header.
DELETE /files?path=P
    Remove a file from the workspace. The sandbox is a scratchpad, not
    storage: once a file has been delivered, the client can remove it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import shutil
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

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
    SESSION_ID_MAX_LEN,
    __version__,
    parse_capability_status,
    safe_id,
    sudo_posture,
    tmux_name,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT = 30  # seconds
MAX_TIMEOUT = 600  # 10 min hard cap
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
logging.basicConfig(
    level=os.environ.get("AGENT_LOG_LEVEL", "INFO"),
    format='{"ts":"%(asctime)s","logger":"%(name)s","level":"%(levelname)s","msg":"%(message)s"}',
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


def _safe_cwd(candidate: str) -> str:
    """Resolve symlinks and confirm the path is rooted at the workspace.

    Raises :class:`HTTPException` (400) for any path that is not a directory
    inside :data:`WORKSPACE_REAL`. This blocks:

    * absolute paths outside the workspace (e.g. ``/etc``),
    * ``..`` traversal (e.g. ``/workspace/../etc``),
    * symlinks pointing outside the workspace.
    """
    real = os.path.realpath(candidate)
    if not os.path.isdir(real):
        raise HTTPException(status_code=400, detail=f"cwd is not a directory: {candidate}")
    # ``commonpath`` raises if the drives differ on Windows; both paths are
    # absolute here so this is safe.
    try:
        common = os.path.commonpath([real, WORKSPACE_REAL])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid cwd: {exc}") from exc
    if common != WORKSPACE_REAL:
        raise HTTPException(status_code=400, detail=f"cwd outside workspace: {candidate}")
    return real


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
@asynccontextmanager
def _warn_about_the_privilege_mode() -> None:
    """Say something when sudo is installed — whether or not it works.

    The privilege mode (docs/SECURITY.md) has two halves: the image
    ships sudo, and the container's posture lets the setuid bit do its
    job. Turn on only one of them and the model gets an error it cannot
    interpret — *"sudo: the 'no new privileges' flag is set"* reads like
    a broken sandbox, not like a misconfiguration.

    A warning at startup is the difference between "the deployment is
    misconfigured" and "the sandbox is flaky".
    """
    if shutil.which("sudo") is None:
        return
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
    if reaped:
        logger.info("startup reaper dropped %d stale session(s): %s", reaped, ",".join(reaped))
    _warn_about_the_privilege_mode()
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
MAX_BODY_BYTES = 2 * 1024 * 1024


@app.middleware("http")
async def _body_size_limit(request, call_next):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                return Response(
                    status_code=413,
                    content=b'{"detail":"request body too large"}',
                    media_type="application/json",
                )
        except ValueError:
            pass
    return await call_next(request)


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
    import hmac as _hmac

    if not _hmac.compare_digest(creds.credentials, AGENT_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return creds.credentials


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
    if sid in SESSIONS and await tmux_session_exists(name):
        return {"session_id": sid, "tmux_name": name, "existed": True}
    cwd = _safe_cwd(body.cwd or WORKSPACE_REAL)
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
    sid = safe_id(session_id)
    name = tmux_name(sid)
    if not await tmux_session_exists(name):
        # Auto-create — this is what makes the API forgiving for clients that
        # don't explicitly create a session first.
        await create_session(CreateSession(session_id=sid, cwd=WORKSPACE_REAL))

    # C1 (sandbox-recommendations): log a preview of the command itself
    # in addition to its length, so post-incident audit can reconstruct
    # what the LLM actually ran. ``cmd_preview`` is truncated to 200 chars
    # so a runaway 1 MB command does not flood the log; the
    # ``cmd_len=N`` field preserves the original length for inspection.
    cmd_preview = (
        body.command
        if len(body.command) <= 200
        else body.command[:200] + f"... (+{len(body.command) - 200} bytes)"
    )

    marker = f"__AGENT_DONE_{uuid.uuid4().hex[:8]}__"
    # A second marker, printed BEFORE the command runs. It is the proof
    # that the capture still contains the beginning of the output. When the
    # pane overflows, tmux drops the OLDEST lines — the beginning — and
    # hands back a fragment that looks like a whole result. Without this
    # marker there is no way to tell that apart from a short command, so
    # the caller would read the tail of a file and believe it read the
    # file. With it, the loss is detected and reported.
    begin_marker = f"__AGENT_BEGIN_{uuid.uuid4().hex[:8]}__"
    # The variable that carries the exit code from the command to the
    # completion marker. It is named after a random token so that the
    # shell's echo of the bookkeeping line can be recognised and cut away
    # from the output — otherwise the caller receives the internals of
    # the protocol along with the command's own output.
    ec_var = f"__AGENT_EC_{uuid.uuid4().hex[:8]}__"
    full_cmd = (
        f"printf '\\n{begin_marker}\\n'\n"
        f"{body.command}\n"
        f"{ec_var}=$?\n"
        f"printf '\\n{marker}=%d\\n' \"${ec_var}\""
    )
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
        output, _, head_survived = _extract_command_output(capture, begin_marker, marker, ec_var)
        if not head_survived and output.strip():
            output = _HEAD_LOST_NOTICE + output
        output = _truncate_output(output)
        duration_ms = int((time.time() - start) * 1000)
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
        capture, begin_marker, marker, ec_var
    )
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


def _extract_command_output(
    capture: str, begin_marker: str, done_marker: str, ec_var: str
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
        region = region[: ec_hits[0].start()]

    return region.strip("\n"), exit_code, head_survived


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


def _within_workspace(real: str) -> bool:
    """True when ``real`` is the workspace root or lives under it."""
    try:
        common = os.path.commonpath([real, WORKSPACE_REAL])
    except ValueError:
        return False
    return common == WORKSPACE_REAL


def _safe_file(candidate: str) -> Path:
    """Resolve a client-supplied path to a regular file inside the workspace.

    The model knows the *logical* workspace path (``/workspace/out.bin``),
    while the real root may be somewhere else — the workspace is a bind
    mount, and the container's view of it is symlinked. So a logical
    absolute path is re-anchored on the real root, and a path that is
    already absolute is checked against the perimeter as it stands.

    Symlinks are resolved before the check, so a link pointing out of the
    workspace is refused rather than followed. Only regular files are
    served: a directory, a fifo or a device is not a file to hand out.
    """
    raw = str(candidate or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="path is required")

    logical = os.path.abspath(WORKSPACE)
    if os.path.isabs(raw):
        if raw == logical:
            raise HTTPException(status_code=400, detail="path is a directory, not a file")
        prefix = logical + os.sep
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
    else:
        raw = str(Path(WORKSPACE_REAL) / raw)

    try:
        resolved = Path(raw).resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=404, detail=f"file not found: {candidate}") from exc
    except RuntimeError as exc:  # symlink loop
        raise HTTPException(status_code=400, detail=f"unresolvable path: {candidate}") from exc

    if not _within_workspace(str(resolved)):
        raise HTTPException(status_code=400, detail=f"path outside the workspace: {candidate}")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail=f"not a regular file: {candidate}")
    return resolved


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


def _iter_file(path: Path, start: int, length: int):
    """Yield the file's bytes in chunks. The file is never fully in memory."""
    remaining = length
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(_BINARY_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@app.get("/files/meta", dependencies=[Depends(_require_token)])
async def file_meta(path: str = Query(..., description="Path of the file in the sandbox")) -> dict:
    """Metadata for a file in the workspace — size, name, mtime.

    A client that wants to move a file needs its size *before* it starts
    moving it: to check the policy limit without downloading a gigabyte
    first. This is the cheap door; :func:`download_file` is the expensive
    one.
    """
    target = _safe_file(path)
    stat = target.stat()
    guessed, _ = mimetypes.guess_type(target.name)
    return {
        "name": target.name,
        "path": str(target),
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
    """
    target = _safe_file(path)
    size = target.stat().st_size

    if BINARY_LIMIT_BYTES and size > BINARY_LIMIT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(f"file is {size} bytes, over the binary limit of {BINARY_LIMIT_BYTES} bytes"),
        )

    start = 0
    length = size
    status_code = 200
    range_header = request.headers.get("range")
    if range_header:
        start, length = _parse_byte_range(range_header, size)
        status_code = 206

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": "application/octet-stream",
        "Content-Disposition": _content_disposition(target.name),
        # HTTP header values are latin-1, and a file name may be anything.
        # Content-Disposition already carries the name in both a sanitised
        # ASCII form and RFC 5987 form; this one is percent-encoded so a
        # client can read it back without parsing the disposition.
        "X-File-Name": urllib.parse.quote(target.name, safe=""),
        "X-File-Size": str(size),
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{start + length - 1}/{size}"

    return StreamingResponse(
        _iter_file(target, start, length),
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
        target.unlink()
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
