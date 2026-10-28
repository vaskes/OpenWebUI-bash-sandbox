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
request is independent, the server keeps no session, and a restart of
the *transport* costs the client nothing but a reconnect. The sandbox
itself is stateful: the shell lives in a tmux session inside the agent,
and a restart of the agent takes those sessions with it. The
statelessness is a property of the protocol, not of the shell. That
also means the endpoint can sit behind the proxy (or any other reverse
proxy) without session affinity.
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

from agent_config import (
    MCP_ALLOWED_HOSTS,
    MCP_READ_CEILING_BYTES,
    MCP_READ_LIMIT_BYTES,
)

logger = logging.getLogger("sandbox-mcp")

#: The name the server announces in the handshake. It is the name of
#: the project, because that is what a caller shows its user.
SERVER_NAME = "openwebui-bash-sandbox"

#: The session a caller gets when it does not ask for one. Named so that
#: it is obvious in the log which session a command ran in.
DEFAULT_SESSION_ID = "mcp"

#: How much of a file a single ``read_file`` call may return is decided by
#: :func:`_read_limit`: the smallest of the model-facing budget
#: (``AGENT_MCP_MAX_READ_BYTES``), the file policy the HTTP door applies
#: (``AGENT_BINARY_LIMIT``) and the hard ceiling. All three live in
#: ``agent_config``, which is where a knob is read and validated once.

#: Hosts the MCP endpoint answers for **when the check is on**. The MCP
#: SDK refuses a request whose ``Host`` header is not on this list — the
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
        "command that writes a file is the way to write one.\n\n"
        "The shell is persistent, and a session is shared: give every task that "
        "must not see another's state its own `session_id` (a name of your "
        "choosing, not the default one), or two conversations will read each "
        "other's output."
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
        "apart; use `cwd` to choose the working directory of a session (it must "
        "be inside the workspace). A session that already exists keeps the "
        "directory it is in — asking for another one is refused, and the answer "
        "names the directory the session is really in."
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
        MAX_COMMAND_LEN,
        MAX_TIMEOUT,
        SESSIONS,
        CreateSession,
        ExecRequest,
        create_session,
        exec_cmd,
    )
    from agent_core import safe_id  # noqa: PLC0415

    if timeout is not None:
        # The tool knows the range, so the tool says it. The request
        # model would refuse an out-of-range timeout in its own words:
        # the model would read a validation error about a class it has
        # never heard of, and no range at all.
        #
        # A bad argument is a client's mistake, not a fault of the
        # server, so it is refused without a traceback — the SDK has
        # already logged the call.
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ToolError(f"the timeout must be a whole number of seconds, got {timeout!r}")
        if not 1 <= timeout <= MAX_TIMEOUT:
            raise ToolError(
                f"the timeout must be between 1 and {MAX_TIMEOUT} seconds, got {timeout}"
            )

    if not command:
        # The request model refuses an empty command, and its refusal
        # is a pydantic error: "ValidationError: ... String should have
        # at least 1 character".  The model would read a framework's
        # internals instead of its own mistake.
        raise ToolError("the command is empty — there is nothing to run")
    if len(command) > MAX_COMMAND_LEN:
        raise ToolError(
            f"the command is {len(command)} characters long, over the limit of "
            f"{MAX_COMMAND_LEN} — send it in parts"
        )

    sid = safe_id(session_id or DEFAULT_SESSION_ID)
    # The session is opened (or adopted) on every call, and not only when
    # it is missing: this is the only place where the directory of a
    # running session can be compared with the one the caller asked for.
    # A `cwd` the session cannot honour is refused here, in words, with
    # the directory the session is really in — instead of being ignored
    # and the command running somewhere the caller did not ask for.
    try:
        await create_session(CreateSession(session_id=sid, cwd=cwd))
    except Exception as exc:  # noqa: BLE001 - the detail must reach the model
        logger.exception("mcp.bash cannot use the session %s", sid)
        raise ToolError(f"cannot use the session: {_reason(exc)}") from exc

    try:
        request = ExecRequest(
            command=command,
            timeout=timeout if timeout is not None else _default_timeout(),
        )
    except Exception as exc:  # noqa: BLE001 - the detail must reach the model
        # The checks above cover the two ways this can fail today.  The
        # guard is here for the day a third way appears: a framework
        # error must not reach the model as "Error executing tool bash".
        logger.exception("mcp.bash cannot build the request")
        raise ToolError(f"the command cannot be run: {_reason(exc)}") from exc
    try:
        result = await exec_cmd(sid, request)
    except Exception as exc:  # noqa: BLE001 - the detail must reach the model
        logger.exception("mcp.bash failed in the session %s", sid)
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
        "A file over the read limit is refused, and the refusal names the "
        "file's size and the limit — use bash (head, tail, sed) to read a "
        "piece of a big file instead."
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
        logger.exception("mcp.read_file refused %s", path)
        raise ToolError(f"cannot read the file: {_reason(exc)}") from exc

    limit = _read_limit()
    try:
        size = os.path.getsize(target)
    except OSError as exc:
        logger.exception("mcp.read_file cannot stat %s", path)
        raise ToolError(f"cannot read the file: {_reason(exc)}") from exc
    if limit and size > limit:
        logger.warning(
            "mcp.read_file refused %s: %d bytes is over the read limit of %d bytes",
            path,
            size,
            limit,
        )
        raise ToolError(
            f"the file is {size} bytes, which is over the limit of {limit} bytes; "
            "read it in pieces with bash (head, tail, sed) or compress it first"
        )

    try:
        handle = _open_for_download(target)
    except Exception as exc:  # noqa: BLE001 - the detail must reach the model
        logger.exception("mcp.read_file cannot open %s", path)
        raise ToolError(f"cannot read the file: {_reason(exc)}") from exc
    with handle:
        # The size that goes into the answer is the size of the file the
        # descriptor points at, not the size of the path: between the
        # stat above and the open below, the name can have been pointed
        # at another file, and an answer whose size disagrees with its
        # own bytes is a claim nobody can check.
        size = os.fstat(handle.fileno()).st_size
        # Read one byte more than the limit allows: a file that grew
        # between the stat and the read is caught here instead of being
        # read whole, and the call is refused with the reason. The
        # limit is never zero — the ceiling is always in force — so
        # there is no "read it all" branch to fall into.
        payload = handle.read(limit + 1)
    if len(payload) > limit:
        raise ToolError(
            f"the file grew past the limit of {limit} bytes while it was being read; "
            "read it in pieces with bash (head, tail, sed) or compress it first"
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
        "size, mime type, modification time and the read limit that applies."
    )
)
async def file_info(path: str) -> FileInfo:
    """Return the metadata of a file."""
    from agent_api import _safe_file  # noqa: PLC0415

    try:
        target = _safe_file(path)
    except Exception as exc:  # noqa: BLE001 - the detail must reach the model
        logger.exception("mcp.file_info refused %s", path)
        raise ToolError(f"cannot stat the file: {_reason(exc)}") from exc

    stat = os.stat(target)
    guessed, _ = mimetypes.guess_type(target)
    return FileInfo(
        name=os.path.basename(target),
        path=target,
        size=stat.st_size,
        mtime=int(stat.st_mtime),
        mime=guessed or "application/octet-stream",
        limit=_read_limit(),
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
        logger.exception("mcp.delete_file refused %s", path)
        raise ToolError(f"cannot delete the file: {_reason(exc)}") from exc

    try:
        # The size is informational (the file is gone after this), but a
        # target that turned into a symlink between the check and the
        # removal is not removed at all: unlink would drop the link and
        # leave whatever it points at alone.
        stat = os.lstat(target)
        if os.path.islink(target):
            raise OSError("the path became a symlink while it was being deleted")
        size = stat.st_size
        os.remove(target)
    except OSError as exc:
        logger.exception("mcp.delete_file cannot remove %s", path)
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

    Three limits, and the smallest one wins:

    * the model-facing budget (``AGENT_MCP_MAX_READ_BYTES``) — how much
      one call may put into a model's context;
    * the file policy the HTTP door applies (``AGENT_BINARY_LIMIT``) — a
      limit of 0 means "no limit" and does not apply here;
    * the hard ceiling (:data:`agent_config.MCP_READ_CEILING_BYTES`) —
      the guard that stays when the policy is 0.

    The budget is the reason this function exists. The binary limit is
    about a file that travels to a client, where the bytes land in a
    file; the deployment runs it at 50 MiB on purpose. Here the bytes
    land in a prompt: the file is held in memory and base64-encoded on
    the way out, and a megabyte of text is already more than a text
    model can use. The HTTP door keeps its own limit — untouched.
    """
    limit = MCP_READ_CEILING_BYTES
    policy = _binary_limit()
    if policy:
        limit = min(limit, policy)
    budget = MCP_READ_LIMIT_BYTES
    if budget:
        limit = min(limit, budget)
    return limit


def _default_timeout() -> int:
    """The command timeout the HTTP door applies by default."""
    from agent_api import DEFAULT_TIMEOUT  # noqa: PLC0415

    return int(DEFAULT_TIMEOUT)


def allowed_hosts() -> list[str]:
    """The hosts the MCP endpoint answers for, or an empty list.

    The value comes from :mod:`agent_config` — read from the
    environment once, at start-up, like every other knob here. An empty
    list means "no check": the check is off by default, and
    :func:`transport_security` says why.
    """
    return list(MCP_ALLOWED_HOSTS)


def transport_security() -> TransportSecuritySettings:
    """The DNS-rebinding protection for the MCP endpoint.

    On when ``AGENT_MCP_ALLOWED_HOSTS`` names the hosts, off when it
    does not.

    The check is **off by default**, and that is the SDK's own default
    for a server that is not bound to the loopback: a containerised
    service is reached by its service name, and a list that does not
    contain that name refuses every legitimate caller — including the
    proxy, which is the published surface. A footgun in the primary use
    case buys very little: the endpoint needs the bearer token, and a
    page in a browser cannot read the token.

    Set ``AGENT_MCP_ALLOWED_HOSTS`` to turn the check on and to name the
    hosts the endpoint answers for. A deployment that publishes the
    endpoint on a name the operator controls should turn it on.

    (There used to be a constant here — ``DEFAULT_ALLOWED_HOSTS`` — that
    named a list nobody read. A constant that is not used is a claim
    about the default that nobody can check, and this one was wrong: the
    default is "no check", not "the loopback names".)
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
