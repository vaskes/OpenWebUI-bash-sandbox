"""The suite's tmux server is its own — under every runner.

The suite drives a real tmux server.  The machinery that keeps the
machine clean may only kill the sessions the suite made, and it can only
tell them apart if the suite's tmux lives in a server of its own: tmux
keeps its socket in ``$TMUX_TMPDIR``, so a private directory is a
private server.

The rule has to hold under **every** runner.  It used to live in a
pytest fixture, and a plain ``python -m unittest discover`` does not
load a pytest fixture — so on the path the README offers ("stdlib
unittest, no pytest required") the suite ran against the machine's
*default* tmux server: two of the suite's own tests failed there, and
the sessions the live tests made were litter on somebody else's
server.  (That regression came in with the v2.14.5 wave, which
introduced the isolation itself; it was found when the README's claim
was finally measured instead of believed.)

The rule lives here now, and both runners call it.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile

# The private server of this process, and the value TMUX_TMPDIR had
# before the suite took it over.  One server per process: the second
# caller gets the first one's, so a pytest fixture and a plain unittest
# run cannot end up with two.
_STATE: dict[str, str | None] = {"private": None, "previous": None}


def ensure_private_tmux_server() -> str | None:
    """Give this process a tmux server of its own.

    Returns the private directory, or ``None`` when there is no tmux on
    the machine (the tmux tests skip then).  Calling it twice is safe:
    the first call wins, and the server it names is the one the suite
    uses.
    """
    if shutil.which("tmux") is None:
        return None
    private = _STATE["private"]
    if private is not None:
        # Re-assert it: a caller may have changed the environment in
        # between, and the suite's server must stay the private one.
        os.environ["TMUX_TMPDIR"] = private
        return private
    private = tempfile.mkdtemp(prefix="owbs-tmux-")
    _STATE["previous"] = os.environ.get("TMUX_TMPDIR")
    _STATE["private"] = private
    os.environ["TMUX_TMPDIR"] = private
    atexit.register(kill_private_tmux_server)
    return private


def kill_private_tmux_server() -> None:
    """Kill the suite's own tmux server — and only that one.

    ``tmux kill-server`` without ``TMUX_TMPDIR`` addresses the machine's
    default server, which belongs to whoever runs on the machine.  The
    environment is passed explicitly, so the socket that is addressed is
    the private one.
    """
    private = _STATE["private"]
    if private is None:
        return
    env = dict(os.environ)
    env["TMUX_TMPDIR"] = private
    subprocess.run(["tmux", "kill-server"], env=env, capture_output=True, check=False)
    previous = _STATE["previous"]
    if previous is None:
        os.environ.pop("TMUX_TMPDIR", None)
    else:
        os.environ["TMUX_TMPDIR"] = previous
    shutil.rmtree(private, ignore_errors=True)
