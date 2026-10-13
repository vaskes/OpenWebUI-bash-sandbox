"""OpenAPI 3.0 proxy for sandbox-agent.

Exposes the sandbox-agent HTTP API as a single ``bash`` tool that Open WebUI
can consume as an *External Tool Server*. No patching of Open WebUI itself is
required — drop in the connection URL, and the LLM can run any shell command
in a persistent sandbox.

Security
--------
* **Bearer-token auth** on every endpoint. The token is loaded from
  ``OPENAPI_PROXY_TOKEN`` and rotated via restart.
* **CORS allowlist** — origins must be configured via
  ``OPENAPI_PROXY_CORS_ORIGINS`` (comma-separated). Defaults to nothing
  (i.e. cross-origin browser calls are blocked). Set explicitly for
  browser-based UIs.
* **Singleton httpx client** — one TCP connection pool for the lifetime of
  the process; configured in the FastAPI lifespan handler.
* **Per-WebSocket session_id** — every WS connection gets its own tmux
  session, not a shared one. Closes the cross-user output-leak vector.
* **Rate limiting** — simple in-memory token-bucket; protects the upstream
  from runaway LLM tool loops.

Two convenience pseudo-commands are recognised by the proxy before the
command is forwarded to the sandbox:

* ``web_search <query>`` — search via a configured SearXNG instance and
  return JSON. Useful when the model needs up-to-date web context.
* ``ssh <rest>`` — invoke ``ssh`` against a host from the configured
  whitelist (``OPENAPI_PROXY_SSH_ALLOWLIST``).

For everything else the command is forwarded to ``POST /sessions/{id}/exec``
on the upstream sandbox-agent.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from proxy_core import (
    DEFAULT_SESSION_ID,
    EXEC_TIMEOUT_SECONDS,
    SEARXNG_TIMEOUT_SECONDS,
    SSH_PREFIX,
    WEBSOCKET_POLL_INTERVAL,
    WEB_SEARCH_PREFIX,
    __version__ as _PROXY_CORE_VERSION,
)

__version__ = _PROXY_CORE_VERSION

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SANDBOX_URL: str = os.environ.get("SANDBOX_URL", "http://127.0.0.1:8082")
SEARXNG_URL: str = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888")
PROXY_TOKEN: str = os.environ.get("OPENAPI_PROXY_TOKEN", "")
CORS_ORIGINS: list[str] = [
    o.strip() for o in os.environ.get("OPENAPI_PROXY_CORS_ORIGINS", "").split(",") if o.strip()
]
# Hosts the proxy is allowed to forward ``ssh <host>`` to. Comma-separated
# hostnames or CIDRs (e.g. ``lab.local,10.0.0.0/8``). Empty list disables
# the ``ssh`` shortcut entirely.
SSH_ALLOWLIST: list[str] = [
    h.strip() for h in os.environ.get("OPENAPI_PROXY_SSH_ALLOWLIST", "").split(",") if h.strip()
]
# In-memory rate limit: max RATE_LIMIT_RPM requests per minute per token.
# Set to 0 to disable.
RATE_LIMIT_RPM: int = int(os.environ.get("OPENAPI_PROXY_RATE_LIMIT_RPM", "120"))

logger = logging.getLogger("openapi-proxy")
logging.basicConfig(
    level=os.environ.get("OPENAPI_PROXY_LOG_LEVEL", "INFO"),
    format='{"ts":"%(asctime)s","logger":"%(name)s","level":"%(levelname)s","msg":"%(message)s"}',
)


# ---------------------------------------------------------------------------
# Rate limiter (in-memory token bucket per token)
# ---------------------------------------------------------------------------
class _RateLimiter:
    """Token-bucket rate limiter, in-memory. Resets on process restart —
    that's acceptable for a single-replica service. For a multi-replica
    deployment, swap for a Redis-backed implementation."""

    def __init__(self, requests_per_minute: int):
        self.rpm = requests_per_minute
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_refill_ts)

    def check(self, key: str) -> bool:
        if self.rpm <= 0:
            return True
        now = time.time()
        tokens, last_refill = self._buckets.get(key, (float(self.rpm), now))
        # Refill at rpm/60 tokens per second.
        tokens = min(float(self.rpm), tokens + (now - last_refill) * (self.rpm / 60.0))
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True


_rate_limiter = _RateLimiter(RATE_LIMIT_RPM)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
_bearer = HTTPBearer(auto_error=False)


def _require_token(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> str:
    """FastAPI dependency: validate ``Authorization: Bearer <token>``.

    Rejects the request with 401 if the proxy has no token configured (we
    never want an unauthenticated deployment) or if the supplied token
    doesn't match. Constant-time comparison defeats timing attacks.
    """
    if not PROXY_TOKEN:
        logger.error("auth disabled but OPENAPI_PROXY_TOKEN is empty — refusing all requests")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="proxy not configured")
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not hmac.compare_digest(creds.credentials, PROXY_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return creds.credentials


async def _rate_limit(request: Request) -> None:
    """FastAPI dependency: enforce per-token rate limit. Key = token, so
    users can't bypass the limit by rotating their session id."""
    auth = request.headers.get("authorization", "")
    key = auth or request.client.host if request.client else "anon"
    if not _rate_limiter.check(key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded",
            headers={"Retry-After": "60"},
        )


# ---------------------------------------------------------------------------
# SSH allowlist
# ---------------------------------------------------------------------------
def _ssh_host_allowed(host: str) -> bool:
    """Return True if ``host`` matches the SSH allowlist (exact hostname or
    CIDR for IP literals). Empty allowlist denies all."""
    if not SSH_ALLOWLIST:
        return False
    # Try as IP / CIDR first.
    try:
        addr = ipaddress.ip_address(host)
        for entry in SSH_ALLOWLIST:
            try:
                net = ipaddress.ip_network(entry, strict=False)
            except ValueError:
                continue
            if addr in net:
                return True
        return False
    except ValueError:
        pass
    return host in SSH_ALLOWLIST


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage a singleton :class:`httpx.AsyncClient` for the lifetime of
    the process. Reusing one client across requests enables HTTP keep-alive
    and a single connection pool, which matters for high-frequency calls
    (e.g. WebSocket polling)."""
    app.state.client = httpx.AsyncClient(
        timeout=EXEC_TIMEOUT_SECONDS,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
    )
    logger.info("proxy.startup token=%sandbox cors=%d", "DISABLED " if not PROXY_TOKEN else "", len(CORS_ORIGINS))
    try:
        yield
    finally:
        await app.state.client.aclose()
        logger.info("proxy.shutdown")


app = FastAPI(
    title="sandbox-agent",
    version=__version__,
    description=__doc__,
    openapi_version="3.0.0",
    lifespan=lifespan,
)

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
else:
    # No CORS = no cross-origin browser access. APIs can still call us from
    # non-browser clients because CORS only governs the browser preflight.
    logger.info("cors disabled (no OPENAPI_PROXY_CORS_ORIGINS configured)")


def _client(request: Request) -> httpx.AsyncClient:
    """Return the singleton httpx client attached to the app lifespan."""
    return request.app.state.client


class BashReq(BaseModel):
    """Request body for ``POST /bash``."""

    command: str = Field(..., min_length=1, max_length=1_000_000, description="Shell command. Persistent state preserved across calls within same session_id.")
    session_id: str = Field(default=DEFAULT_SESSION_ID, max_length=128, description="Session ID for persistent shell.")


@app.get("/healthz")
async def health(_: str = Depends(_require_token)) -> dict[str, str]:
    """Liveness probe (auth required to avoid leaking server info to anonymous probers)."""
    return {"status": "ok"}


@app.get("/api/config")
async def api_config(_: str = Depends(_require_token)) -> dict[str, Any]:
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
            headers={"User-Agent": "openapi-proxy/2.1 (sandbox-agent)"},
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


async def _do_ssh(client: httpx.AsyncClient, rest: str, session_id: str) -> dict[str, Any]:
    """Validate that the SSH target is in the allowlist, then forward to the sandbox."""
    tokens = rest.split()
    if not tokens:
        return {"error": "ssh: missing host"}
    # Strip ``user@`` prefix; the allowlist is hostnames/CIDRs only.
    target = tokens[0]
    host = target.split("@", 1)[-1] if "@" in target else target
    if not _ssh_host_allowed(host):
        logger.warning("ssh.denied host=%s", host)
        return {"error": f"ssh host not in allowlist: {host}"}
    cmd = f"ssh -o StrictHostKeyChecking=accept-new {rest}"
    try:
        r = await client.post(
            f"{SANDBOX_URL}/sessions/{session_id}/exec",
            json={"command": cmd, "timeout": 60},
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        return {"error": f"sandbox-agent request failed: {exc}"}


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


async def run_bash(
    client: httpx.AsyncClient,
    command: str,
    session_id: str = DEFAULT_SESSION_ID,
) -> dict[str, Any]:
    """Top-level command dispatcher.

    Recognises ``web_search <q>`` and ``ssh <rest>`` shortcuts; everything
    else is forwarded verbatim to the sandbox.
    """
    stripped = command.strip()
    if stripped.startswith(WEB_SEARCH_PREFIX):
        query = stripped[len(WEB_SEARCH_PREFIX):].strip()
        return await _do_web_search(client, query)
    if stripped.startswith(SSH_PREFIX):
        rest = stripped[len(SSH_PREFIX):]
        return await _do_ssh(client, rest, session_id)
    return await _do_exec(client, command, session_id)


@app.post("/bash", operation_id="bash", dependencies=[Depends(_rate_limit)])
async def bash(req: BashReq, request: Request, _: str = Depends(_require_token)) -> dict[str, Any]:
    """Run a shell command in the persistent sandbox. Single tool, full power."""
    client = _client(request)
    logger.info("bash sid=%s cmd_len=%d", req.session_id, len(req.command))
    return await run_bash(client, req.command, req.session_id)


@app.post("/shell_exec", operation_id="shell_exec", dependencies=[Depends(_rate_limit)])
async def shell_exec(req: BashReq, request: Request, _: str = Depends(_require_token)) -> dict[str, Any]:
    """Alias for :func:`bash`, kept for compatibility with older Open WebUI
    tool-server configurations that look for ``shell_exec`` instead of ``bash``."""
    client = _client(request)
    return await run_bash(client, req.command, req.session_id)


# ---------------------------------------------------------------------------
# WebSocket terminal
# ---------------------------------------------------------------------------
# Experimental terminal endpoint. The LLM/UI sees a live shell; the proxy
# polls sandbox-agent's /capture endpoint (not /exec — that would spawn a
# shell per poll, which leaks file descriptors and burns CPU).


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Bidirectional terminal. Per-connection session_id prevents cross-user
    output leakage. Reads use the dedicated /capture endpoint instead of
    a shell wrapper so the polling loop is cheap."""
    if not PROXY_TOKEN:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="proxy not configured")
        return

    # Auth: token can come as a query param (browsers can't set headers on WS)
    # or as the standard ``Sec-WebSocket-Protocol`` header.
    token = websocket.query_params.get("token", "")
    if not token:
        # Try subprotocol — browsers can negotiate via that.
        for proto in websocket.headers.get("sec-websocket-protocol", "").split(","):
            proto = proto.strip()
            if proto.startswith("bearer."):
                token = proto[len("bearer."):]
                break
    if not hmac.compare_digest(token, PROXY_TOKEN):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="unauthorized")
        return

    await websocket.accept()
    # Unique session per connection — no cross-user leaks.
    session_id = f"ws-{uuid.uuid4().hex[:12]}"
    await websocket.send_text(f"$ sandbox-agent terminal session={session_id}\r\n")

    client = websocket.app.state.client
    try:
        # Make sure the tmux session exists. Failures here are non-fatal.
        await client.post(
            f"{SANDBOX_URL}/sessions",
            json={"session_id": session_id, "cwd": "/workspace"},
        )
    except httpx.HTTPError:
        pass

    stop = asyncio.Event()

    async def read_from_tmux() -> None:
        """Background task: poll sandbox-agent /capture, push new bytes to the websocket."""
        last_output = ""
        while not stop.is_set():
            try:
                r = await client.get(
                    f"{SANDBOX_URL}/sessions/{session_id}/capture",
                    params={"lines": 100},
                )
                r.raise_for_status()
                output = r.json().get("output", "")
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
        # Best-effort cleanup of the per-connection tmux session.
        try:
            await client.delete(f"{SANDBOX_URL}/sessions/{session_id}")
        except httpx.HTTPError:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "openapi_proxy:app",
        host="0.0.0.0",
        port=int(os.environ.get("OPENAPI_PROXY_PORT", "8090")),
        log_level=os.environ.get("OPENAPI_PROXY_LOG_LEVEL", "info").lower(),
        timeout_graceful_shutdown=25,
    )
