"""Pytest configuration — adds the project root to sys.path so we can import
the application modules without a package layout.

It also leaves the machine as it found it: the live tests use a real
tmux server, and a session they leave behind is litter *and* a source
of flakiness — the next run adopts it (the registry is a file, the pane
is not) and it carries the state of the run before, a ``cd`` and an
``export`` included.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest
from _tmux_isolation import ensure_private_tmux_server, kill_private_tmux_server

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _tmux_sessions() -> set[str]:
    """The sessions of the default tmux server."""
    if shutil.which("tmux") is None:
        return set()
    proc = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


@pytest.fixture(scope="session", autouse=True)
def _the_suite_has_a_tmux_server_of_its_own(tmp_path_factory):
    """The suite's tmux lives in a server of its own.

    tmux keeps its socket in ``$TMUX_TMPDIR``, so a private directory
    is a private server.  That is what makes the fixture below safe:
    it can only see — and only kill — the sessions the suite itself
    made.

    The old rule was "a session that appeared during the run is ours",
    and that is not the same thing.  The machine's default server
    belongs to whoever runs on the machine: the operator's own
    session, another repository's suite, a probe.  The review's probe
    was killed by a suite run — live, not hypothetically.
    """
    # The rule itself lives in tests/_tmux_isolation.py, because it has
    # to hold under every runner and a plain `python -m unittest
    # discover` does not load a pytest fixture.
    private = ensure_private_tmux_server()
    if private is None:
        yield
        return
    try:
        yield
    finally:
        # Only our own server: tmux reads $TMUX_TMPDIR for the socket.
        kill_private_tmux_server()


@pytest.fixture(scope="session")
def _the_sessions_of_the_machine():
    """The tmux sessions that were running before the suite started.

    Whatever was running on the machine before the suite started belongs
    to whoever started it: the operator may have their own session open
    on the same box, and the suite has no business killing it.
    """
    return _tmux_sessions()


@pytest.fixture(autouse=True)
def _no_test_leaves_a_session_behind(_the_sessions_of_the_machine):
    """Kill the tmux sessions the test created — and only those.

    A session that cannot be killed is reported. "The suite left a
    session behind" is not a thing one should discover a week later,
    when a run inherits a pane with somebody else's ``cd`` in it.

    The check is against the sessions of the *whole run*, not of this
    test: a session a previous test left behind is already in this
    test's "before" set, and the leftover would go unnoticed — the
    first test to leave it is the one that has to be told.
    """
    before = _tmux_sessions()
    yield
    for name in sorted(_tmux_sessions() - before):
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, check=False)
    left = sorted(_tmux_sessions() - _the_sessions_of_the_machine)
    if left:
        raise RuntimeError(f"the test left tmux sessions behind: {left}")
