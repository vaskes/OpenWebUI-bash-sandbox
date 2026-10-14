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
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from agent_core import (
    SESSION_ID_MAX_LEN,
    __version__,
    safe_id,
    tmux_name,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT = 30  # seconds
MAX_TIMEOUT = 600  # 10 min hard cap
MAX_COMMAND_LEN = 1_000_000  # 1 MB hard cap on a single command
MAX_OUTPUT_BYTES = 1_000_000  # 1 MB hard cap on captured command output
CAPTURE_PANE_LINES = 5000  # ``tmux capture-pane -S -N`` upper bound
WORKSPACE = os.environ.get("AGENT_WORKSPACE", "/workspace")
SESSIONS_FILE = os.environ.get("AGENT_SESSIONS_FILE", "/workspace/.agent-sessions.json")
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
    rc, _, err = await _tmux("new-session", "-d", "-s", name, "-c", cwd, "-x", "200", "-y", "50")
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"tmux new-session failed: {err}")
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
    full_cmd = f"{body.command}\n__ec=$?\nprintf '\\n{marker}=%d\\n' \"$__ec\""
    # ``send-keys -l`` writes the command as a literal string. Without
    # ``-l`` tmux interprets special tokens (``C-c``, ``Enter``, ``Tab``,
    # arrow keys, etc.) which can silently corrupt or terminate the
    # command — e.g. ``git commit -m "Press Enter to continue"`` would
    # fire the Enter mid-message. Two-step write avoids the issue
    # regardless of the command content.
    await _tmux("send-keys", "-t", name, "-l", full_cmd)
    await _tmux("send-keys", "-t", name, "Enter")

    start = time.time()
    output = ""
    while time.time() - start < body.timeout:
        rc, out, _ = await _tmux("capture-pane", "-t", name, "-p", "-S", f"-{CAPTURE_PANE_LINES}")
        if rc == 0 and marker in out:
            output = out
            break
        await asyncio.sleep(0.3)
    else:
        # Timed out — return whatever we have so the model can decide.
        _, output, _ = await _tmux("capture-pane", "-t", name, "-p", "-S", f"-{CAPTURE_PANE_LINES}")
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

    exit_code: int | None = None
    # ``re.findall`` returns every match in order; we want the **last**
    # one because the real marker is appended after the command output
    # in the wrapped ``full_cmd`` above. ``re.search`` would pick the
    # first — and a malicious command could print a fake marker first
    # to confuse the parser.
    matches = re.findall(rf"{marker}=(-?\d+)", output)
    if matches:
        exit_code = int(matches[-1])
        # Strip the last marker occurrence (the legitimate one) and
        # everything after. The earlier fake ones, if any, stay in
        # stdout so the model can see they were fakes.
        last_marker_pattern = rf"\n?{marker}={re.escape(matches[-1])}\n?"
        output = re.sub(last_marker_pattern, "", output).rstrip()
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
    effective = min(lines, CAPTURE_PANE_LINES)
    _, out, _ = await _tmux("capture-pane", "-t", name, "-p", "-S", f"-{effective}")
    return {"output": out}


@app.get("/sessions/{session_id}/output", dependencies=[Depends(_require_token)])
async def tail_output(
    session_id: str, lines: int = Query(default=100, ge=1, le=10000)
) -> dict[str, str]:
    """Alias of :func:`capture_pane` kept for backwards compatibility."""
    return await capture_pane(session_id, lines=lines)


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
