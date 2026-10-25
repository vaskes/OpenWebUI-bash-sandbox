"""Pure-function core of the sandbox agent.

Separated from :mod:`agent_api` so it can be unit-tested without pulling in
FastAPI, Pydantic, or tmux. Anything security-relevant (session-id
sanitization, naming conventions) lives here.
"""

from __future__ import annotations

import re

__version__ = "2.7.1"

TMUX_PREFIX = "agent_"
SESSION_ID_MAX_LEN = 64
SESSION_ID_PATTERN = re.compile(r"[^a-zA-Z0-9_-]")


def safe_id(raw: str) -> str:
    """Sanitize a user-supplied session id.

    Restricts characters to ``[a-zA-Z0-9_-]`` and caps length at
    :data:`SESSION_ID_MAX_LEN`. Returns ``"default"`` for empty input.
    """
    cleaned = SESSION_ID_PATTERN.sub("_", raw)[:SESSION_ID_MAX_LEN]
    return cleaned or "default"


def tmux_name(session_id: str) -> str:
    """Return the tmux session name for a given session id."""
    return f"{TMUX_PREFIX}{safe_id(session_id)}"
