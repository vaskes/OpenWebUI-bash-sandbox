"""Pure-function core of the openapi-proxy.

Separated from :mod:`openapi_proxy` so it can be unit-tested without
pulling in FastAPI/Pydantic/httpx. The constants here define the public
command syntax that the LLM sees.
"""

from __future__ import annotations

from token_rules import token_problem as _token_problem

__version__ = "2.14.8"

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

    The rules live in :mod:`token_rules` so the agent and the proxy
    cannot disagree about what a usable token is — in particular about
    the template placeholder, which is a published string and not a
    secret at all.
    """
    return _token_problem(token)
