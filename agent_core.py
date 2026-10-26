"""Pure-function core of the sandbox agent.

Separated from :mod:`agent_api` so it can be unit-tested without pulling in
FastAPI, Pydantic, or tmux. Anything security-relevant (session-id
sanitization, naming conventions) lives here.
"""

from __future__ import annotations

import re

__version__ = "2.10.2"

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


def rootfs_is_read_only(mountinfo: str) -> bool:
    """Is the root filesystem mounted read-only?

    A root shell in a read-only container is a half-root: it owns the
    filesystem and cannot change a single byte of it. ``apt-get`` needs
    three writable paths — ``/var/lib/apt/lists`` (the index),
    ``/var/lib/dpkg`` (the package database) and ``/usr`` (where the
    packages land) — and a read-only rootfs refuses all three.

    Parses ``/proc/self/mountinfo``: the mount point of the root mount
    is field 5 and its options field 6; the options after the ``-``
    separator belong to the superblock. ``ro`` in either of them means
    read-only.

    A blob without a root mount counts as read-only: an unreadable
    mount table must never be read as "the root may write here".
    """
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 6:
            continue
        mount_point = fields[4]
        if mount_point != "/":
            continue
        options = fields[5]
        # The kernel separates the mount's own options from the
        # superblock's with " - ". What follows it is
        # ``fstype, source, super-options`` — the super options are the
        # last field. A dash inside a mount point is not the separator:
        # a space is escaped as \040, so " - " is unambiguous.
        if " - " in line:
            super_options = line.split(" - ", 1)[1].split()
            if super_options:
                options = f"{options},{super_options[-1]}"
        return "ro" in options.split(",")
    return True


def unquoted_standalone_braces(command: str) -> list[str]:
    """The unquoted ``{`` / ``}`` of *command* that stand alone as a word.

    A brace is only special to bash when it is a word of its own:
    ``echo }`` closes a brace group, while ``echo a}b`` and
    ``find -exec {} \\;`` do not. A brace inside quotes is text.

    The scan is deliberately conservative: it reports only what it is
    sure about, because a false negative here would change the meaning
    of the command we wrap.
    """
    found: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote is None:
            if ch == "\\":
                i += 2  # the escaped character is text, whatever it is
                continue
            if ch in ("'", '"'):
                quote = ch
                i += 1
                continue
            if ch in "{}":
                prev = command[i - 1] if i else ""
                nxt = command[i + 1] if i + 1 < n else ""
                alone = not (prev and not prev.isspace()) and not (nxt and not nxt.isspace())
                if alone:
                    found.append(ch)
            i += 1
            continue
        if ch == "\\" and quote == '"':
            i += 2  # inside double quotes a backslash escapes the next character
            continue
        if ch == quote:
            quote = None
        i += 1
    return found


def command_can_be_grouped(command: str) -> bool:
    """Can *command* be run inside a brace group of our own?

    A brace group lets us give the command a stdin of its own without
    changing what the command means — but only when the command does not
    contain a brace that bash would read as a group delimiter.
    """
    return not unquoted_standalone_braces(command)


def build_exec_block(command: str, begin_marker: str, ec_var: str, marker: str) -> tuple[str, bool]:
    """Build the shell code that runs one command and reports its exit code.

    The block is a sequence of lines the shell reads one by one:

    * ``printf`` prints the begin marker, so a caller can tell a whole
      output from a fragment;
    * the command itself;
    * the exit code is stored in ``ec_var``;
    * the last ``printf`` prints the completion marker with that code.

    The command runs inside a brace group whose stdin is ``/dev/null``.
    Without the group the command reads the *protocol*: the rest of the
    block is still sitting in the terminal's input buffer, so ``read x``
    waits forever, ``apt-get`` swallows the completion marker, and the
    caller gets a timeout instead of an answer. With the group the
    command reads EOF and the protocol stays intact.

    The group changes two things about the order of the shell's work,
    and both are accounted for in :func:`agent_api._extract_command_output`:

    * The shell parses the whole group before it runs any of it, so the
      command's output arrives *after* the line that closes the group.
      The bookkeeping therefore sits on that closing line, and the
      output follows it.
    * A continuation line is redrawn with ``PS2`` instead of ``PS1``.
      ``PS2="$PS1"`` makes the redraw look exactly like the one the
      command had without the group, so the caller's stdout does not
      change.

    Returns ``(block, grouped)``. ``grouped`` is False when the command
    contains a brace that bash would read as a group delimiter; such a
    command runs unwrapped, exactly as it did before, because wrapping
    it would change what it means.
    """
    if command_can_be_grouped(command):
        block = (
            f"printf '\\n{begin_marker}\\n'; PS2=\"$PS1\"\n"
            "{\n"
            + command
            + "\n"
            + f"}} < /dev/null; {ec_var}=$?; printf '\\n{marker}=%d\\n' \"${{{ec_var}}}\""
        )
        return block, True
    block = (
        f"printf '\\n{begin_marker}\\n'\n"
        + command
        + "\n"
        + f"{ec_var}=$?\n"
        + f"printf '\\n{marker}=%d\\n' \"${{{ec_var}}}\""
    )
    return block, False
