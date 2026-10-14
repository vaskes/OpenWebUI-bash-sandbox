"""Pure-function core of the openapi-proxy.

Separated from :mod:`openapi_proxy` so it can be unit-tested without
pulling in FastAPI/Pydantic/httpx. The constants here define the public
command syntax that the LLM sees.
"""

from __future__ import annotations

__version__ = "2.4.0"

WEB_SEARCH_PREFIX = "web_search "
SSH_PREFIX = "ssh "
DEFAULT_SESSION_ID = "openwebui"
EXEC_TIMEOUT_SECONDS = 60.0
SEARXNG_TIMEOUT_SECONDS = 30.0
WEBSOCKET_POLL_INTERVAL = 0.5
WEBSOCKET_SESSION_ID = "openwebui-ws"
