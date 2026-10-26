"""Pure-function core of the openapi-proxy.

Separated from :mod:`openapi_proxy` so it can be unit-tested without
pulling in FastAPI/Pydantic/httpx. The constants here define the public
command syntax that the LLM sees.
"""

from __future__ import annotations

__version__ = "2.10.2"

WEB_SEARCH_PREFIX = "web_search "
SSH_PREFIX = "ssh "
DEFAULT_SESSION_ID = "openwebui"
#: The default exec limit the proxy asks the sandbox-agent for, in
#: seconds. A deployment overrides it with ``AGENT_EXEC_TIMEOUT``.
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
EXEC_TIMEOUT_MIN_SECONDS = 5
EXEC_TIMEOUT_MAX_SECONDS = 600
SEARXNG_TIMEOUT_SECONDS = 30.0
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
