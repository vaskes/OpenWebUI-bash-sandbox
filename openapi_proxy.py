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

One convenience pseudo-command is recognised by the proxy before the command
is forwarded to the sandbox:

* ``ssh <rest>`` — invoke ``ssh`` against a host from the configured
  whitelist (``OPENAPI_PROXY_SSH_ALLOWLIST``).

For everything else the command is forwarded to ``POST /sessions/{id}/exec``
on the upstream sandbox-agent.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import re
import shlex
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from body_limit import BodySizeLimit
from json_logging import configure_logging
from metrics import Metrics
from proxy_core import (
    DEFAULT_SESSION_ID,
    EXEC_TIMEOUT_SECONDS,
    PROXY_WAIT_GRACE_SECONDS,
    SSH_PREFIX,
    WEBSOCKET_POLL_INTERVAL,
    parse_exec_timeout,
    token_problem,
)
from proxy_core import (
    __version__ as _PROXY_CORE_VERSION,
)
from token_rules import is_placeholder

__version__ = _PROXY_CORE_VERSION

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SANDBOX_URL: str = os.environ.get("SANDBOX_URL", "http://127.0.0.1:8082")
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
# Upper bound on the number of distinct rate-limit keys we track. Past
# this we evict the least-recently-used key — prevents unbounded growth
# under token-spraying attacks.
RATE_LIMIT_MAX_KEYS: int = int(os.environ.get("OPENAPI_PROXY_RATE_LIMIT_MAX_KEYS", "10000"))
#: The exec limit the proxy asks the sandbox-agent for, in seconds. It is
#: the longest a single command may run before the agent gives up on it
#: and answers ``timed_out: true``.
#:
#: Configurable via ``AGENT_EXEC_TIMEOUT`` (5..3600, default 60). A value
#: outside the range is a start-up failure, not a clamp: a limit nobody
#: can trust is worse than a container that refuses to boot.
AGENT_EXEC_TIMEOUT: int = parse_exec_timeout(
    os.environ.get("AGENT_EXEC_TIMEOUT", str(int(EXEC_TIMEOUT_SECONDS)))
)
#: How long the proxy waits for the agent. Strictly larger than the exec
#: limit it asked for — see ``PROXY_WAIT_GRACE_SECONDS``. With the same
#: number on both sides, a command that runs out of time loses its
#: answer: the agent answers at the very moment the proxy stops
#: listening, and the caller sees a transport error instead of the
#: timeout.
PROXY_REQUEST_TIMEOUT_SECONDS: float = AGENT_EXEC_TIMEOUT + PROXY_WAIT_GRACE_SECONDS
#: How long the MCP relay waits for the agent.
#:
#: The exec door and the MCP door carry different horizons. The exec
#: door's horizon is this proxy's own ``AGENT_EXEC_TIMEOUT`` — the proxy
#: decides it, asks the agent for that much, and waits a little longer.
#: The MCP door's horizon is the *model's*: the tool takes a ``timeout``
#: argument, and the agent honours it up to its own hard cap (3600 s).
#: With the shared client timeout the relay would drop the connection
#: at 75 s and the caller would get a transport error instead of the
#: answer the agent was still computing.
#:
#: So the MCP relay waits longer than the agent can possibly work: the
#: agent always answers within its own cap, and the relay's job is to
#: be there when it does. The number is a ceiling, not a policy — a
#: command that ends early ends the call early.
#:
#: The agent's hard cap lives in ``agent_api.MAX_TIMEOUT``; a tripwire
#: checks that this number is still above it.
MCP_RELAY_TIMEOUT_SECONDS: float = 3660.0
# Idle timeout for WebSocket terminal connections (sandbox-recommendations #M3).
# Connections with no inbound traffic for this long are closed by the server
# so a forgotten browser tab does not pin a file descriptor and a tmux
# session forever. Default: 5 minutes, the de-facto standard for
# interactive terminals. 0 disables the timeout.
WEBSOCKET_IDLE_TIMEOUT_SECONDS: int = int(
    os.environ.get("OPENAPI_PROXY_WEBSOCKET_IDLE_TIMEOUT", "300")
)
# SSH shortcuts: which ``-o`` flags we allow (defence against command
# injection via ``ProxyCommand`` and friends). Anything not in this set
# is rejected.
SSH_ALLOWED_FLAGS: set[str] = {"-p", "-l", "-o", "-q", "-v", "-vv", "-vvv"}
# Allowlist of ``-o key=value`` options the proxy will forward. We deny
# everything else — including ``ProxyCommand`` (RCE), ``LocalCommand`` /
# ``PermitLocalCommand`` (local execution), ``LocalForward`` /
# ``RemoteForward`` (port forwarding to arbitrary hosts) and
# ``DynamicForward`` (SOCKS proxy). The list only contains options that
# adjust connection behaviour, not what the connection does.
SSH_SAFE_OPTIONS: set[str] = {
    "ConnectTimeout",
    "ServerAliveInterval",
    "ServerAliveCountMax",
    "ConnectionAttempts",
    "BatchMode",
    "AddressFamily",
    "BindAddress",
    "IPQoS",
    "Compression",
    "TCPKeepAlive",
}

logger = logging.getLogger("openapi-proxy")
# One JSON line per record, with the message escaped properly — the
# proxy logs user-supplied text (a rejected ssh command, for example),
# and the old format string broke the JSON on the first quote in it.
configure_logging(os.environ.get("OPENAPI_PROXY_LOG_LEVEL", "INFO"))


# ---------------------------------------------------------------------------
# Rate limiter (in-memory token bucket per token)
# ---------------------------------------------------------------------------
class _RateLimiter:
    """Token-bucket rate limiter with LRU eviction.

    In-memory and resets on process restart — that's acceptable for a
    single-replica service. For a multi-replica deployment, swap for a
    Redis-backed implementation. The LRU cap prevents unbounded memory
    growth when attackers rotate tokens.
    """

    def __init__(self, requests_per_minute: int, max_keys: int = 10_000):
        self.rpm = requests_per_minute
        self.max_keys = max_keys
        # OrderedDict so we can popitem(last=False) to evict the LRU.
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def check(self, key: str) -> bool:
        if self.rpm <= 0:
            return True
        now = time.time()
        if key in self._buckets:
            self._buckets.move_to_end(key)
            tokens, last_refill = self._buckets[key]
        else:
            if len(self._buckets) >= self.max_keys:
                self._buckets.popitem(last=False)  # evict oldest
            tokens, last_refill = float(self.rpm), now
        # Refill at rpm/60 tokens per second.
        tokens = min(float(self.rpm), tokens + (now - last_refill) * (self.rpm / 60.0))
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True


_rate_limiter = _RateLimiter(RATE_LIMIT_RPM, RATE_LIMIT_MAX_KEYS)

#: What the proxy exposes on ``/metrics``. Counters only go up; the
#: gauge is read at scrape time.
METRICS = Metrics("sandbox_proxy")
METRICS.counter("requests_total", "Requests the proxy handled.")
METRICS.counter("rate_limited_total", "Requests refused by the rate limiter.")
METRICS.counter("exec_timeouts_total", "Upstream calls that ran out of time.")
METRICS.counter("upstream_errors_total", "Upstream calls that failed for another reason.")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
_bearer = HTTPBearer(auto_error=False)


def _require_token(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008
) -> str:
    """FastAPI dependency: validate ``Authorization: Bearer <token>``.

    Rejects the request with 401 if the proxy has no token configured (we
    never want an unauthenticated deployment) or if the supplied token
    doesn't match. Constant-time comparison defeats timing attacks.

    A token that is still the template's **placeholder** is refused the
    same way an empty one is: it is a published string, so a deployment
    carrying it has no secret — only the appearance of one. The refusal
    is 503 rather than 401: nothing is wrong with the caller, the
    deployment is.
    """
    if not PROXY_TOKEN:
        logger.error("auth disabled but OPENAPI_PROXY_TOKEN is empty — refusing all requests")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="proxy not configured"
        )
    if is_placeholder(PROXY_TOKEN):
        logger.error(
            "auth disabled but OPENAPI_PROXY_TOKEN is still the template placeholder — "
            "refusing all requests"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="proxy not configured"
        )
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
    """FastAPI dependency: enforce per-token rate limit. Key = hash(token),
    so users can't bypass the limit by rotating their session id AND we
    don't store the bearer token in memory longer than necessary.

    Wave 3 #L3: the previous implementation keyed on the full
    ``Authorization`` header (including the bearer token). The dict is
    process-local and LRU-evicted, so the risk was bounded — but
    hashing the token before keying removes the in-memory token
    presence entirely. The hash is keyed on the *whole* header so
    different tokens (or no token) still get different buckets.
    """
    auth = request.headers.get("authorization", "") or (
        request.client.host if request.client else "anon"
    )
    import hashlib as _hashlib  # noqa: PLC0415

    key = _hashlib.sha256(auth.encode("utf-8")).hexdigest()
    if not _rate_limiter.check(key):
        METRICS.inc("rate_limited_total")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded",
            headers={"Retry-After": "60"},
        )


# ---------------------------------------------------------------------------
# SSH allowlist + argument validation
# ---------------------------------------------------------------------------
def _ssh_host_allowed(host: str) -> bool:
    """Return True if ``host`` matches the SSH allowlist (exact hostname or
    CIDR for IP literals). Hostname comparison is case-insensitive per
    DNS semantics. Empty allowlist denies all."""
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
    return host.lower() in {h.lower() for h in SSH_ALLOWLIST}


def _validate_ssh_args(rest: str) -> tuple[str | None, str | None]:
    """Parse the ``ssh …`` payload and return ``(host, safe_argv_string)``.

    Defence-in-depth: we accept only a small whitelist of ssh flags
    (``-p``, ``-l``, ``-o``, ``-q``, ``-v…``) plus an optional
    ``user@host`` or ``host`` token. Anything else — ``-i`` (identity
    file), ``-J`` (jump host),
    ``-W`` (netcat mode), ``-L``/``-R`` (port forwards), ``-F`` (config
    override), ``-o ProxyCommand=…`` injection, ``-o LocalCommand=…``
    local execution, ``-o LocalForward=…`` arbitrary tunnelling — is
    rejected with a structured error. Returns ``(None, error)`` on
    failure.
    """
    try:
        tokens = shlex.split(rest)
    except ValueError as exc:
        return None, f"ssh: failed to parse: {exc}"
    if not tokens:
        return None, "ssh: missing host"
    # The caller passes the whole ``ssh …`` string — drop the literal
    # ``ssh`` command word if it's there. The shell_exec endpoint sends
    # ``command="ssh -p 22 host"`` and the first token is the program
    # name, not a positional argument.
    if tokens and tokens[0] == "ssh":
        tokens = tokens[1:]
    if not tokens:
        return None, "ssh: missing host"
    # Reject shell metacharacters — they're valid shlex tokens but
    # dangerous to forward into a tmux shell. We accept only tokens
    # made of ``[A-Za-z0-9_.@:=/-]`` plus the known flag characters.
    for tok in tokens:
        if not re.fullmatch(r"[A-Za-z0-9_.@:=/\-]+|-[A-Za-z]+", tok):
            return None, f"ssh: unsafe token rejected: {tok!r}"

    # Walk the whole token list. The only non-flag token is the host
    # (which may be ``user@host``). Flags must come from
    # :data:`SSH_ALLOWED_FLAGS`, ``-i`` is refused outright, and ``-o``
    # may only set options from :data:`SSH_SAFE_OPTIONS`.
    i = 0
    host: str | None = None
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-"):
            if host is not None:
                return None, f"ssh: multiple positional arguments not allowed: {tok!r}"
            host = tok
            i += 1
            continue
        if tok == "-i":
            # ``-i <path>`` was whitelisted in v2.4.x with a workspace
            # scope check. The check was a *path* check performed here,
            # in the proxy, while the file is opened by ``ssh`` later,
            # in the sandbox — two different processes, two different
            # filesystems, a round-trip in between. A model with a
            # second session could swap the validated file for a
            # symlink inside that window, and the code even said so
            # (the old ``_ssh_identity_within_workspace`` docstring
            # called the window "exploitable").
            #
            # There is no honest way to keep a *path* rule here: the
            # only check that cannot be raced is the one the shell
            # itself performs when it opens the file — and that check
            # is a permission check, not a location check. So the flag
            # is refused outright, and what protects the operator's key
            # is stated where it actually lives: the file permissions
            # of the read-only key mount (docs/SECURITY.md, section 3).
            return None, (
                "ssh: -i is not allowed: an identity file cannot be validated "
                "atomically with the ssh call that uses it, so the sandbox "
                "could swap it in between. Use the default identity, or run "
                "ssh outside this shortcut if you need a custom key."
            )
        if tok not in SSH_ALLOWED_FLAGS:
            return None, f"ssh: flag not allowed: {tok}"
        # Flags that take a value consume the next token.
        if tok in {"-p", "-l"} and i + 1 < len(tokens):
            i += 2
            continue
        if tok == "-o" and i + 1 < len(tokens):
            opt = tokens[i + 1]
            if "=" not in opt:
                return None, f"ssh: invalid -o option (missing =): {opt!r}"
            key = opt.split("=", 1)[0]
            # Whitelist by option *name*, case-insensitive — ssh itself
            # is case-insensitive on option names. A blocklist would
            # still let through new dangerous options we haven't
            # thought of.
            # SSH option names are case-insensitive — match real ssh.
            if key not in SSH_SAFE_OPTIONS and key.lower() not in {
                o.lower() for o in SSH_SAFE_OPTIONS
            }:
                return None, f"ssh: -o {key}=… not allowed"
            i += 2
            continue
        i += 1

    # Strip ``user@`` prefix; the allowlist is hostnames/CIDRs only.
    bare_host = host.split("@", 1)[-1]
    if not _ssh_host_allowed(bare_host):
        return None, f"ssh host not in allowlist: {bare_host}"

    # Re-assemble the command. StrictHostKeyChecking=accept-new is the
    # default; we don't need to override it.
    # The whole command. The slice used to end at the loop's stop
    # position — which happens to be the end of the token list, but only
    # because the loop walks every token. It reads as "up to the host",
    # and it is not: a flag after the host ("ssh user@host -p 2222")
    # is part of the command and has to reach the agent.
    return bare_host, " ".join(shlex.quote(t) for t in tokens[:])


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
        timeout=PROXY_REQUEST_TIMEOUT_SECONDS,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
    )
    logger.info(
        "proxy.startup token=%s cors=%d ssh_allowlist=%d rate_limit=%d exec_timeout=%d wait=%d",
        "DISABLED" if not PROXY_TOKEN else "ENABLED",
        len(CORS_ORIGINS),
        len(SSH_ALLOWLIST),
        RATE_LIMIT_RPM,
        AGENT_EXEC_TIMEOUT,
        int(PROXY_REQUEST_TIMEOUT_SECONDS),
    )
    # The token is not only a bearer token: the compose file interpolates
    # it into the Open WebUI tool connection as JSON. A token that breaks
    # that JSON leaves the UI without the sandbox tool and says nothing
    # about why — so the proxy says it here, at start-up, where an
    # operator is looking.
    problem = token_problem(PROXY_TOKEN)
    if problem:
        logger.error(
            "proxy.token_invalid %s — Open WebUI will not be able to parse "
            "the tool connection with this token. Regenerate it with: "
            "openssl rand -hex 32",
            problem,
        )
    try:
        yield
    finally:
        await app.state.client.aclose()
        logger.info("proxy.shutdown")


# NOTE: this app used to pass ``openapi_version="3.0.0"`` here. That
# keyword has never been a FastAPI parameter — on FastAPI <0.149 it was
# swallowed by the ``**kwargs`` catch-all in ``FastAPI.__init__`` and
# silently ignored (the service served 3.1.0 the whole time, so the
# kwarg was not even truthful). FastAPI >=0.149 removed the catch-all, so
# on a fresh install it raises ``TypeError`` at import and the proxy
# crash-loops. Removed in v2.5.0; ``requirements.txt`` no longer pins
# FastAPI to the 0.143 series because of it.
app = FastAPI(
    title="sandbox-agent",
    version=__version__,
    description=__doc__,
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request body size limit — defense in depth
# ---------------------------------------------------------------------------
# Same rationale as agent_api: cap the *body*, not just the field.
# The BashReq ``command`` field is already capped at 1 MB by Pydantic;
# this guard rejects the *whole* body if it would exceed 2 MB before
# Pydantic ever sees it.
MAX_BODY_BYTES = 2 * 1024 * 1024

# The limit counts the bytes as they arrive, not just the ones the
# client declared: a chunked body has no length to read, and reading
# only the header leaves the case the limit exists for open. The proxy
# is the published surface, so it is the one that has to hold the line.
app.add_middleware(BodySizeLimit, limit=MAX_BODY_BYTES)


@app.middleware("http")
async def _count_requests(request, call_next):
    """Count every request the proxy handled."""
    METRICS.inc("requests_total")
    return await call_next(request)


# ---------------------------------------------------------------------------
# Security headers — defense in depth
# ---------------------------------------------------------------------------
# Same rationale as agent_api: bearer-token JSON API, no browser surface.
# nosniff stops MIME-sniffing; no-store stops intermediate caches from
# holding bearer-token responses.
@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


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

    command: str = Field(
        ...,
        min_length=1,
        max_length=1_000_000,
        description="Shell command. Persistent state preserved across calls within same session_id.",
    )
    session_id: str = Field(
        default=DEFAULT_SESSION_ID, max_length=128, description="Session ID for persistent shell."
    )


@app.get("/healthz")
async def health(_: str = Depends(_require_token)) -> dict[str, str]:
    """Liveness probe (auth required to avoid leaking server info to anonymous probers)."""
    return {"status": "ok"}


@app.get("/metrics", operation_id="metrics")
async def metrics(_: str = Depends(_require_token)) -> Response:
    """Prometheus exposition.

    Authenticated like every other endpoint: the counters are harmless,
    but an unauthenticated endpoint on a published port is a habit
    worth not having. Prometheus reads the token from a file
    (``bearer_token_file``) — see ``docker-compose.monitoring.yml``.
    """
    return Response(
        content=METRICS.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/api/config")
async def api_config(_: str = Depends(_require_token)) -> dict[str, Any]:
    """Open WebUI capability probe. Returning ``features.system=False`` disables
    the (unused) system-prompt endpoint, keeping the tool surface minimal."""
    return {
        "name": "sandbox-agent",
        "version": __version__,
        "features": {"system": False},
    }


async def _do_ssh(client: httpx.AsyncClient, rest: str, session_id: str) -> dict[str, Any]:
    """Validate the SSH args, then forward to the sandbox."""
    # ``rest`` is everything after the ``ssh`` prefix that triggered
    # the shortcut. Strip a leading ``ssh`` token if the LLM passed it.
    rest = rest.strip()
    parts = rest.split(maxsplit=1)
    if parts and parts[0] == "ssh":
        rest = parts[1] if len(parts) > 1 else ""
    host, safe_argv = _validate_ssh_args(rest)
    if host is None:
        # safe_argv carries the error message in this case.
        logger.warning("ssh.denied reason=%s", safe_argv)
        return {"error": safe_argv or "ssh validation failed"}
    cmd = f"ssh -o StrictHostKeyChecking=accept-new {safe_argv}"
    try:
        r = await client.post(
            f"{SANDBOX_URL}/sessions/{session_id}/exec",
            json={"command": cmd, "timeout": AGENT_EXEC_TIMEOUT},
            headers=_upstream_headers(),
        )
        r.raise_for_status()
        return r.json()
    except httpx.TimeoutException as exc:
        METRICS.inc("exec_timeouts_total")
        return {"error": _upstream_timeout_reason(exc)}
    except httpx.HTTPError as exc:
        METRICS.inc("upstream_errors_total")
        return {"error": f"sandbox-agent request failed: {exc!r}"}


async def _do_exec(client: httpx.AsyncClient, command: str, session_id: str) -> dict[str, Any]:
    """Forward a command to sandbox-agent and return its JSON response."""
    try:
        r = await client.post(
            f"{SANDBOX_URL}/sessions/{session_id}/exec",
            json={"command": command, "timeout": AGENT_EXEC_TIMEOUT},
            headers=_upstream_headers(),
        )
        r.raise_for_status()
        return r.json()
    except httpx.TimeoutException as exc:
        METRICS.inc("exec_timeouts_total")
        return {"error": _upstream_timeout_reason(exc)}
    except httpx.HTTPError as exc:
        METRICS.inc("upstream_errors_total")
        return {"error": f"sandbox-agent request failed: {exc!r}"}


def _upstream_timeout_reason(exc: httpx.TimeoutException) -> str:
    """Describe a timeout towards the agent in words.

    A timeout must say so. The bare ``str()`` of an httpx timeout is
    empty, and ``sandbox-agent request failed: `` with nothing after it
    sends the operator to the network instead of to the clock. The
    numbers are in the message for the same reason: the caller has to be
    able to tell "the command ran out of time" from "the proxy ran out
    of patience".
    """
    return (
        f"sandbox-agent request timed out after {int(PROXY_REQUEST_TIMEOUT_SECONDS)}s "
        f"(exec limit {AGENT_EXEC_TIMEOUT}s): {exc!r}"
    )


def _mcp_relay_timeout_reason(exc: httpx.TimeoutException) -> str:
    """Describe a timeout of the MCP relay towards the agent in words.

    The relay waits an hour — the agent's exec limit plus a little — so
    a timeout here is a real path. The bare ``str()`` of an httpx timeout
    is empty, and ``sandbox-agent request failed: `` with nothing after
    it is indistinguishable from "the agent is broken". The two horizons
    are in the message because the caller has to be able to tell "the
    command ran out of time" from "the relay ran out of patience".
    """
    return (
        f"sandbox-agent request timed out after {int(MCP_RELAY_TIMEOUT_SECONDS)}s "
        f"(the relay's patience; the agent's exec limit is {AGENT_EXEC_TIMEOUT}s): {exc!r}"
    )


def _file_relay_timeout_reason(exc: httpx.TimeoutException) -> str:
    """Describe a timeout of the file door towards the agent in words.

    The same reasoning as the MCP relay: the bare ``str()`` of an httpx
    timeout is empty, and an empty reason sends the operator to the
    network instead of to the clock.
    """
    return (
        f"sandbox-agent request timed out after {int(PROXY_REQUEST_TIMEOUT_SECONDS)}s "
        f"(the proxy's patience): {exc!r}"
    )


def _upstream_headers() -> dict[str, str]:
    """Return the auth headers we forward to the upstream sandbox-agent.

    If ``OPENAPI_PROXY_UPSTREAM_TOKEN`` is set, use it. Otherwise fall
    back to the same ``OPENAPI_PROXY_TOKEN`` we use for incoming auth —
    that's the typical single-trust-boundary deployment.

    An **empty** value is not a value: ``os.environ.get`` with a default
    does not fall back on an empty string, so a compose file that passes
    ``OPENAPI_PROXY_UPSTREAM_TOKEN=`` (unset in the operator's env file)
    would otherwise forward **no** Authorization header at all — and the
    agent would answer every call with a 401 that says nothing about the
    cause. An empty setting therefore means "not set", exactly as it
    does in the shell.
    """
    token = os.environ.get("OPENAPI_PROXY_UPSTREAM_TOKEN") or PROXY_TOKEN
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


async def run_bash(
    client: httpx.AsyncClient,
    command: str,
    session_id: str = DEFAULT_SESSION_ID,
) -> dict[str, Any]:
    """Top-level command dispatcher.

    Recognises the ``ssh <rest>`` shortcut; everything else is forwarded
    verbatim to the sandbox.
    """
    stripped = command.strip()
    if stripped.startswith(SSH_PREFIX):
        rest = stripped[len(SSH_PREFIX) :]
        return await _do_ssh(client, rest, session_id)
    return await _do_exec(client, command, session_id)


@app.post("/bash", operation_id="bash", dependencies=[Depends(_rate_limit)])
async def bash(req: BashReq, request: Request, _: str = Depends(_require_token)) -> dict[str, Any]:
    """Run a shell command in the persistent sandbox. Single tool, full power."""
    client = _client(request)
    logger.info("bash sid=%s cmd_len=%d", req.session_id, len(req.command))
    return await run_bash(client, req.command, req.session_id)


@app.post("/shell_exec", operation_id="shell_exec", dependencies=[Depends(_rate_limit)])
async def shell_exec(
    req: BashReq, request: Request, _: str = Depends(_require_token)
) -> dict[str, Any]:
    """Alias for :func:`bash`, kept for compatibility with older Open WebUI
    tool-server configurations that look for ``shell_exec`` instead of ``bash``."""
    client = _client(request)
    return await run_bash(client, req.command, req.session_id)


# ---------------------------------------------------------------------------
# The MCP door
# ---------------------------------------------------------------------------
# The sandbox speaks MCP as well as OpenAPI, and the proxy is the
# published surface — so the MCP endpoint is relayed here too, and a
# caller that holds the proxy's token can talk to the sandbox in
# whichever protocol it speaks.
#
# The relay is a straight pass-through: the body, the method and the
# headers that matter for the transport go upstream unchanged, and the
# answer comes back as it arrives. The MCP transport is stateless (the
# server keeps no session), so there is no session id to carry and no
# affinity to keep — the endpoint can sit behind any reverse proxy.
#: The headers the relay forwards. ``host`` and ``origin`` are in the
#: list on purpose: the agent's MCP endpoint refuses a request whose
#: Host is not one it knows (the protection against DNS rebinding), and
#: the host the *caller* used is the one that has to be checked. If the
#: proxy rewrote the Host to its own idea of the agent's address, that
#: check would pass for every caller and stop protecting anything.
_MCP_REQUEST_HEADERS = (
    "accept",
    "content-type",
    "host",
    "origin",
    "mcp-protocol-version",
    "mcp-session-id",
)
_MCP_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "cache-control")


@app.api_route(
    "/mcp",
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
    # The same limit the exec door has. The MCP door is the one a model
    # talks through, and an unbounded tool-call loop is exactly what the
    # limiter was written for.
    dependencies=[Depends(_rate_limit)],
)
async def mcp_endpoint(
    request: Request,
    _: str = Depends(_require_token),
) -> Response:
    """Relay the MCP endpoint to the sandbox agent.

    The bearer token of the *proxy* is the caller's ticket; the agent
    is called with the upstream token. The two are the same value in the
    usual single-trust-boundary deployment, and separate values when
    the operator wants them separate.

    Only POST is relayed. The other two methods of the Streamable HTTP
    transport have nothing to carry here, and one of them is expensive:

    * ``DELETE`` ends an MCP session. The transport is stateless, so
      there is no session to end — the agent answers 405 itself.
    * ``GET`` opens the event stream. Relaying it would hold a
      connection out of the pool for as long as the client keeps it
      (the relay waits an hour), and this server has no
      server-initiated messages to deliver over it: the four tools are
      request and response. The SDK's own client (mcp 2.3.0) never
      opens the stream — measured — so the refusal costs nothing.

    Both refusals happen here, before the upstream is called: a
    refused method must not take a connection out of the pool.
    """
    if request.method != "POST":
        raise HTTPException(
            status_code=405,
            detail=(
                f"{request.method} /mcp is not relayed: the MCP door is "
                "request and response (POST). The transport is stateless, so "
                "there is no session to terminate, and the event stream has "
                "no server-initiated messages to carry."
            ),
            headers={"Allow": "POST"},
        )

    client = _client(request)
    # The header names are canonicalised: the request's own headers are
    # lower-cased by the framework, and a reader of the log (or of a
    # test) should not have to know that.
    headers = {
        key.title(): value
        for key, value in request.headers.items()
        if key.lower() in _MCP_REQUEST_HEADERS
    }
    headers.update(_upstream_headers())
    # httpx would set the Host from the target URL — the agent's own
    # address inside the network. The caller's Host is the one the
    # agent has to check, so it is put back.
    if "Host" not in headers:
        host = request.headers.get("host")
        if host:
            headers["Host"] = host

    body = await request.body()
    try:
        upstream = await client.send(
            client.build_request(
                request.method,
                f"{SANDBOX_URL}/mcp",
                content=body,
                headers=headers,
                timeout=MCP_RELAY_TIMEOUT_SECONDS,
            ),
            stream=True,
        )
    except httpx.TimeoutException as exc:
        # A timeout must say so. The relay waits an hour, so this is a
        # real path and not a theory — and the bare ``str()`` of an httpx
        # timeout is empty, which would send the operator to the network
        # instead of to the clock.
        METRICS.inc("exec_timeouts_total")
        raise HTTPException(status_code=502, detail=_mcp_relay_timeout_reason(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"sandbox-agent request failed: {exc!r}"
        ) from exc

    async def _relay():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() in _MCP_RESPONSE_HEADERS
    }
    return StreamingResponse(
        _relay(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


# ---------------------------------------------------------------------------
# Binary transfers
# ---------------------------------------------------------------------------
# The proxy is the only published surface, so a file has to come through
# here. Two rules make that safe:
#
#   * it is streamed, never buffered. The proxy container has a 512 MB
#     memory limit; a proxy that read the body into memory would turn a
#     100 MB file into an OOM kill.
#   * it is not rate-limited. A file of any size may arrive in ranges, and
#     a per-minute budget on requests would make a large transfer fail for
#     a reason that has nothing to do with the file.

_FILE_RESPONSE_HEADERS = (
    "accept-ranges",
    "content-length",
    "content-range",
    "content-disposition",
    "content-type",
    "x-file-name",
    "x-file-size",
)


def _upstream_detail(body: bytes) -> str:
    """The upstream's error message, unwrapped from its JSON envelope.

    The sandbox-agent answers an error as ``{"detail": "..."}``. Passing
    that whole document on as the proxy's own detail would render it as
    a JSON *string* inside a JSON document — ``{"detail": "{\\"detail\\":
    ...}"}`` — which is technically faithful and practically unreadable.
    The message inside is what the caller needs: the real size of a file
    that is over the limit, for instance.
    """
    text = body[:500].decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(parsed, dict) and isinstance(parsed.get("detail"), str):
        return parsed["detail"]
    return text


@app.get("/files/meta", operation_id="file_meta")
async def file_meta(
    request: Request,
    path: str = Query(..., description="File in the sandbox"),
    _: str = Depends(_require_token),
) -> dict[str, Any]:
    """Metadata for a file in the sandbox: size, name, mtime.

    The status is relayed, exactly as ``/files`` and ``DELETE /files`` do:
    a file that is not there stays a 404, a path outside the workspace
    stays a 400, and an unreachable agent is a 502. Flattening those into
    a 200 with an ``error`` key would be a lie to the client — the courier
    asks this endpoint for the size of a file it is about to pull, and a
    200 says "here is the file" even when there is none.
    """
    client = _client(request)
    try:
        response = await client.get(
            f"{SANDBOX_URL}/files/meta",
            params={"path": path},
            headers=_upstream_headers(),
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"sandbox-agent request failed: {exc}") from exc

    if response.status_code >= 400:
        body = await response.aread()
        await response.aclose()
        raise HTTPException(
            status_code=response.status_code, detail=_upstream_detail(body)
        ) from None

    return response.json()


@app.get("/files", operation_id="file")
async def download_file(
    request: Request,
    path: str = Query(..., description="File in the sandbox"),
    _: str = Depends(_require_token),
) -> Response:
    """Stream a file out of the sandbox, byte for byte.

    The upstream response is relayed as it arrives: the proxy never holds
    the whole file, so its memory use does not depend on the file's size.
    A ``Range`` header from the client is forwarded, which is how a file
    larger than one request is pulled.
    """
    client = _client(request)
    upstream_headers = _upstream_headers()
    range_header = request.headers.get("range")
    if range_header:
        upstream_headers["Range"] = range_header

    try:
        upstream = await client.send(
            client.build_request(
                "GET",
                f"{SANDBOX_URL}/files",
                params={"path": path},
                headers=upstream_headers,
            ),
            stream=True,
        )
    except httpx.TimeoutException as exc:
        METRICS.inc("upstream_errors_total")
        raise HTTPException(status_code=502, detail=_file_relay_timeout_reason(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"sandbox-agent request failed: {exc!r}"
        ) from exc

    if upstream.status_code >= 400:
        body = await upstream.aread()
        await upstream.aclose()
        raise HTTPException(
            status_code=upstream.status_code, detail=_upstream_detail(body)
        ) from None

    async def _relay():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() in _FILE_RESPONSE_HEADERS
    }
    return StreamingResponse(
        _relay(),
        status_code=upstream.status_code,
        headers=headers,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
    )


@app.delete("/files", operation_id="file_delete")
async def delete_file(
    request: Request,
    path: str = Query(..., description="File in the sandbox"),
    _: str = Depends(_require_token),
) -> Response:
    """Ask the sandbox to delete a file.

    The sandbox is a scratchpad, not storage: once a client has pulled a
    file out and delivered it, the artifact would otherwise live in two
    places at once. This is how the sandbox copy is removed.

    The upstream status is relayed as it is — a file that is not there
    stays a 404, and an unreachable agent is a 502. Flattening either into
    a 500 would tell the client "the sandbox is broken" when the answer
    is a plain "no".
    """
    client = _client(request)
    try:
        upstream = await client.delete(
            f"{SANDBOX_URL}/files",
            params={"path": path},
            headers=_upstream_headers(),
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"sandbox-agent request failed: {exc}") from exc

    if upstream.status_code >= 400:
        body = await upstream.aread()
        await upstream.aclose()
        raise HTTPException(
            status_code=upstream.status_code, detail=_upstream_detail(body)
        ) from None

    return Response(status_code=upstream.status_code)


# ---------------------------------------------------------------------------
# WebSocket terminal
# ---------------------------------------------------------------------------
# Experimental terminal endpoint. The LLM/UI sees a live shell; the proxy
# polls sandbox-agent's /capture endpoint (not /exec — that would spawn a
# shell per poll, which leaks file descriptors and burns CPU).


async def _stop_the_reader(reader: asyncio.Task) -> None:
    """Cancel the reader task and wait for it — without letting go of the cancellation.

    The websocket handler has a reader task; when the connection ends,
    the reader is cancelled and awaited. Awaiting a cancelled task
    raises ``CancelledError``, and ``CancelledError`` is **not** an
    ``Exception`` (it is a ``BaseException`` since Python 3.8) — so the
    bare ``except Exception`` would not catch it, it would walk out of
    the handler's ``finally`` and kill the terminal instead of closing
    it.

    The tuple looks redundant and is not. A linter that calls it
    redundant is wrong about this Python, and the review that called it
    redundant was wrong about it too: the change was made, and the
    terminal died.

    This lives in its own function so that the property can be *tested*
    — the inline version could only be caught by a race, and a race
    catches a mutation only when the wind blows the right way.
    """
    reader.cancel()
    try:
        await reader
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Bidirectional terminal. Per-connection session_id prevents cross-user
    output leakage. Reads use the dedicated /capture endpoint instead of
    a shell wrapper so the polling loop is cheap."""
    if not PROXY_TOKEN or is_placeholder(PROXY_TOKEN):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="proxy not configured")
        return

    # Auth: token comes via the ``Sec-WebSocket-Protocol`` subprotocol
    # header. We deliberately do NOT accept ``?token=…`` query params
    # because every reverse proxy (nginx, Cloudflare, ALB) logs the
    # full URL — that's a token leak by default.
    token = ""
    for proto in websocket.headers.get("sec-websocket-protocol", "").split(","):
        proto = proto.strip()
        if proto.startswith("bearer."):
            token = proto[len("bearer.") :]
            break
    if not token or not hmac.compare_digest(token, PROXY_TOKEN):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="unauthorized")
        return

    await websocket.accept()
    # Unique session per connection — no cross-user leaks.
    session_id = f"ws-{uuid.uuid4().hex[:12]}"
    await websocket.send_text(f"$ sandbox-agent terminal session={session_id}\r\n")

    client = websocket.app.state.client
    upstream = _upstream_headers()
    try:
        # Make sure the tmux session exists. Failures here are non-fatal.
        await client.post(
            f"{SANDBOX_URL}/sessions",
            json={"session_id": session_id, "cwd": "/workspace"},
            headers=upstream,
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
                    headers=upstream,
                )
                r.raise_for_status()
                output = r.json().get("output", "")
                if output != last_output:
                    new = output[len(last_output) :] if output.startswith(last_output) else output
                    if new:
                        await websocket.send_text(new)
                    last_output = output
            except httpx.HTTPError:
                break
            await asyncio.sleep(WEBSOCKET_POLL_INTERVAL)

    reader = asyncio.create_task(read_from_tmux())
    try:
        # Idle-timeout (sandbox-recommendations #M3). We use
        # ``asyncio.wait_for`` around ``receive_text`` rather than a
        # separate timer task so the cancel path is a single try/finally
        # and we don't have to coordinate two stop conditions. Setting
        # ``WEBSOCKET_IDLE_TIMEOUT_SECONDS=0`` disables the timeout
        # entirely (for long-running interactive sessions like ``top``).
        while True:
            if WEBSOCKET_IDLE_TIMEOUT_SECONDS > 0:
                try:
                    data = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=WEBSOCKET_IDLE_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    await websocket.close(
                        code=status.WS_1008_POLICY_VIOLATION,
                        reason="idle timeout",
                    )
                    break
            else:
                data = await websocket.receive_text()
            try:
                await client.post(
                    f"{SANDBOX_URL}/sessions/{session_id}/exec",
                    json={"command": data, "timeout": 5},
                    headers=upstream,
                )
            except httpx.HTTPError:
                # Drop the connection if the backend is unreachable.
                break
    except WebSocketDisconnect:
        pass
    finally:
        stop.set()
        await _stop_the_reader(reader)
        # Best-effort cleanup of the per-connection tmux session.
        try:
            await client.delete(
                f"{SANDBOX_URL}/sessions/{session_id}",
                headers=upstream,
            )
        except httpx.HTTPError:
            pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "openapi_proxy:app",
        host="0.0.0.0",  # noqa: S104 - dev server binds all interfaces (same as FastAPI docs)
        port=int(os.environ.get("OPENAPI_PROXY_PORT", "8090")),
        log_level=os.environ.get("OPENAPI_PROXY_LOG_LEVEL", "info").lower(),
        timeout_graceful_shutdown=25,
    )
