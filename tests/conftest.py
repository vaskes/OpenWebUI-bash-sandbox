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


@pytest.fixture(autouse=True)
def _no_test_leaves_a_session_behind():
    """Kill the tmux sessions the test created — and only those.

    Whatever was running on the machine before the test started belongs
    to whoever started it: the operator may have their own session open
    on the same box, and the suite has no business killing it.

    A session that cannot be killed is reported. "The suite left a
    session behind" is not a thing one should discover a week later,
    when a run inherits a pane with somebody else's ``cd`` in it.
    """
    before = _tmux_sessions()
    yield
    for name in sorted(_tmux_sessions() - before):
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, check=False)
    left = sorted(_tmux_sessions() - before)
    if left:
        raise RuntimeError(f"the test left tmux sessions behind: {left}")
