"""Pure-function core of the sandbox agent.

Separated from :mod:`agent_api` so it can be unit-tested without pulling in
FastAPI, Pydantic, or tmux. Anything security-relevant (session-id
sanitization, naming conventions) lives here.
"""

from __future__ import annotations

import re

__version__ = "2.9.1"

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


def parse_capability_status(text: str) -> tuple[int, int]:
    """Read ``NoNewPrivs`` and ``CapBnd`` out of a ``/proc/<pid>/status`` blob.

    Returns ``(no_new_privs, cap_bnd)`` — the flag as 0/1 and the
    capability bounding set as a bitmask.

    A field that is missing or unreadable counts as the *safe* value:
    the flag as on and the bounding set as empty. A status file that
    cannot be read must never be read as "escalation would work".
    """
    no_new_privs = 1
    cap_bnd = 0
    for line in text.splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        if key == "NoNewPrivs":
            try:
                no_new_privs = int(value)
            except ValueError:
                no_new_privs = 1
        elif key == "CapBnd":
            try:
                cap_bnd = int(value, 16)
            except ValueError:
                cap_bnd = 0
    return no_new_privs, cap_bnd


def sudo_posture(no_new_privs: int, cap_bnd: int) -> str:
    """Would ``sudo su`` actually work in this container?

    Two container settings decide it, and neither is visible in the
    image:

    * ``no-new-privileges`` makes the kernel ignore the setuid bit, so
      the ``sudo`` binary runs as its caller and cannot elevate;
    * an empty capability bounding set means the process can never hold
      ``CAP_SETUID``, so ``su`` cannot change the user at all.

    Returns ``"ok"`` when the escalation would work, or a short reason
    why it would not.
    """
    if no_new_privs:
        return "no-new-privileges is on: the kernel ignores the setuid bit, so sudo cannot elevate"
    if cap_bnd == 0:
        return (
            "the capability bounding set is empty: the process can never "
            "hold CAP_SETUID, so su cannot change the user"
        )
    return "ok"
