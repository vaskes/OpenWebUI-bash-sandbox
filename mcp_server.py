"""The MCP face of the sandbox.

The sandbox already speaks one protocol — a small OpenAPI surface that
the proxy relays to Open WebUI. That is not enough any more: the same
sandbox has to serve a telegram harness, an Open WebUI instance and a
llama.cpp-style caller, and each of them speaks a different protocol.
Re-implementing the sandbox per protocol is how the copies drift apart,
so this module is a **face**, not a second implementation: every tool
calls the same functions the HTTP API calls, in the same process, with
the same containment checks.

What the tools are:

* ``bash`` — run a shell command in the workspace and return its output;
* ``read_file`` — read a file out of the workspace (text as text, binary
  as a base64 blob, which is how MCP carries bytes);
* ``file_info`` — the cheap door: name, size, mime, mtime;
* ``delete_file`` — remove a file from the workspace.

What the tools are **not**: a wider sandbox. The MCP door grants exactly
what the HTTP door grants, and the bearer token is the same gate. A
caller that can talk MCP can do what a caller that can talk HTTP can
do — no more.

Auth: the same bearer token as every other endpoint, checked by the
same constant-time comparison. The check lives in this module only as a
*callable* — the agent passes its own checker in — so that the rule
"one token, one comparison" has exactly one implementation.

The transport is MCP's Streamable HTTP, in its stateless flavour: every
request is independent, the server keeps no session, and a restart in
the middle of a conversation costs the client nothing but a reconnect.
That also means the endpoint can sit behind the proxy (or any other
reverse proxy) without session affinity.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import mimetypes
import os
from collections.abc import AsyncIterator, Callable
from typing import TypedDict

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, TextContent
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("sandbox-mcp")

#: The name the server announces in the handshake. It is the name of
#: the project, because that is what a caller shows its user.
SERVER_NAME = "openwebui-bash-sandbox"

#: The session a caller gets when it does not ask for one. Named so that
#: it is obvious in the log which session a command ran in.
DEFAULT_SESSION_ID = "mcp"

#: How much of a file a single ``read_file`` call may return. The same
#: policy as the HTTP door: 0 means no limit. The value comes from the
#: agent's own configuration, so the two doors cannot disagree.
BINARY_LIMIT_BYTES = 0

#: The hard ceiling for one MCP read, independent of the configured limit.
#:
#: The HTTP door streams a file out in chunks and never holds it whole, so
#: its limit is a policy about what may be *served*. The MCP door cannot
#: stream: a tool result is one JSON-RPC message, so the file is held in
#: memory and then base64-encoded (a third more). With the configured
#: limit set to "no limit" — a legal setting — one call could therefore
#: ask the machine for everything it has. This ceiling is the answer to
#: that: the configured limit still decides the policy, and this decides
#: how much one call may cost the machine. It is above the limit the
#: HTTP door is normally run with, so it does not silently narrow it.
MCP_MAX_READ_BYTES = 64 * 1024 * 1024

#: Hosts the MCP endpoint answers for **when the check is on**. The MCP
#: SDK refuses a request whose ``Host`` header is not on this list — the
#: protection against DNS rebinding, where a web page in the operator's
#: browser talks to a service on the machine.
#:
#: The check is **off by default**, and that is the SDK's own default
#: for a server that is not bound to the loopback: a containerised
#: service is reached by its service name, and a list that does not
#: contain that name refuses every legitimate caller — including the
#: proxy, which is the published surface. A footgun in the primary use
#: case buys very little: the endpoint needs the bearer token, and a
#: page in a browser cannot read the token.
#:
#: Set ``AGENT_MCP_ALLOWED_HOSTS`` to turn the check on and to name the
#: hosts the endpoint answers for. A deployment that publishes the
#: endpoint on a name the operator controls should turn it on.
DEFAULT_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")

#: The token check. The agent passes its own in; the module never reads
#: the token itself, so there is exactly one comparison in the service.
TokenChecker = Callable[[str | None], bool]


class BashResult(TypedDict):
    """What a command produced."""

    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    session_id: str
    cwd: str


class FileInfo(TypedDict):
    """What a file is."""

    name: str
    path: str
    size: int
    mtime: int
    mime: str
    limit: int


class DeleteResult(TypedDict):
    """What the purge did."""

    deleted: bool
    path: str
    size: int


mcp = MCPServer(
    name=SERVER_NAME,
    version="0.0.0",  # replaced below, once the agent's version is known
    instructions=(
        "A shell and a workspace, over the Model Context Protocol.\n\n"
        "Use `bash` to run a command and read its output. Use `read_file` to "
        "take a file out of the workspace, `file_info` to look at one without "
        "reading it, and `delete_file` to remove one. Every path is inside the "
        "sandbox workspace; a path that points outside is refused, and so is a "
        "symlink that leaves the workspace.\n\n"
        "The shell is the write path: there is no separate write tool, and a "
        "command that writes a file is the way to write one."
    ),
)


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "Run a shell command in the sandbox workspace and return its output. "
        "The command runs in a persistent session, so `cd` and `export` survive "
        "from one call to the next. Use `session_id` to keep two conversations "
        "apart; use `cwd` to choose the working directory (it must be inside "
        "the workspace)."
    )
)
async def bash(
    command: str,
    session_id: str | None = None,
    cwd: str | None = None,
    timeout: int | None = None,
) -> BashResult:
    """Run a command and return what it printed."""
    from agent_api import (  # noqa: PLC0415 - the module is mounted, not imported
        SESSIONS,
        WORKSPACE_REAL,
        CreateSession,
        ExecRequest,
        create_session,
        exec_cmd,
    )
    from agent_core import safe_id  # noqa: PLC0415

    sid = safe_id(session_id or DEFAULT_SESSION_ID)
    if sid not in SESSIONS:
        try:
            await create_session(CreateSession(session_id=sid, cwd=cwd or WORKSPACE_REAL))
        except Exception as exc:  # noqa: BLE001 - the detail must reach the model
            raise ToolError(f"cannot start the session: {_reason(exc)}") from exc

    request = ExecRequest(
        command=command,
        timeout=timeout if timeout is not None else _default_timeout(),
    )
    try:
        result = await exec_cmd(sid, request)
    except Exception as exc:  # noqa: BLE001 - the detail must reach the model
        raise ToolError(f"the command did not run: {_reason(exc)}") from exc

    return BashResult(
        stdout=result.get("stdout", ""),
        stderr=result.get("stderr", ""),
        exit_code=result.get("exit_code"),
        timed_out=bool(result.get("timed_out")),
        duration_ms=int(result.get("duration_ms", 0)),
        session_id=sid,
        cwd=str(SESSIONS.get(sid, {}).get("cwd", "")),
    )


@mcp.tool(
    description=(
        "Read a file from the sandbox workspace. A text file comes back as "
        "text; anything else comes back as a base64 blob with its mime type. "
        "The size limit is the same one the HTTP door applies."
    )
)
async def read_file(path: str) -> CallToolResult:
    """Read a file out of the workspace."""
    from agent_api import (
        _open_for_download,  # noqa: PLC0415
        _safe_file,  # noqa: PLC0415
    )

    try:
        target = _safe_file(path)
    except Exception as exc:  # noqa: BLE001 - the detail must reach the model
        raise ToolError(f"cannot read the file: {_reason(exc)}") from exc

    limit = _read_limit()
    try:
        size = os.path.getsize(target)
    except OSError as exc:
        raise ToolError(f"cannot read the file: {_reason(exc)}") from exc
    if limit and size > limit:
        raise ToolError(
            f"the file is {size} bytes, which is over the limit of {limit} bytes; "
            "read it in pieces or compress it first"
        )

    try:
        handle = _open_for_download(target)
    except Exception as exc:  # noqa: BLE001 - the detail must reach the model
        raise ToolError(f"cannot read the file: {_reason(exc)}") from exc
    with handle:
        # Read one byte more than the limit allows: a file that grew
        # between the stat and the read is caught here instead of being
        # read whole, and the call is refused with the reason. The
        # limit is never zero — the ceiling is always in force — so
        # there is no "read it all" branch to fall into.
        payload = handle.read(limit + 1)
    if len(payload) > limit:
        raise ToolError(
            f"the file grew past the limit of {limit} bytes while it was being read; "
            "read it in pieces or compress it first"
        )

    mime = mimetypes.guess_type(target)[0] or "application/octet-stream"
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = None

    if text is not None:
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content={"name": os.path.basename(target), "size": size, "mime": mime},
        )
    return CallToolResult(
        content=[
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri=f"file://{os.path.basename(target)}",
                    mimeType=mime,
                    blob=base64.b64encode(payload).decode("ascii"),
                ),
            )
        ],
        structured_content={"name": os.path.basename(target), "size": size, "mime": mime},
    )


@mcp.tool(
    description=(
        "Look at a file in the sandbox workspace without reading it: its name, "
        "size, mime type and modification time."
    )
)
async def file_info(path: str) -> FileInfo:
    """Return the metadata of a file."""
    from agent_api import _safe_file  # noqa: PLC0415

    try:
        target = _safe_file(path)
    except Exception as exc:  # noqa: BLE001 - the detail must reach the model
        raise ToolError(f"cannot stat the file: {_reason(exc)}") from exc

    stat = os.stat(target)
    guessed, _ = mimetypes.guess_type(target)
    return FileInfo(
        name=os.path.basename(target),
        path=target,
        size=stat.st_size,
        mtime=int(stat.st_mtime),
        mime=guessed or "application/octet-stream",
        limit=_binary_limit(),
    )


@mcp.tool(
    description=(
        "Delete a file from the sandbox workspace. The workspace is a "
        "scratchpad: the file is gone for good."
    )
)
async def delete_file(path: str) -> DeleteResult:
    """Remove a file from the workspace."""
    from agent_api import _safe_file  # noqa: PLC0415

    try:
        target = _safe_file(path)
    except Exception as exc:  # noqa: BLE001 - the detail must reach the model
        raise ToolError(f"cannot delete the file: {_reason(exc)}") from exc

    size = os.path.getsize(target)
    try:
        os.remove(target)
    except OSError as exc:
        raise ToolError(f"cannot delete the file: {exc}") from exc
    return DeleteResult(deleted=True, path=target, size=size)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _reason(exc: Exception) -> str:
    """Turn an exception into a sentence a model can act on.

    The HTTP layer raises ``HTTPException`` with a detail that was
    written for a human reading a status line. Over MCP the detail
    travels to the model, so it has to read as a sentence: "path outside
    the workspace: /etc/passwd", not "400: path outside the workspace:
    /etc/passwd".
    """
    detail = getattr(exc, "detail", None)
    if detail:
        return str(detail)
    return f"{type(exc).__name__}: {exc}"


def _binary_limit() -> int:
    """The size limit the HTTP door applies, read from the agent's config."""
    from agent_api import BINARY_LIMIT_BYTES  # noqa: PLC0415

    return int(BINARY_LIMIT_BYTES)


def _read_limit() -> int:
    """How many bytes one MCP read may return.

    The configured limit is the policy; the ceiling is the guard. With
    the limit set to 0 ("no limit"), the ceiling is what applies — see
    :data:`MCP_MAX_READ_BYTES` for why a read needs one.
    """
    limit = _binary_limit()
    if limit and limit <= MCP_MAX_READ_BYTES:
        return limit
    return MCP_MAX_READ_BYTES


def _default_timeout() -> int:
    """The command timeout the HTTP door applies by default."""
    from agent_api import DEFAULT_TIMEOUT  # noqa: PLC0415

    return int(DEFAULT_TIMEOUT)


def allowed_hosts() -> list[str]:
    """The hosts the MCP endpoint answers for, or an empty list.

    ``AGENT_MCP_ALLOWED_HOSTS`` is a comma-separated list of ``Host``
    header values. A pattern may end in ``:*`` to allow any port. An
    empty value means "no check" — see :data:`DEFAULT_ALLOWED_HOSTS`.
    """
    raw = os.environ.get("AGENT_MCP_ALLOWED_HOSTS", "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def transport_security() -> TransportSecuritySettings:
    """The DNS-rebinding protection for the MCP endpoint.

    On when ``AGENT_MCP_ALLOWED_HOSTS`` names the hosts, off when it
    does not. The reasons are in :data:`DEFAULT_ALLOWED_HOSTS`.
    """
    hosts = allowed_hosts()
    if not hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    origins = []
    for host in hosts:
        base = host[:-2] if host.endswith(":*") else host
        origins.append(f"http://{base}:*")
        origins.append(f"https://{base}:*")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def rebinding_protection_is_on() -> bool:
    """True when the endpoint checks the ``Host`` header."""
    return bool(allowed_hosts())


# ---------------------------------------------------------------------------
# The ASGI app
# ---------------------------------------------------------------------------
class _BearerGate:
    """Refuse a request that carries no valid bearer token.

    The MCP transport carries its own authentication story (OAuth, in
    the general case) and this server does not use it: the sandbox has
    one token and one trust boundary, and the token is the same one the
    HTTP door uses. A caller without it gets a 401 with a body that says
    what is missing — the same wording as the HTTP door, so an operator
    reads one message in both places.
    """

    def __init__(self, app: ASGIApp, token_ok: TokenChecker) -> None:
        self.app = app
        self.token_ok = token_ok

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            authorization = headers.get("authorization")
            scheme, _, credentials = (authorization or "").partition(" ")
            if scheme.lower() != "bearer" or not self.token_ok(credentials.strip()):
                body = json.dumps({"detail": "missing bearer token"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"www-authenticate", b"Bearer"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_app(token_ok: TokenChecker) -> ASGIApp:
    """Build the ASGI app that serves the MCP endpoint.

    The endpoint is stateless Streamable HTTP: every request stands on
    its own, the server keeps no session, and the answers are JSON. A
    stateless endpoint is what makes the door proxyable — no session
    affinity, no state to lose on a restart.
    """
    inner = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=transport_security(),
    )
    return _BearerGate(inner, token_ok)


@contextlib.asynccontextmanager
async def lifespan() -> AsyncIterator[None]:
    """Run the MCP session manager for as long as the agent runs.

    The session manager owns the task group the transport runs in, and
    it refuses requests until ``run()`` has been entered. A mounted
    sub-application does not get a lifespan of its own, so the agent
    enters this one.
    """
    if not rebinding_protection_is_on():
        # Said out loud, once, at startup: the operator should know that
        # the endpoint does not check the Host header, and why that is
        # the default rather than an oversight.
        logger.warning(
            "mcp.host_check_off — the MCP endpoint does not check the Host "
            "header (AGENT_MCP_ALLOWED_HOSTS is empty). The bearer token is "
            "the guard; set AGENT_MCP_ALLOWED_HOSTS to add the check."
        )
    async with mcp.session_manager.run():
        yield


def set_version(version: str) -> None:
    """Tell the server which version of the sandbox it is fronting.

    The SDK keeps the version on the low-level server; the high-level
    one only reads it. So the value is set where it lives.
    """
    mcp._lowlevel_server.version = version  # noqa: SLF001 - the SDK exposes no setter


def tool_names() -> list[str]:
    """The names of the tools this server offers (for the docs and tests)."""
    return ["bash", "read_file", "file_info", "delete_file"]


__all__ = [
    "BashResult",
    "DeleteResult",
    "FileInfo",
    "allowed_hosts",
    "bash",
    "build_app",
    "delete_file",
    "file_info",
    "lifespan",
    "mcp",
    "read_file",
    "set_version",
    "tool_names",
    "transport_security",
]
