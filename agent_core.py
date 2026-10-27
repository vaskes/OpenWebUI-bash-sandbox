"""Pure-function core of the sandbox agent.

Separated from :mod:`agent_api` so it can be unit-tested without pulling in
FastAPI, Pydantic, or tmux. Anything security-relevant (session-id
sanitization, naming conventions) lives here.
"""

from __future__ import annotations

import re
import uuid

__version__ = "2.13.1"

TMUX_PREFIX = "agent_"

#: How many hex characters a protocol marker carries. The marker names
#: the bookkeeping lines the exec protocol leaves in the terminal, so a
#: command that prints a line of its own must not be able to impersonate
#: one. 16 hex characters are 64 bits: the chance of a collision is not
#: a number anybody needs to compute any more.
MARKER_HEX_LEN = 16
SESSION_ID_MAX_LEN = 64
SESSION_ID_PATTERN = re.compile(r"[^a-zA-Z0-9_-]")


def new_marker_token() -> str:
    """A fresh random token for the exec protocol's markers.

    Three markers are used per command (begin, exit-code variable, done)
    and they must not collide with anything the command prints, nor with
    each other.
    """
    return uuid.uuid4().hex[:MARKER_HEX_LEN]


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


# ---------------------------------------------------------------------------
# Secret redaction for the command preview that goes into the log
# ---------------------------------------------------------------------------

#: The words that make an identifier secret. Matched per *segment*, so
#: ``API_KEY`` and ``apiKey`` count while ``monkey`` does not.
_SECRET_NAME_PARTS = frozenset(
    {
        "key",
        "keys",
        "token",
        "tokens",
        "secret",
        "secrets",
        "password",
        "passwd",
        "pwd",
        "credential",
        "credentials",
    }
)

#: What a redacted value is replaced with.
REDACTED = "[REDACTED]"

# The redaction patterns below are matched against a *command*, which is
# attacker-controlled text. Two properties matter, and they pull in
# opposite directions: the pattern has to find a secret wherever it sits,
# and it must not take time proportional to the square of the command
# length.
#
# The possessive quantifiers are what keep the match linear. A greedy
# star gives characters back when the rest of the pattern does not
# match, so a command made of 100 000 "A"s is retried at every position
# of the run: the cost of the match is quadratic in the length of the
# command. A possessive quantifier never gives anything back, so there
# is nothing to retry. (CodeQL: py/polynomial-redos.)
#
# The possessive form also has to *mean* the same thing as the greedy
# one — the two are compared over a corpus in
# tests/test_review_v2110.py, because a redaction that quietly stops
# matching is worse than a slow one.
#
# Possessive quantifiers arrived in Python 3.11, and the project runs
# 3.11 and newer.

_ASSIGNMENT_RE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.-]*+)(?P<eq>\s*=\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s]+)"
)
_FLAG_RE = re.compile(
    r"(?P<flag>--[A-Za-z0-9][A-Za-z0-9-]*+)(?P<eq>=|\s+)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s]+)"
)
_COLON_RE = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_.-]*+)(?P<eq>\s*:\s*)(?P<value>[^\s\"']+)")
_AUTH_RE = re.compile(
    r"(?P<head>\b[Aa]uthorization\s*:\s*)(?P<scheme>[Bb]earer|[Bb]asic)\s+"
    r"(?P<value>[^\s\"']+)"
)
_SSHPASS_RE = re.compile(r"(?P<head>\bsshpass\s+-p\s+)(?P<value>[^\s]+)")
_PEM_RE = re.compile(
    # The body is bounded: a private key is a few kilobytes, and an
    # unbounded ".*?" between two markers means that every marker in the
    # text starts a scan to the end of the text — the cost of the match
    # becomes the square of the length of the command.
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?P<body>.{0,8192}?)"
    r"-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


def _name_parts(name: str) -> set[str]:
    """Split an identifier into lowercase words.

    ``OPENAI_API_KEY`` -> ``{openai, api, key}``, ``apiKey`` ->
    ``{api, key}``, ``monkey`` -> ``{monkey}``. The split is what keeps
    ``monkey=banana`` out of the redacted set.
    """
    parts: set[str] = set()
    for chunk in re.findall(r"[A-Za-z]+|[0-9]+", name):
        for word in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+", chunk):
            parts.add(word.lower())
    return parts


def _is_secret_name(name: str) -> bool:
    """True when an identifier looks like it holds a secret."""
    return bool(_name_parts(name) & _SECRET_NAME_PARTS)


def _redact_value(value: str) -> str:
    """Replace a value with the marker, keeping its quotes."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[0] + REDACTED + value[0]
    return REDACTED


def redact_secrets(text: str) -> str:
    """Hide secret-shaped values in a string that is about to be logged.

    The agent logs a 200-character preview of every command, so that an
    operator can reconstruct what the model ran. A command that carries
    a token — ``curl -H "Authorization: Bearer …"``, ``export API_KEY=…``,
    ``mysql --password …`` — would otherwise put that token into the
    log file, where it stays readable by everything that can read the
    logs.

    What is redacted: the values of assignments, flags and ``name:
    value`` pairs whose name looks secret; the value of an
    ``Authorization:`` header; the value of ``sshpass -p``; and PEM
    private-key blocks. The names stay, so the log still says what the
    command was doing — a redacted log is still an audit trail.
    """
    if not text:
        return text
    text = _PEM_RE.sub(REDACTED + " PRIVATE KEY", text)
    text = _AUTH_RE.sub(lambda m: f"{m.group('head')}{m.group('scheme')} {REDACTED}", text)
    text = _SSHPASS_RE.sub(lambda m: f"{m.group('head')}{REDACTED}", text)

    def _flag_sub(match: re.Match[str]) -> str:
        flag = match.group("flag")
        if not _is_secret_name(flag.lstrip("-").replace("-", "_")):
            return match.group(0)
        return f"{flag}{match.group('eq')}{_redact_value(match.group('value'))}"

    text = _FLAG_RE.sub(_flag_sub, text)

    def _assignment_sub(match: re.Match[str]) -> str:
        name = match.group("name")
        if not _is_secret_name(name):
            return match.group(0)
        return f"{name}{match.group('eq')}{_redact_value(match.group('value'))}"

    text = _ASSIGNMENT_RE.sub(_assignment_sub, text)

    def _colon_sub(match: re.Match[str]) -> str:
        name = match.group("name")
        if not _is_secret_name(name):
            return match.group(0)
        return f"{name}{match.group('eq')}{REDACTED}"

    return _COLON_RE.sub(_colon_sub, text)
