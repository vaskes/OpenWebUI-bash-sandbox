"""Runtime configuration for the sandbox agent.

Every knob is read from the environment once, at import time, and validated
once. A value outside its documented range is a **startup failure**, not a
clamp: a typo in ``.env`` has to be loud, because a silently clamped limit is
indistinguishable from a limit that was never applied.

Two budgets live here, and they are independent axes:

* ``AGENT_STDOUT_LIMIT`` — how much **text** one command may return. The
  agent truncates what it hands back and says so in the payload.
* ``AGENT_BINARY_LIMIT`` — how large a file the **binary endpoint** will
  serve. Binary never travels through stdout, so this limit is about policy,
  not about transport.

A third budget arrived with the MCP door, and it is on a third axis
again:

* ``AGENT_MCP_MAX_READ_BYTES`` — how many bytes one MCP ``read_file`` may
  put into a **model's context**. The binary limit is about a file that
  travels to a client, where the bytes land in a file on disk; this one is
  about bytes that land in a prompt, where they cost the model's attention
  and the memory of the process that holds them. The deployment runs the
  binary limit at 50 MiB on purpose, and that limit must not become the
  price of one tool call.

The stdout budget is only meaningful if the tmux pane can actually hold it.
A pane that is smaller than the budget loses the *beginning* of the output
silently — no truncation marker, no error — so the scrollback depth is
derived from the budget here rather than left at a constant that only looks
right on paper.
"""

from __future__ import annotations

import os

__all__ = [
    "BINARY_LIMIT_BYTES",
    "CAPTURE_DEPTH_LINES",
    "HISTORY_LIMIT_LINES",
    "MCP_READ_CEILING_BYTES",
    "MCP_READ_LIMIT_BYTES",
    "MCP_READ_LIMIT_DEFAULT_BYTES",
    "PANE_HEIGHT",
    "PANE_WIDTH",
    "STDOUT_LIMIT_BYTES",
    "parse_size",
]

_KIB = 1024
_MIB = 1024 * _KIB
_GIB = 1024 * _MIB

#: The pane the sandbox drives. These must match the ``new-session`` call in
#: ``agent_api.create_session`` — the stdout budget is derived from the
#: width, because a pane 200 columns wide holds 200 bytes per line.
PANE_WIDTH = 200
PANE_HEIGHT = 50

#: Scrollback kept in reserve for what is not payload: the echoed command
#: line, the shell prompt, the agent's own completion marker, and whatever
#: the session printed before. None of it belongs to the command's output,
#: but all of it occupies lines in the pane.
HISTORY_MARGIN_LINES = 256

#: The narrowest line the stdout budget is guaranteed to cover.
#:
#: A pane line holds up to :data:`PANE_WIDTH` bytes, but real output is
#: often much narrower — an 80-column text file is the common case, and
#: the budget is counted in bytes while the pane is counted in lines. If
#: the scrollback were sized for full-width lines, a 512 KB budget would
#: only be deliverable for output wider than ~100 columns, and a plain
#: ``cat`` of a text file would lose its beginning — silently, because an
#: overfull pane drops the oldest lines without saying so.
#:
#: Sizing the scrollback for this width instead makes the budget
#: deliverable for ordinary output. A command printing lines narrower than
#: this can still overflow the pane; that case is detected and reported
#: rather than returned as a plausible-looking fragment.
MIN_LINE_WIDTH = 64

#: Accepted suffixes for a size value. Bare numbers are bytes.
_SIZE_SUFFIXES = {
    "": 1,
    "b": 1,
    "k": _KIB,
    "kb": _KIB,
    "m": _MIB,
    "mb": _MIB,
    "g": _GIB,
    "gb": _GIB,
}


def parse_size(
    raw: str,
    *,
    name: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Parse ``512k`` / ``2m`` / ``1g`` / ``1048576`` into a byte count.

    Raises :class:`ValueError` with a message that names the variable, the
    offending value and the allowed range. The caller is expected to let
    that abort start-up: a limit nobody can trust is worse than a container
    that refuses to boot.
    """
    text = str(raw).strip().lower()
    if not text:
        raise ValueError(f"{name} is empty")

    digits = ""
    index = 0
    while index < len(text) and text[index].isdigit():
        digits += text[index]
        index += 1
    suffix = text[index:].strip()

    if not digits:
        raise ValueError(f"{name}={raw!r} is not a size")
    if suffix not in _SIZE_SUFFIXES:
        raise ValueError(
            f"{name}={raw!r} has an unknown suffix {suffix!r} "
            "(use a plain byte count, or a k/m/g suffix)"
        )

    value = int(digits) * _SIZE_SUFFIXES[suffix]

    if minimum is not None and value < minimum:
        raise ValueError(f"{name}={raw!r} is {value} bytes, below the minimum of {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name}={raw!r} is {value} bytes, above the maximum of {maximum}")
    return value


#: The stdout budget is bounded on purpose. 32k is enough for a command that
#: prints a result; 1024k is the ceiling, and going above it buys nothing
#: because the client truncates far earlier anyway.
STDOUT_LIMIT_MIN_BYTES = 32 * _KIB
STDOUT_LIMIT_MAX_BYTES = 1024 * _KIB

STDOUT_LIMIT_BYTES = parse_size(
    os.environ.get("AGENT_STDOUT_LIMIT", "512k"),
    name="AGENT_STDOUT_LIMIT",
    minimum=STDOUT_LIMIT_MIN_BYTES,
    maximum=STDOUT_LIMIT_MAX_BYTES,
)

#: The binary budget has no ceiling: the endpoint streams, so the mechanism
#: is the same for 10 kB and for 10 GB. A limit of 0 means "no limit".
BINARY_LIMIT_BYTES = parse_size(
    os.environ.get("AGENT_BINARY_LIMIT", "2048k"),
    name="AGENT_BINARY_LIMIT",
    minimum=0,
)

#: The hard ceiling for one MCP read, independent of any configured limit.
#:
#: The HTTP door streams a file out in chunks and never holds it whole, so
#: its limit is a policy about what may be *served*. The MCP door cannot
#: stream: a tool result is one JSON-RPC message, so the file is held in
#: memory and then base64-encoded (a third more). With the configured
#: limit set to "no limit" - a legal setting - one call could therefore
#: ask the machine for everything it has. This ceiling is the answer to
#: that: the configured limit still decides the policy, and this decides
#: how much one call may cost the machine.
MCP_READ_CEILING_BYTES = 64 * _MIB

#: The default model-facing read budget: one mebibyte. A text model asked
#: to reason about a megabyte of text has lost the thread, and the bytes
#: are held in memory and base64-encoded on the way out. The operator can
#: raise the budget, but the default is the one that fits in a prompt.
MCP_READ_LIMIT_DEFAULT_BYTES = 1 * _MIB

#: The model-facing read budget. A limit of 0 means "no budget of its own",
#: the same convention as ``AGENT_BINARY_LIMIT=0`` ("unlimited"): the file
#: policy and the ceiling decide then. The range ends at the ceiling,
#: because a value above it would be silently clamped - and a limit that
#: is silently clamped is a limit nobody can trust.
MCP_READ_LIMIT_BYTES = parse_size(
    os.environ.get("AGENT_MCP_MAX_READ_BYTES", str(MCP_READ_LIMIT_DEFAULT_BYTES)),
    name="AGENT_MCP_MAX_READ_BYTES",
    minimum=0,
    maximum=MCP_READ_CEILING_BYTES,
)

#: Scrollback the pane must hold for the stdout budget to be deliverable.
#: One line of a 200-column pane is 200 bytes, so this is the budget
#: divided by the width, rounded up.
HISTORY_LIMIT_LINES = -(-STDOUT_LIMIT_BYTES // MIN_LINE_WIDTH) + HISTORY_MARGIN_LINES

#: How far back a capture may reach. It has to cover the whole scrollback
#: plus the visible pane, or the capture truncates what the pane still
#: holds — the same silent loss, one layer up.
CAPTURE_DEPTH_LINES = HISTORY_LIMIT_LINES + PANE_HEIGHT
