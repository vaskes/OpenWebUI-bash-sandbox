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
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from agent_core import (
    SESSION_ID_MAX_LEN,
    TMUX_PREFIX,
    safe_id,
    tmux_name,
)

__version__ = "1.1.0"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT = 30          # seconds
MAX_TIMEOUT = 600             # 10 min hard cap
MAX_COMMAND_LEN = 1_000_000   # 1 MB hard cap on a single command
WORKSPACE = os.environ.get("AGENT_WORKSPACE", "/workspace")
SESSIONS_FILE = os.environ.get("AGENT_SESSIONS_FILE", "/workspace/.agent-sessions.json")

logger = logging.getLogger("sandbox-agent")
logging.basicConfig(
    level=os.environ.get("AGENT_LOG_LEVEL", "INFO"),
    format='{"ts":"%(asctime)s","logger":"%(name)s","level":"%(levelname)s","msg":"%(message)s"}',
)


# ---------------------------------------------------------------------------
# Persistent session registry
# ---------------------------------------------------------------------------
def _load_sessions() -> dict[str, dict[str, Any]]:
    """Load the session registry from disk; return empty dict on missing/corrupt."""
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("sessions file corrupt (%s), starting fresh", exc)
    return {}


def _save_sessions() -> None:
    """Atomically persist the session registry to disk. Best-effort — a
    failure to write must not break the request path."""
    try:
        tmp = f"{SESSIONS_FILE}.tmp.{uuid.uuid4().hex[:8]}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(SESSIONS, f)
        os.replace(tmp, SESSIONS_FILE)
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
    return proc.returncode or 0, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


async def tmux_session_exists(name: str) -> bool:
    """Return True if a tmux session with the given name currently exists."""
    rc, _, _ = await _tmux("has-session", "-t", name)
    return rc == 0


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """Lifespan handler — reaps orphan tmux sessions on shutdown."""
    yield
    for sid, info in list(SESSIONS.items()):
        await _tmux("kill-session", "-t", info["tmux_name"])


app = FastAPI(
    title="Sandbox Agent",
    version=__version__,
    description=__doc__,
    lifespan=lifespan,
)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Liveness probe. Reports current session count and workspace root."""
    return {"status": "ok", "sessions": len(SESSIONS), "workspace": WORKSPACE_REAL}


@app.get("/sessions")
async def list_sessions() -> dict[str, Any]:
    """List active sessions, reaping any whose tmux backend has died."""
    dead: list[str] = []
    for sid, info in SESSIONS.items():
        if not await tmux_session_exists(info["tmux_name"]):
            dead.append(sid)
    for sid in dead:
        SESSIONS.pop(sid, None)
    if dead:
        _save_sessions()
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
    cwd: Optional[str] = Field(default=None, description="Initial working directory. Must be inside the workspace.")


@app.post("/sessions")
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
    _save_sessions()
    logger.info("session.create sid=%s cwd=%s", sid, cwd)
    return {"session_id": sid, "tmux_name": name, "existed": False, "cwd": cwd}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, str]:
    """Kill a tmux session and remove it from the registry. Idempotent."""
    sid = safe_id(session_id)
    name = tmux_name(sid)
    await _tmux("kill-session", "-t", name)
    SESSIONS.pop(sid, None)
    _save_sessions()
    logger.info("session.delete sid=%s", sid)
    return {"deleted": sid}


class ExecRequest(BaseModel):
    """Request body for ``POST /sessions/{id}/exec``."""

    command: str = Field(..., min_length=1, max_length=MAX_COMMAND_LEN, description="Shell command to execute.")
    timeout: int = Field(default=DEFAULT_TIMEOUT, ge=1, le=MAX_TIMEOUT, description="Max seconds to wait for completion.")


@app.post("/sessions/{session_id}/exec")
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

    marker = f"__AGENT_DONE_{uuid.uuid4().hex[:8]}__"
    full_cmd = (
        f"{body.command}\n"
        f"__ec=$?\n"
        f"printf '\\n{marker}=%d\\n' \"$__ec\""
    )
    await _tmux("send-keys", "-t", name, full_cmd, "Enter")

    start = time.time()
    output = ""
    while time.time() - start < body.timeout:
        rc, out, _ = await _tmux("capture-pane", "-t", name, "-p")
        if rc == 0 and marker in out:
            output = out
            break
        await asyncio.sleep(0.3)
    else:
        # Timed out — return whatever we have so the model can decide.
        _, output, _ = await _tmux("capture-pane", "-t", name, "-p")
        duration_ms = int((time.time() - start) * 1000)
        logger.warning(
            "exec.timeout sid=%s duration_ms=%d cmd_len=%d",
            sid, duration_ms, len(body.command),
        )
        return {
            "stdout": output,
            "stderr": "",
            "exit_code": None,
            "timed_out": True,
            "duration_ms": duration_ms,
        }

    exit_code: Optional[int] = None
    match = re.search(rf"{marker}=(-?\d+)", output)
    if match:
        exit_code = int(match.group(1))
        output = re.sub(rf"\n?{marker}=-?\d+\n?", "", output).rstrip()
    duration_ms = int((time.time() - start) * 1000)
    logger.info(
        "exec sid=%s exit=%s duration_ms=%d cmd_len=%d",
        sid, exit_code, duration_ms, len(body.command),
    )
    return {
        "stdout": output,
        "stderr": "",
        "exit_code": exit_code,
        "timed_out": False,
        "duration_ms": duration_ms,
    }


@app.get("/sessions/{session_id}/capture")
async def capture_pane(session_id: str, lines: int = Query(default=100, ge=1, le=10000)) -> dict[str, str]:
    """Return the last ``lines`` lines of the session's tmux scrollback.

    Dedicated endpoint — does not spawn a shell, so it's safe to call from
    a high-frequency WebSocket poll loop. The old ``/output`` alias below
    is kept for backwards compatibility.
    """
    sid = safe_id(session_id)
    name = tmux_name(sid)
    if not await tmux_session_exists(name):
        raise HTTPException(status_code=404, detail=f"session not found: {sid}")
    _, out, _ = await _tmux("capture-pane", "-t", name, "-p", "-S", f"-{lines}")
    return {"output": out}


@app.get("/sessions/{session_id}/output")
async def tail_output(session_id: str, lines: int = Query(default=100, ge=1, le=10000)) -> dict[str, str]:
    """Alias of :func:`capture_pane` kept for backwards compatibility."""
    return await capture_pane(session_id, lines=lines)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "agent_api:app",
        host="0.0.0.0",
        port=int(os.environ.get("AGENT_PORT", "8082")),
        log_level=os.environ.get("AGENT_LOG_LEVEL", "info").lower(),
        # Graceful shutdown — give in-flight requests 25s to finish.
        timeout_graceful_shutdown=25,
    )
