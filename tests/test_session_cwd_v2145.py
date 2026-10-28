"""A session that is known but not alive comes back where it was.

**What was wrong.** The auto-create path — the one that runs when a
caller execs in a session whose tmux backend has died (the container
was restarted, the pane crashed) — started the session in the
*workspace root* and wrote the root into the registry.  So the
directory the session had was silently gone, and the registry recorded
the root as if that were where it had always been.

The lifespan handler's own docstring promised the opposite: "the
auto-create path recreates a tmux backend if the session_id is known
but the tmux process has died, using the persisted cwd".  The promise
was not kept.

**What the fix does.** A known session comes back in the directory it
had, and the registry keeps saying so.  A session nobody knows about
still starts in the workspace root, and a directory that is gone any
more is not a directory to come back to.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agent_api_endpoints import _fresh_app  # noqa: E402


def _has_tmux() -> bool:
    import shutil  # noqa: PLC0415

    return shutil.which("tmux") is not None


requires_tmux = unittest.skipUnless(_has_tmux(), "tmux is not installed")


@requires_tmux
class TestTheSessionComesBackWhereItWas(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient  # noqa: PLC0415

        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-agent-token-xyz"}
        self.sub = Path(self.tmpdir) / "sub"
        self.sub.mkdir(exist_ok=True)
        # A session id of its own per test: the tmux server outlives the
        # test, and a session left behind by the previous test would be
        # adopted by this one.
        self.sid = "dead-" + uuid.uuid4().hex[:8]
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        import agent_api  # noqa: PLC0415

        agent_api.SESSIONS.pop(self.sid, None)
        subprocess.run(
            ["tmux", "kill-session", "-t", agent_api.tmux_name(self.sid)],
            capture_output=True,
            check=False,
        )

    def _create(self, cwd: str | None) -> None:
        response = self.client.post(
            "/sessions",
            json={"session_id": self.sid, **({"cwd": cwd} if cwd else {})},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _exec(self, command: str) -> dict:
        response = self.client.post(
            f"/sessions/{self.sid}/exec",
            json={"command": command, "timeout": 10},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_dead_session_comes_back_in_its_directory(self) -> None:
        self._create(str(self.sub))
        import agent_api  # noqa: PLC0415

        self.assertEqual(
            agent_api.SESSIONS[self.sid]["cwd"],
            str(self.sub),
            "the session was not created in the directory that was asked for",
        )

        # The pane dies; the registry remembers.
        subprocess.run(
            ["tmux", "kill-session", "-t", agent_api.tmux_name(self.sid)],
            capture_output=True,
            check=False,
        )
        self.assertIn(self.sid, agent_api.SESSIONS, "the registry forgot the session")

        result = self._exec("pwd")
        self.assertIn(str(self.sub), result["stdout"], result)
        self.assertEqual(
            agent_api.SESSIONS[self.sid]["cwd"],
            str(self.sub),
            "the registry recorded a directory the session was not in",
        )

    def test_an_unknown_session_still_starts_in_the_workspace_root(self) -> None:
        result = self._exec("pwd")
        self.assertIn(str(self.workspace_real), result["stdout"], result)


if __name__ == "__main__":
    unittest.main()
