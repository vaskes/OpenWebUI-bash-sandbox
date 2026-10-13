"""OpenAPI 3.0 proxy for sandbox-agent.

Exposes the sandbox-agent HTTP API as a single ``bash`` tool that Open WebUI
can consume as an *External Tool Server*. No patching of Open WebUI itself is
required — drop in the connection URL, and the LLM can run any shell command
in a persistent sandbox.

Two convenience pseudo-commands are recognised by the proxy before the
command is forwarded to the sandbox:

* ``web_search <query>`` — search via a configured SearXNG instance and
  return JSON. Useful when the model needs up-to-date web context.
* ``ssh <rest>`` — append ``-o StrictHostKeyChecking=accept-new`` to make
  first-time SSH access frictionless.

For everything else the command is forwarded to ``POST /sessions/{id}/exec``
on the upstream sandbox-agent.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from proxy_core import (
    DEFAULT_SESSION_ID,
    EXEC_TIMEOUT_SECONDS,
    SEARXNG_TIMEOUT_SECONDS,
    SSH_PREFIX,
    WEBSOCKET_POLL_INTERVAL,
    WEBSOCKET_SESSION_ID,
    WEB_SEARCH_PREFIX,
    __version__ as _PROXY_CORE_VERSION,
)

__version__ = _PROXY_CORE_VERSION

app = FastAPI(
    title="sandbox-agent",
    version=__version__,
    description=__doc__,
    openapi_version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SANDBOX_URL: str = os.environ.get("SANDBOX_URL", "http://127.0.0.1:8082")
SEARXNG_URL: str = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888")


class BashReq(BaseModel):
    """Request body for ``POST /bash``."""

    command: str = Field(..., description="Shell command. Persistent state preserved across calls within same session_id.")
    session_id: str = Field(default=DEFAULT_SESSION_ID, description="Session ID for persistent shell.")


@app.get("/healthz")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


@app.get("/api/config")
async def api_config() -> dict[str, Any]:
    """Open WebUI capability probe. Returning ``features.system=False`` disables
    the (unused) system-prompt endpoint, keeping the tool surface minimal."""
    return {
        "name": "sandbox-agent",
        "version": __version__,
        "features": {"system": False},
    }


async def _do_web_search(client: httpx.AsyncClient, query: str) -> dict[str, Any]:
    """Search via SearXNG and return the top 5 results as a JSON-friendly dict."""
    try:
        r = await client.get(
            f"{SEARXNG_URL}/search",
            params={"q": query, "format": "json"},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        results = r.json().get("results", [])[:5]
        return {
            "results": [
                {"title": item.get("title", ""), "url": item.get("url", "")}
                for item in results
            ]
        }
    except httpx.HTTPError as exc:
        return {"error": f"searxng request failed: {exc}"}
    except Exception as exc:  # noqa: BLE001 — last-resort guard for JSON parse errors
        return {"error": f"searxng response parse failed: {exc}"}


async def _do_exec(client: httpx.AsyncClient, command: str, session_id: str) -> dict[str, Any]:
    """Forward a command to sandbox-agent and return its JSON response."""
    try:
        r = await client.post(
            f"{SANDBOX_URL}/sessions/{session_id}/exec",
            json={"command": command, "timeout": 60},
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        return {"error": f"sandbox-agent request failed: {exc}"}


async def run_bash(command: str, session_id: str = DEFAULT_SESSION_ID) -> dict[str, Any]:
    """Top-level command dispatcher.

    Recognises ``web_search <q>`` and ``ssh <rest>`` shortcuts; everything
    else is forwarded verbatim to the sandbox.
    """
    stripped = command.strip()
    async with httpx.AsyncClient(timeout=EXEC_TIMEOUT_SECONDS) as client:
        if stripped.startswith(WEB_SEARCH_PREFIX):
            query = stripped[len(WEB_SEARCH_PREFIX):].strip()
            return await _do_web_search(client, query)
        if stripped.startswith(SSH_PREFIX):
            # Avoid strict-host-key prompts on first connect.
            cmd = f"ssh -o StrictHostKeyChecking=accept-new {stripped[len(SSH_PREFIX):]}"
        else:
            cmd = command
        return await _do_exec(client, cmd, session_id)


@app.post("/bash", operation_id="bash")
async def bash(req: BashReq) -> dict[str, Any]:
    """Run a shell command in the persistent sandbox. Single tool, full power."""
    return await run_bash(req.command, req.session_id)


@app.post("/shell_exec", operation_id="shell_exec")
async def shell_exec(req: BashReq) -> dict[str, Any]:
    """Alias for :func:`bash`, kept for compatibility with older Open WebUI
    tool-server configurations that look for ``shell_exec`` instead of ``bash``."""
    return await run_bash(req.command, req.session_id)


# ---------------------------------------------------------------------------
# WebSocket terminal
# ---------------------------------------------------------------------------
# Best-effort experimental terminal endpoint. The LLM/UI sees a live shell;
# the proxy polls tmux via sandbox-agent and pushes diffs to the client.


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Bidirectional terminal. The client sends lines, the proxy echoes the
    tmux pane state. Useful for an interactive tab inside Open WebUI's
    *Open Terminal* feature."""
    await websocket.accept()
    session_id = WEBSOCKET_SESSION_ID
    await websocket.send_text("$ Welcome to sandbox-agent terminal\r\n")

    # Make sure the tmux session exists. Failures here are non-fatal.
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{SANDBOX_URL}/sessions",
                json={"session_id": session_id, "cwd": "/workspace"},
            )
    except httpx.HTTPError:
        pass

    stop = asyncio.Event()

    async def read_from_tmux() -> None:
        """Background task: poll tmux pane, push new bytes to the websocket."""
        last_output = ""
        while not stop.is_set():
            try:
                capture_cmd = f"tmux capture-pane -t agent_{session_id} -p | tail -100"
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.post(
                        f"{SANDBOX_URL}/sessions/{session_id}/exec",
                        json={"command": capture_cmd, "timeout": 5},
                    )
                    output = r.json().get("stdout", "")
                if output != last_output:
                    new = output[len(last_output):] if output.startswith(last_output) else output
                    if new:
                        await websocket.send_text(new)
                    last_output = output
            except (httpx.HTTPError, WebSocketDisconnect):
                break
            await asyncio.sleep(WEBSOCKET_POLL_INTERVAL)

    reader = asyncio.create_task(read_from_tmux())
    try:
        while True:
            data = await websocket.receive_text()
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"{SANDBOX_URL}/sessions/{session_id}/exec",
                        json={"command": data, "timeout": 5},
                    )
            except httpx.HTTPError:
                # Drop the connection if the backend is unreachable.
                break
    except WebSocketDisconnect:
        pass
    finally:
        stop.set()
        reader.cancel()
        try:
            await reader
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
