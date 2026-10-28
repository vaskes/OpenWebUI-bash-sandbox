"""Pure-function core of the openapi-proxy.

Separated from :mod:`openapi_proxy` so it can be unit-tested without
pulling in FastAPI/Pydantic/httpx. The constants here define the public
command syntax that the LLM sees.
"""

from __future__ import annotations

__version__ = "2.14.4"

SSH_PREFIX = "ssh "
DEFAULT_SESSION_ID = "openwebui"
#: The default exec limit the proxy asks the sandbox-agent for, in
#: seconds. A deployment overrides it with ``AGENT_EXEC_TIMEOUT``.
#:
#: 60 s is the number the operator's v2.14.0 kept on purpose: the range
#: is what says how far a deployment may raise the limit, and the default
#: is what a deployment that never touched the knob gets.  It is also the
#: number that bites when a start path does not pass the deployment's
#: env file — the compose interpolates ``${AGENT_EXEC_TIMEOUT:-60}``, so
#: a proxy started without that file silently falls back to a minute
#: while the caller still believes it may run for an hour.
EXEC_TIMEOUT_SECONDS = 60.0
#: How much longer the proxy waits for the agent than the exec limit it
#: asked for.
#:
#: The agent answers *after* the limit expires — with ``timed_out: true``
#: and the partial output — and that answer has to survive the trip. With
#: the same number on both sides the agent's answer and the proxy's
#: patience expire at the same instant, and the caller gets a transport
#: error instead of the timeout it asked for.
PROXY_WAIT_GRACE_SECONDS = 15.0
#: The accepted range for the exec limit, in seconds.
#:
#: 2026-10-27: the ceiling was 600 s, and the operator measured the
#: limit from the inside -- a command that ran for a minute was cut
#: off, and the output that had already been produced went with it.
#: His ruling: an hour. The ceiling is now 3600 s, which is what the
#: agent's own hard cap allows (see ``agent_api.MAX_TIMEOUT``), so the
#: proxy can ask for a limit the agent will actually honour.
EXEC_TIMEOUT_MIN_SECONDS = 5
EXEC_TIMEOUT_MAX_SECONDS = 3600
WEBSOCKET_POLL_INTERVAL = 0.5
WEBSOCKET_SESSION_ID = "openwebui-ws"


def parse_exec_timeout(raw: str) -> int:
    """Parse the exec limit in seconds, refusing anything out of range.

    Raises :class:`ValueError` naming the variable, the offending value
    and the allowed range. The caller is expected to let that abort
    start-up: a limit nobody can trust is worse than a container that
    refuses to boot.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"AGENT_EXEC_TIMEOUT={raw!r} is not a number of seconds") from None
    if value < EXEC_TIMEOUT_MIN_SECONDS or value > EXEC_TIMEOUT_MAX_SECONDS:
        raise ValueError(
            f"AGENT_EXEC_TIMEOUT={raw!r} is {value}s, outside the allowed range "
            f"{EXEC_TIMEOUT_MIN_SECONDS}..{EXEC_TIMEOUT_MAX_SECONDS}s"
        )
    return value


def token_problem(token: str) -> str | None:
    """Return why ``token`` cannot be used, or ``None`` when it is fine.

    The token is not only a bearer token. The compose file interpolates
    it into a JSON document — ``"key":"${OPENAPI_PROXY_TOKEN}"`` inside
    ``TOOL_SERVER_CONNECTIONS`` — and that document is parsed by Open
    WebUI. A quote or a backslash in the token turns it into something
    Open WebUI cannot parse, and the failure is quiet: the UI comes up
    without the sandbox tool and nobody is told why.

    The same token also travels in an ``Authorization`` header, where a
    newline is a header injection rather than a broken JSON.

    So the token has to be non-empty and made of printable ASCII
    without whitespace. ``openssl rand -hex 32`` satisfies all of that,
    which is why the template asks for it.
    """
    if not token:
        return "the token is empty"
    bad = sorted(
        {
            char
            for char in token
            if char.isspace() or not (0x20 <= ord(char) < 0x7F) or char in '"\\'
        }
    )
    if bad:
        shown = ", ".join(repr(char) for char in bad)
        return (
            f"the token contains {shown} — it is embedded into the Open WebUI "
            f"tool connection as JSON and travels in an HTTP header, and those "
            f"characters break one or the other. Regenerate it with: "
            f"openssl rand -hex 32"
        )
    return None
