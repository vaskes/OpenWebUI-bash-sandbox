#!/usr/bin/env python3
"""Sandbox Agent — persistent tmux sessions per chat, HTTP API on :8082."""
import asyncio
import os
import re
import time
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Sandbox Agent", version="1.0")

# session_id -> {tmux_name, created, cwd, alive}
SESSIONS: dict[str, dict] = {}
TMUX_PREFIX = "agent_"
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 600  # 10 min hard cap
WORKSPACE = "/workspace"


def safe_id(s: str) -> str:
    """Sanitize session id — only [a-zA-Z0-9_-], max 64 chars."""
    s = re.sub(r"[^a-zA-Z0-9_-]", "_", s)[:64]
    return s or "default"


def tmux_name(session_id: str) -> str:
    return f"{TMUX_PREFIX}{safe_id(session_id)}"


async def tmux(*args, check: bool = False) -> tuple[int, str, str]:
    """Run tmux command. Returns (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "tmux", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


async def tmux_session_exists(name: str) -> bool:
    rc, out, _ = await tmux("has-session", "-t", name)
    return rc == 0


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "sessions": len(SESSIONS), "workspace": WORKSPACE}


@app.get("/sessions")
async def list_sessions():
    # Cleanup dead sessions from registry
    dead = []
    for sid, info in SESSIONS.items():
        if not await tmux_session_exists(info["tmux_name"]):
            dead.append(sid)
    for sid in dead:
        SESSIONS.pop(sid, None)
    return {
        "sessions": [
            {"session_id": sid, "tmux_name": info["tmux_name"], "cwd": info.get("cwd"), "created": info["created"]}
            for sid, info in SESSIONS.items()
        ]
    }


class CreateSession(BaseModel):
    session_id: str = Field(default="default", max_length=64)
    cwd: Optional[str] = None


@app.post("/sessions")
async def create_session(body: CreateSession):
    sid = safe_id(body.session_id)
    name = tmux_name(sid)
    if sid in SESSIONS and await tmux_session_exists(name):
        return {"session_id": sid, "tmux_name": name, "existed": True}
    cwd = body.cwd or WORKSPACE
    if not os.path.isdir(cwd):
        raise HTTPException(400, f"cwd does not exist: {cwd}")
    # Start tmux session
    rc, out, err = await tmux("new-session", "-d", "-s", name, "-c", cwd, "-x", "200", "-y", "50")
    if rc != 0:
        raise HTTPException(500, f"tmux new-session failed: {err}")
    SESSIONS[sid] = {
        "tmux_name": name,
        "created": time.time(),
        "cwd": cwd,
    }
    return {"session_id": sid, "tmux_name": name, "existed": False, "cwd": cwd}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    sid = safe_id(session_id)
    name = tmux_name(sid)
    await tmux("kill-session", "-t", name, check=False)
    SESSIONS.pop(sid, None)
    return {"deleted": sid}


class ExecRequest(BaseModel):
    command: str
    timeout: int = Field(default=DEFAULT_TIMEOUT, ge=1, le=MAX_TIMEOUT)


@app.post("/sessions/{session_id}/exec")
async def exec_cmd(session_id: str, body: ExecRequest):
    sid = safe_id(session_id)
    name = tmux_name(sid)
    if not await tmux_session_exists(name):
        # Auto-create
        await create_session(CreateSession(session_id=sid, cwd=WORKSPACE))
    # Send command to tmux
    # Use a unique marker to know when output ends
    marker = f"__AGENT_DONE_{uuid.uuid4().hex[:8]}__"
    # Wrap command to print marker after; capture exit code via $?
    full_cmd = (
        f"{body.command}\n"
        f"__ec=$?\n"
        f"printf '\\n{marker}=%d\\n' \"$__ec\""
    )
    # Send as single line (tmux send-keys can be tricky with newlines; use -l literal)
    await tmux("send-keys", "-t", name, full_cmd, "Enter")
    # Poll for marker
    start = time.time()
    last_output = ""
    exit_code = None
    while time.time() - start < body.timeout:
        rc, out, _ = await tmux("capture-pane", "-t", name, "-p")
        if rc == 0 and marker in out:
            exit_code = 0
            last_output = out
            break
        await asyncio.sleep(0.3)
    else:
        # Timeout — return what we have
        rc, last_output, _ = await tmux("capture-pane", "-t", name, "-p")
        return {
            "stdout": last_output,
            "stderr": "",
            "exit_code": None,
            "timed_out": True,
            "duration_ms": int((time.time() - start) * 1000),
        }
    # Parse exit code from marker line
    m = re.search(rf"{marker}=(-?\d+)", last_output)
    if m:
        exit_code = int(m.group(1))
        # Strip marker line and everything after
        last_output = re.sub(rf"\n?{marker}=-?\d+\n?", "", last_output).rstrip()
    return {
        "stdout": last_output,
        "stderr": "",
        "exit_code": exit_code,
        "timed_out": False,
        "duration_ms": int((time.time() - start) * 1000),
    }


@app.get("/sessions/{session_id}/output")
async def tail_output(session_id: str, lines: int = 100):
    sid = safe_id(session_id)
    name = tmux_name(sid)
    if not await tmux_session_exists(name):
        raise HTTPException(404, f"session not found: {sid}")
    await tmux("capture-pane", "-t", name, "-p", "-S", f"-{lines}")
    # capture-pane with -S -N needs different invocation; use scrollback via pipe
    rc, out, _ = await tmux("capture-pane", "-t", name, "-p", "-S", f"-{lines}")
    return {"output": out}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8082, log_level="info")
