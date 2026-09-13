"""OpenAPI 3.0 proxy for sandbox-agent — bash tool + WS-compatible."""
import os
import json
import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="sandbox-agent", version="2.0.0", openapi_version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SANDBOX_URL = os.environ.get("SANDBOX_URL", "http://127.0.0.1:8082")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888")


class BashReq(BaseModel):
    command: str = Field(..., description="Shell command. Persistent state preserved across calls within same session_id.")
    session_id: str = Field("openwebui", description="Session ID for persistent shell.")


@app.get("/healthz")
async def health():
    return {"status": "ok"}


@app.get("/api/config")
async def api_config():
    """Open WebUI expects this endpoint for terminal servers (system prompt feature)."""
    return {
        "name": "sandbox-agent",
        "version": "2.0.0",
        "features": {
            "system": False,  # disable /system endpoint
        }
    }


async def run_bash(command: str, session_id: str = "openwebui") -> dict:
    async with httpx.AsyncClient(timeout=120) as client:
        if command.strip().startswith("web_search "):
            query = command.strip()[len("web_search "):].strip()
            try:
                r = await client.get(f"{SEARXNG_URL}/search",
                    params={"q": query, "format": "json"},
                    headers={"User-Agent": "Mozilla/5.0"})
                results = r.json().get("results", [])[:5]
                return {"results": [{"title": x.get("title",""), "url": x.get("url","")} for x in results]}
            except Exception as e:
                return {"error": str(e)}
        if command.strip().startswith("ssh "):
            cmd = f"ssh -o StrictHostKeyChecking=accept-new {command.strip()[4:]}"
        else:
            cmd = command
        try:
            r = await client.post(f"{SANDBOX_URL}/sessions/{session_id}/exec",
                json={"command": cmd, "timeout": 60})
            return r.json()
        except Exception as e:
            return {"error": str(e)}


@app.post("/bash", operation_id="bash")
async def bash(req: BashReq):
    return await run_bash(req.command, req.session_id)


# WebSocket for terminal connection
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    session_id = "openwebui-ws"
    # Send initial prompt
    await websocket.send_text("$ Welcome to sandbox-agent terminal\r\n")
    # tmux session needs to exist
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.post(f"{SANDBOX_URL}/sessions", json={"session_id": session_id, "cwd": "/workspace"})
        except:
            pass
    
    import asyncio
    loop = asyncio.get_running_loop()
    
    async def read_from_tmux():
        """Read from tmux output and send to websocket."""
        last_output = ""
        while True:
            try:
                # Capture tmux pane
                cmd = f"tmux capture-pane -t agent_{session_id} -p | tail -100"
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.post(f"{SANDBOX_URL}/sessions/{session_id}/exec",
                        json={"command": cmd, "timeout": 5})
                    output = r.json().get("stdout", "")
                if output != last_output:
                    # Send only diff
                    new = output[len(last_output):] if output.startswith(last_output) else output
                    if new:
                        await websocket.send_text(new)
                    last_output = output
            except:
                pass
            await asyncio.sleep(0.5)
    
    reader = asyncio.create_task(read_from_tmux())
    
    try:
        while True:
            data = await websocket.receive_text()
            # Send to tmux
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(f"{SANDBOX_URL}/sessions/{session_id}/exec",
                    json={"command": data, "timeout": 5})
    except WebSocketDisconnect:
        reader.cancel()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
