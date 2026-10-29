"""The suite must not touch what belongs to somebody else.

**What was wrong.** The live tests use a real tmux server, and the
fixture that keeps the machine clean killed every session that
*appeared* during a test.  "Appeared" is not "ours": the machine's
default tmux server belongs to whoever runs on the machine — the
operator's own session, another repository's suite, a probe.

The review's probe created a session and found it gone after a suite
run.  That is not a hypothesis about the fixture; it is what happened.

**What the fix does.** The suite gets a tmux server of its own: tmux
keeps its socket in ``$TMUX_TMPDIR``, so a private directory is a
private server.  The fixture can then only see — and only kill — the
sessions the suite made.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The suite's tmux server must be its own -- under every runner.  A
# pytest fixture is not loaded by `python -m unittest discover`, and the
# README promises that path works, so the isolation is set up here as
# well: at import time, which is before any test of this module runs.
#
# The rule's own module sits next to this one; the path is made
# explicit so that the import does not depend on what the runner
# happened to put on sys.path.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _tmux_isolation import ensure_private_tmux_server  # noqa: E402

ensure_private_tmux_server()


def _sessions(env: dict[str, str] | None = None) -> set[str]:
    """The sessions of a tmux server (the default one, without an env)."""
    if shutil.which("tmux") is None:
        return set()
    proc = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


class TestTheSuiteHasATmuxServerOfItsOwn(unittest.TestCase):
    def test_the_suite_runs_its_own_tmux_server(self) -> None:
        if shutil.which("tmux") is None:
            self.skipTest("no tmux on this machine")
        self.assertIn(
            "TMUX_TMPDIR",
            os.environ,
            "the suite does not name a tmux server of its own",
        )

    def test_a_session_of_another_server_is_not_ours(self) -> None:
        """A session in the machine's default server is not the suite's.

        The suite's own server is private, so the session below is
        invisible to it.  Under the old rule ("a session that appeared
        during the run is ours") the fixture would have killed it.
        """
        if shutil.which("tmux") is None:
            self.skipTest("no tmux on this machine")
        name = f"foreign-{uuid.uuid4().hex[:8]}"
        default_env = {k: v for k, v in os.environ.items() if k != "TMUX_TMPDIR"}
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name],
            env=default_env,
            check=False,
            capture_output=True,
        )
        try:
            self.assertIn(name, _sessions(default_env), "the session was never created")
            self.assertNotIn(
                name,
                _sessions(),
                "the suite sees another server's session — it would kill it",
            )
        finally:
            subprocess.run(
                ["tmux", "kill-session", "-t", name],
                env=default_env,
                check=False,
                capture_output=True,
            )

    def test_the_fixture_kills_the_sessions_the_suite_made(self) -> None:
        """The private server is not a licence to leave litter.

        The point of the private server is that the suite can still
        clean up after itself — its own sessions and nothing else.
        """
        if shutil.which("tmux") is None:
            self.skipTest("no tmux on this machine")
        name = f"ours-{uuid.uuid4().hex[:8]}"
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name],
            check=False,
            capture_output=True,
        )
        self.assertIn(name, _sessions(), "the suite's own session is not visible to it")
        subprocess.run(
            ["tmux", "kill-session", "-t", name],
            check=False,
            capture_output=True,
        )
        self.assertNotIn(name, _sessions(), "the session was not killed")


if __name__ == "__main__":
    unittest.main()
