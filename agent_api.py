#!/usr/bin/env python3
"""Sandbox Agent — persistent tmux sessions per chat, exposed via HTTP API.

Each HTTP session is backed by a tmux session with a stable name
``agent_<sanitized_session_id>``. The shell state (cwd, env vars, history,
running processes) persists across calls within the same session_id, so an
LLM can incrementally build up work in a sandboxed environment.

Public API
----------
GET  /healthz
    Liveness probe.
GET  /sessions
    List active sessions, with stale sessions reaped.
POST /sessions
    Create or attach to a session.
DELETE /sessions/{session_id}
    Kill a session and remove it from the registry.
POST /sessions/{session_id}/exec
    Run a shell command in the session, return stdout and exit code.
GET  /sessions/{session_id}/output?lines=N
    Tail the last N lines of the session's terminal buffer.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
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

app = FastAPI(
    title="Sandbox Agent",
    version=__version__,
    description=__doc__,
)

# session_id -> {"tmux_name": str, "created": float, "cwd": str}
SESSIONS: dict[str, dict[str, Any]] = {}

DEFAULT_TIMEOUT = 30          # seconds
MAX_TIMEOUT = 600             # 10 min hard cap
WORKSPACE = "/workspace"


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


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Liveness probe. Reports current session count and workspace root."""
    return {"status": "ok", "sessions": len(SESSIONS), "workspace": WORKSPACE}


@app.get("/sessions")
async def list_sessions() -> dict[str, Any]:
    """List active sessions, reaping any whose tmux backend has died."""
    dead: list[str] = []
    for sid, info in SESSIONS.items():
        if not await tmux_session_exists(info["tmux_name"]):
            dead.append(sid)
    for sid in dead:
        SESSIONS.pop(sid, None)
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
    cwd: Optional[str] = Field(default=None, description="Initial working directory. Defaults to /workspace.")


@app.post("/sessions")
async def create_session(body: CreateSession) -> dict[str, Any]:
    """Create a new tmux-backed session or attach to an existing one."""
    sid = safe_id(body.session_id)
    name = tmux_name(sid)
    if sid in SESSIONS and await tmux_session_exists(name):
        return {"session_id": sid, "tmux_name": name, "existed": True}
    cwd = body.cwd or WORKSPACE
    if not os.path.isdir(cwd):
        raise HTTPException(status_code=400, detail=f"cwd does not exist: {cwd}")
    rc, _, err = await _tmux("new-session", "-d", "-s", name, "-c", cwd, "-x", "200", "-y", "50")
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"tmux new-session failed: {err}")
    SESSIONS[sid] = {"tmux_name": name, "created": time.time(), "cwd": cwd}
    return {"session_id": sid, "tmux_name": name, "existed": False, "cwd": cwd}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, str]:
    """Kill a tmux session and remove it from the registry. Idempotent."""
    sid = safe_id(session_id)
    name = tmux_name(sid)
    await _tmux("kill-session", "-t", name)
    SESSIONS.pop(sid, None)
    return {"deleted": sid}


class ExecRequest(BaseModel):
    """Request body for ``POST /sessions/{id}/exec``."""

    command: str = Field(..., description="Shell command to execute.")
    timeout: int = Field(default=DEFAULT_TIMEOUT, ge=1, le=MAX_TIMEOUT, description="Max seconds to wait for completion.")


@app.post("/sessions/{session_id}/exec")
async def exec_cmd(session_id: str, body: ExecRequest) -> dict[str, Any]:
    """Run a shell command in the session, return captured stdout and exit code.

    A unique marker is appended to the command so we know when the shell has
    finished producing output and can read the exit code from ``$?``. The
    marker line is stripped from the returned stdout.
    """
    sid = safe_id(session_id)
    name = tmux_name(sid)
    if not await tmux_session_exists(name):
        # Auto-create — this is what makes the API forgiving for clients that
        # don't explicitly create a session first.
        await create_session(CreateSession(session_id=sid, cwd=WORKSPACE))

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
        return {
            "stdout": output,
            "stderr": "",
            "exit_code": None,
            "timed_out": True,
            "duration_ms": int((time.time() - start) * 1000),
        }

    exit_code: Optional[int] = None
    match = re.search(rf"{marker}=(-?\d+)", output)
    if match:
        exit_code = int(match.group(1))
        output = re.sub(rf"\n?{marker}=-?\d+\n?", "", output).rstrip()
    return {
        "stdout": output,
        "stderr": "",
        "exit_code": exit_code,
        "timed_out": False,
        "duration_ms": int((time.time() - start) * 1000),
    }


@app.get("/sessions/{session_id}/output")
async def tail_output(session_id: str, lines: int = Query(default=100, ge=1, le=10000)) -> dict[str, str]:
    """Return the last ``lines`` lines of the session's tmux scrollback buffer."""
    sid = safe_id(session_id)
    name = tmux_name(sid)
    if not await tmux_session_exists(name):
        raise HTTPException(status_code=404, detail=f"session not found: {sid}")
    _, out, _ = await _tmux("capture-pane", "-t", name, "-p", "-S", f"-{lines}")
    return {"output": out}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8082, log_level="info")
