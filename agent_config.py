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

And two more came with the file tools, on the axes those tools travel on:

* ``AGENT_MCP_MAX_WRITE_BYTES`` — how many bytes one MCP ``write_file``
  (or one ``patch``) may put on disk. The bytes here travel the other way
  round: they are in the request, and the request body is capped by the
  transport. So the ceiling is the transport's number, and the budget
  defaults to the same size as the read budget — a file the model cannot
  read back is a file it should not have written.
* ``AGENT_MCP_MAX_SEARCH_RESULTS`` — how many matches one search
  returns. A count, not a size: the walk is bounded by the machine and by
  a time budget, and the count is what keeps one call's answer from
  becoming the whole workspace.

And one door that is not a budget on a file: the **vision door**. The
operator may name an endpoint of their own that looks at pictures
(``AGENT_VISION_URL``), and the door has budgets of its own — the
picture, the question and the answer. The endpoint is the operator's
choice and the model's is not: while the URL is empty the door is
closed, and the tool takes a path inside the workspace rather than a
URL, because a door that fetches a URL the model names is a retrieval
door wearing a different hat.

The stdout budget is only meaningful if the tmux pane can actually hold it.
A pane that is smaller than the budget loses the *beginning* of the output
silently — no truncation marker, no error — so the scrollback depth is
derived from the budget here rather than left at a constant that only looks
right on paper.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

__all__ = [
    "BINARY_LIMIT_BYTES",
    "CAPTURE_DEPTH_LINES",
    "HISTORY_LIMIT_LINES",
    "MCP_ALLOWED_HOSTS",
    "MCP_READ_CEILING_BYTES",
    "MCP_READ_LIMIT_BYTES",
    "MCP_READ_LIMIT_DEFAULT_BYTES",
    "MCP_SEARCH_CEILING_RESULTS",
    "MCP_SEARCH_DEFAULT_RESULTS",
    "MCP_SEARCH_MAX_RESULTS",
    "MCP_SEARCH_TIME_BUDGET_SECONDS",
    "MCP_WRITE_CEILING_BYTES",
    "MCP_WRITE_LIMIT_BYTES",
    "MCP_WRITE_LIMIT_DEFAULT_BYTES",
    "MEMORY_MAX_NOTE_BYTES",
    "MEMORY_MAX_NOTES",
    "PANE_HEIGHT",
    "PANE_WIDTH",
    "PREINSTALLED_PYTHON_PATH",
    "SESSION_HISTORY_MAX_COMMAND_BYTES",
    "SESSION_HISTORY_MAX_COMMANDS",
    "SESSION_SEARCH_MAX_RESULTS",
    "STDOUT_LIMIT_BYTES",
    "TODO_MAX_ITEM_BYTES",
    "TODO_MAX_ITEMS",
    "model_read_limit",
    "model_write_limit",
    "VISION_API_KEY",
    "VISION_MAX_ANSWER_BYTES",
    "VISION_MAX_BYTES",
    "VISION_MAX_CEILING_BYTES",
    "VISION_MAX_PROMPT_BYTES",
    "VISION_MODEL",
    "VISION_TIMEOUT_SECONDS",
    "VISION_URL",
    "model_read_limit",
    "model_write_limit",
    "parse_count",
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


def parse_count(
    raw: str,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    """Parse a plain integer knob, and check it against its range.

    Not every budget is a size — a count of matches is a number, and a
    number with a suffix in it is a typo. The rule is the same as for
    every other knob here: a value outside the documented range is a
    **startup failure**, not a clamp, because a silently clamped limit
    is indistinguishable from a limit that was never applied.
    """
    text = str(raw).strip()
    if not text:
        raise ValueError(f"{name} is empty")
    if not text.isdigit():
        raise ValueError(f"{name}={raw!r} is not a whole number")
    value = int(text)
    if value < minimum:
        raise ValueError(f"{name}={raw!r} is {value}, below the minimum of {minimum}")
    if value > maximum:
        raise ValueError(f"{name}={raw!r} is {value}, above the maximum of {maximum}")
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

#: The transport ceiling for the *upload* door (``POST /files``).
#:
#: The command doors carry a body of JSON, and 2 MB is a generous
#: ceiling for that: a body that weighs more is either a mistake or an
#: attack. The upload door carries a *file*, and the file that has to
#: come through it is tens of megabytes - so the door needs a ceiling of
#: its own, and the general one would refuse the very bytes the door
#: exists to deliver.
#:
#: This is the *transport* ceiling, not the file policy: the file that
#: lands is still measured against ``AGENT_BINARY_LIMIT``, and the
#: transport ceiling only decides how many bytes are read before the
#: request is refused. It therefore has to be **at least**
#: ``AGENT_BINARY_LIMIT`` - a transport ceiling below the file policy
#: refuses an upload the door itself would have accepted, and refuses it
#: with the vaguer of the two answers.
#:
#: 64 MiB is the default: it is comfortably above the 50 MiB the
#: production deployment allows as a file, so the file policy is the one
#: that answers, and it is a round number an operator can reason about.
UPLOAD_MAX_BYTES = parse_size(
    os.environ.get("AGENT_UPLOAD_MAX_BYTES", "64m"),
    name="AGENT_UPLOAD_MAX_BYTES",
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

#: The hard ceiling for one MCP write, independent of any configured limit.
#:
#: A write travels the other way round from a read: the bytes are in the
#: request, and the request body is capped by the transport
#: (``MAX_BODY_BYTES``, 2 MiB). So the ceiling here is not a policy the
#: operator sets — it is the largest write the door can physically
#: carry, and a budget above it would be a budget the transport
#: silently clamps. That is why the ceiling is the transport's number
#: and not a round one of our own.
MCP_WRITE_CEILING_BYTES = 2 * _MIB

#: The default model-facing write budget: one mebibyte. A file the model
#: writes is a file it is going to read back, so the write budget and
#: the read budget are the same size on purpose: a file the model
#: cannot read back is a file it should not have written.
MCP_WRITE_LIMIT_DEFAULT_BYTES = 1 * _MIB

#: The model-facing write budget. Unlike the read budget this one has no
#: "0 means unlimited" convention: the transport makes an unlimited
#: write impossible, and a knob that offers a value the door cannot
#: deliver is a knob that lies. The range ends at the transport
#: ceiling, because a value above it would be silently clamped.
MCP_WRITE_LIMIT_BYTES = parse_size(
    os.environ.get("AGENT_MCP_MAX_WRITE_BYTES", str(MCP_WRITE_LIMIT_DEFAULT_BYTES)),
    name="AGENT_MCP_MAX_WRITE_BYTES",
    minimum=64 * _KIB,
    maximum=MCP_WRITE_CEILING_BYTES,
)

#: How many matches one search may return. The ceiling is what the tool
#: promises at most; the budget is what it returns by default.
MCP_SEARCH_CEILING_RESULTS = 1000
MCP_SEARCH_DEFAULT_RESULTS = 100

#: The model-facing search budget: how many matches one search returns
#: unless the caller asks for fewer. It is a count and not a size, so it
#: is parsed as one — with the same rule as every other knob: a value
#: outside the range is a startup failure, not a clamp.
MCP_SEARCH_MAX_RESULTS = parse_count(
    os.environ.get("AGENT_MCP_MAX_SEARCH_RESULTS", str(MCP_SEARCH_DEFAULT_RESULTS)),
    name="AGENT_MCP_MAX_SEARCH_RESULTS",
    minimum=1,
    maximum=MCP_SEARCH_CEILING_RESULTS,
)

#: The default picture budget: eight mebibytes.
VISION_MAX_DEFAULT_BYTES = 8 * _MIB

#: The default answer budget: 64 KiB. The answer goes into a model's
#: context, where a megabyte of text costs attention and memory.
VISION_MAX_ANSWER_DEFAULT_BYTES = 64 * _KIB

#: The default prompt budget: 8 KiB.
VISION_MAX_PROMPT_DEFAULT_BYTES = 8 * _KIB

#: The default timeout for one call to the vision endpoint: a minute.
VISION_TIMEOUT_DEFAULT_SECONDS = 60

#: The default bounds of the session tools.
TODO_MAX_ITEMS_DEFAULT = 50
TODO_MAX_ITEM_BYTES_DEFAULT = 512
MEMORY_MAX_NOTES_DEFAULT = 100
MEMORY_MAX_NOTE_BYTES_DEFAULT = 4000
SESSION_HISTORY_MAX_COMMANDS_DEFAULT = 100
SESSION_HISTORY_MAX_COMMAND_BYTES_DEFAULT = 1000
SESSION_SEARCH_MAX_RESULTS_DEFAULT = 50

#: How long one search may walk the workspace, in seconds.
#:
#: A constant and not a knob: the walk is bounded by the machine, not
#: by a policy the operator has an opinion about. A search that runs
#: out of time says so — it returns what it found and names the
#: reason, because a truncated answer that does not say it is
#: truncated is a lie the model will act on.
MCP_SEARCH_TIME_BUDGET_SECONDS = 30

# ---------------------------------------------------------------------------
# The vision door
# ---------------------------------------------------------------------------
#: The endpoint that looks at pictures. Empty means the door is **closed**:
#: ``vision_analyze`` refuses and says the door is closed.
#:
#: The endpoint is the operator's choice and nobody else's. There is no
#: knob the model can reach, and the tool takes a path inside the
#: workspace — not a URL — because a door that fetches a URL the model
#: names is a retrieval door wearing a different hat.
VISION_URL = os.environ.get("AGENT_VISION_URL", "").strip()

#: The bearer for that endpoint, when it wants one. Empty means no header.
VISION_API_KEY = os.environ.get("AGENT_VISION_API_KEY", "").strip()

#: The model name the endpoint expects.
#:
#: Required when the URL is set: an endpoint without a model name is a
#: misconfiguration, and a misconfiguration is loud at startup — not a
#: surprise at the first picture.
VISION_MODEL = os.environ.get("AGENT_VISION_MODEL", "").strip()

#: The ceiling for one picture: 32 MiB. The picture travels in the
#: request to the endpoint as a base64 data URI, so the request is about
#: a third bigger than the picture — the ceiling is on the picture, and
#: the request that carries it.
VISION_MAX_CEILING_BYTES = 32 * _MIB

#: The largest picture the door will hand to the endpoint.
VISION_MAX_BYTES = parse_size(
    os.environ.get("AGENT_VISION_MAX_BYTES", str(VISION_MAX_DEFAULT_BYTES)),
    name="AGENT_VISION_MAX_BYTES",
    minimum=64 * _KIB,
    maximum=VISION_MAX_CEILING_BYTES,
)

#: How long the door waits for the answer.
VISION_TIMEOUT_SECONDS = parse_count(
    os.environ.get("AGENT_VISION_TIMEOUT", str(VISION_TIMEOUT_DEFAULT_SECONDS)),
    name="AGENT_VISION_TIMEOUT",
    minimum=1,
    maximum=600,
)

#: The largest answer the door will hand back. The answer travels into the
#: model's context, so it is bounded the way a read is bounded: a
#: truncated answer says that it is truncated.
VISION_MAX_ANSWER_BYTES = parse_size(
    os.environ.get("AGENT_VISION_MAX_ANSWER_BYTES", str(VISION_MAX_ANSWER_DEFAULT_BYTES)),
    name="AGENT_VISION_MAX_ANSWER_BYTES",
    minimum=1024,
    maximum=MCP_READ_CEILING_BYTES,
)

#: The largest prompt the door will forward. A prompt is text the model
#: wrote; there is no reason for it to be long, and a long one is the
#: cheapest way to make the endpoint's answer expensive.
VISION_MAX_PROMPT_BYTES = parse_size(
    os.environ.get("AGENT_VISION_MAX_PROMPT_BYTES", str(VISION_MAX_PROMPT_DEFAULT_BYTES)),
    name="AGENT_VISION_MAX_PROMPT_BYTES",
    minimum=16,
    maximum=MCP_READ_CEILING_BYTES,
)

# ---------------------------------------------------------------------------
# The session tools: a task list, a memory, a recall, a question
# ---------------------------------------------------------------------------
#: How many items one session's task list may hold. The list is the
#: model's own plan; it is bounded because a plan that grows without
#: limit is a log, and a log is not a plan.
TODO_MAX_ITEMS = parse_count(
    os.environ.get("AGENT_TODO_MAX_ITEMS", str(TODO_MAX_ITEMS_DEFAULT)),
    name="AGENT_TODO_MAX_ITEMS",
    minimum=1,
    maximum=500,
)

#: How long one item of the task list may be.
TODO_MAX_ITEM_BYTES = parse_size(
    os.environ.get("AGENT_TODO_MAX_ITEM_BYTES", str(TODO_MAX_ITEM_BYTES_DEFAULT)),
    name="AGENT_TODO_MAX_ITEM_BYTES",
    minimum=16,
    maximum=64 * _KIB,
)

#: How many notes the memory may hold. The memory is the model's own
#: file: small, readable and deletable, and not a profile of the
#: operator.
MEMORY_MAX_NOTES = parse_count(
    os.environ.get("AGENT_MEMORY_MAX_NOTES", str(MEMORY_MAX_NOTES_DEFAULT)),
    name="AGENT_MEMORY_MAX_NOTES",
    minimum=1,
    maximum=1000,
)

#: How long one note of the memory may be.
MEMORY_MAX_NOTE_BYTES = parse_size(
    os.environ.get("AGENT_MEMORY_MAX_NOTE_BYTES", str(MEMORY_MAX_NOTE_BYTES_DEFAULT)),
    name="AGENT_MEMORY_MAX_NOTE_BYTES",
    minimum=16,
    maximum=256 * _KIB,
)

#: How many commands one session remembers. The recall searches these:
#: a session that forgets what it ran is a session the model has to
#: guess about.
SESSION_HISTORY_MAX_COMMANDS = parse_count(
    os.environ.get("AGENT_SESSION_HISTORY_MAX_COMMANDS", str(SESSION_HISTORY_MAX_COMMANDS_DEFAULT)),
    name="AGENT_SESSION_HISTORY_MAX_COMMANDS",
    minimum=1,
    maximum=1000,
)

#: How long one remembered command may be.
SESSION_HISTORY_MAX_COMMAND_BYTES = parse_size(
    os.environ.get(
        "AGENT_SESSION_HISTORY_MAX_COMMAND_BYTES", str(SESSION_HISTORY_MAX_COMMAND_BYTES_DEFAULT)
    ),
    name="AGENT_SESSION_HISTORY_MAX_COMMAND_BYTES",
    minimum=16,
    maximum=64 * _KIB,
)

#: How many matches one recall returns.
SESSION_SEARCH_MAX_RESULTS = parse_count(
    os.environ.get("AGENT_SESSION_SEARCH_MAX_RESULTS", str(SESSION_SEARCH_MAX_RESULTS_DEFAULT)),
    name="AGENT_SESSION_SEARCH_MAX_RESULTS",
    minimum=1,
    maximum=500,
)

if VISION_URL:
    if not VISION_MODEL:
        raise ValueError(
            "AGENT_VISION_MODEL must be set when AGENT_VISION_URL is set: "
            "the endpoint has to be told which model to use"
        )
    _vision_scheme = urlsplit(VISION_URL).scheme.lower()
    if _vision_scheme not in ("http", "https"):
        raise ValueError(
            f"AGENT_VISION_URL={VISION_URL!r} is not an http(s) URL; "
            "the vision door speaks http and https, and nothing else"
        )


#: The hosts the MCP endpoint answers for.
#:
#: An empty list means "no check": the protection against DNS rebinding
#: is opt-in (see ``mcp_server.transport_security``). A pattern may end
#: in ``:*`` to allow any port.
#:
#: This knob used to be read from the environment by the MCP module
#: itself, on every call. It is the one knob in the project that did
#: not come through here — and the reason it mattered was not the
#: reading but the *timing*: the check is decided when the MCP
#: application is built, which happens at import, so a value read later
#: could disagree with the decision that was already made.
MCP_ALLOWED_HOSTS: tuple[str, ...] = tuple(
    item.strip()
    for item in os.environ.get("AGENT_MCP_ALLOWED_HOSTS", "").split(",")
    if item.strip()
)


def model_read_limit() -> int:
    """How many bytes one MCP read may return.

    Three limits, and the smallest one wins:

    * the model-facing budget (``AGENT_MCP_MAX_READ_BYTES``) — how much
      one call may put into a model's context;
    * the file policy the HTTP door applies (``AGENT_BINARY_LIMIT``) — a
      limit of 0 means "no limit" and does not apply here;
    * the hard ceiling (:data:`MCP_READ_CEILING_BYTES`) — the guard that
      stays when the policy is 0.

    The budget is the reason this function exists. The binary limit is
    about a file that travels to a client, where the bytes land in a
    file; the deployment runs it at 50 MiB on purpose. Here the bytes
    land in a prompt: the file is held in memory and base64-encoded on
    the way out, and a megabyte of text is already more than a text
    model can use. The HTTP door keeps its own limit — untouched.

    It lives here, and not in the MCP module, because more than one
    tool needs it: the search reads files too, and a search that reads
    a file the read door would refuse is a hole in the read door.
    """
    limit = MCP_READ_CEILING_BYTES
    policy = BINARY_LIMIT_BYTES
    if policy:
        limit = min(limit, policy)
    budget = MCP_READ_LIMIT_BYTES
    if budget:
        limit = min(limit, budget)
    return limit


def model_write_limit() -> int:
    """How many bytes one MCP write may put on disk.

    The same three limits as the read, on the write's own axis: the
    model-facing budget (``AGENT_MCP_MAX_WRITE_BYTES``), the file policy
    the HTTP door applies (``AGENT_BINARY_LIMIT``) and the transport
    ceiling (:data:`MCP_WRITE_CEILING_BYTES`).

    The file policy is in the list for the same reason it is in the
    read's: a file the model writes is a file the door may serve, and a
    file the door may not serve is a file the model should not have
    written.
    """
    limit = MCP_WRITE_CEILING_BYTES
    policy = BINARY_LIMIT_BYTES
    if policy:
        limit = min(limit, policy)
    budget = MCP_WRITE_LIMIT_BYTES
    if budget:
        limit = min(limit, budget)
    return limit


#: Scrollback the pane must hold for the stdout budget to be deliverable.
#: One line of a 200-column pane is 200 bytes, so this is the budget
#: divided by the width, rounded up.
HISTORY_LIMIT_LINES = -(-STDOUT_LIMIT_BYTES // MIN_LINE_WIDTH) + HISTORY_MARGIN_LINES

#: Where the preinstalled python packages live in the sandbox image.
#:
#: The image ships a tree of packages the model keeps asking for (numpy,
#: matplotlib, ...) plus the wheelhouse it was built from. The tree is
#: put on PYTHONPATH for the **model's shell only** -- see
#: ``agent_api._tmux_environment`` -- and deliberately not for the agent
#: service: the tree carries its own pydantic and httpx, and letting
#: those shadow the service's pinned dependencies would break the
#: service for a reason that has nothing to do with the model.
#:
#: The path is a fact in two places (this constant and the Dockerfile),
#: so ``tests/test_python_env_v2180.py`` compares the two instead of
#: trusting either.
PREINSTALLED_PYTHON_PATH = "/opt/pylibs"

#: How far back a capture may reach. It has to cover the whole scrollback
#: plus the visible pane, or the capture truncates what the pane still
#: holds — the same silent loss, one layer up.
CAPTURE_DEPTH_LINES = HISTORY_LIMIT_LINES + PANE_HEIGHT
