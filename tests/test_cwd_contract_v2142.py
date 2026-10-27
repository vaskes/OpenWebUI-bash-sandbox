"""A directory a session cannot honour is refused — not ignored.

The MCP ``bash`` tool takes a ``cwd``, and its description promises that
the command runs there. It did not: the directory was applied only when
the session was *created*, so a second call with a different ``cwd`` ran
the command in the session's own directory — somewhere the caller did
not ask for, without an error and without a word.

A running session has one directory, and it is not a parameter the
caller can set from the outside. The honest answer is a refusal that
names the way out (use another session id), and the door that has the
session registry is the right place to say it — so both doors (HTTP and
MCP) refuse the same way.

The second half of the bug: a session that was alive but unknown to the
registry (the sessions file is rebuilt after a restart, and it can be
stale) used to be *replaced* — a new window in the requested directory,
and the old one killed. The shell state the user had built up died with
it. A live session is now adopted, and the registry learns the pane's
real directory instead of the one the caller asked for.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from test_mcp_server import TOKEN, _fresh_app, _has_tmux  # noqa: E402

requires_tmux = unittest.skipUnless(_has_tmux(), "tmux is not installed")


@requires_tmux
class TestTheDirectoryOfASession(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        self.app, self.mcp, self.tmpdir = _fresh_app()
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8090")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.auth = {"Authorization": f"Bearer {TOKEN}"}
        self.ws = Path(self.tmpdir)
        self.sub = self.ws / "sub"
        self.sub.mkdir(exist_ok=True)
        # A session id of its own per test: the tmux server outlives the
        # test, and a session left behind by the previous test would be
        # adopted by this one — which is correct behaviour and a useless
        # test.
        self.sid = "cwd-" + uuid.uuid4().hex[:8]
        self.addCleanup(self._kill_session)

    def _kill_session(self) -> None:
        """Leave no session behind: the tmux server outlives the test."""
        import subprocess  # noqa: PLC0415

        import agent_api  # noqa: PLC0415

        agent_api.SESSIONS.pop(self.sid, None)
        subprocess.run(
            ["tmux", "kill-session", "-t", agent_api.tmux_name(self.sid)],
            capture_output=True,
            check=False,
        )

    # -- helpers -----------------------------------------------------------
    def _call(self, tool: str, arguments: dict) -> dict:
        """Call a tool over the wire and return the raw result object."""
        response = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            headers={**self.auth, "Accept": "application/json, text/event-stream"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("result", body, json.dumps(body))
        return body["result"]

    def _session(self, session_id: str | None = None) -> dict:
        """Ask the HTTP door about a session."""
        response = self.client.post(
            "/sessions",
            json={"session_id": session_id or self.sid},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # -- the contract ------------------------------------------------------
    def test_a_cwd_the_session_cannot_honour_is_refused(self) -> None:
        """The command must not run somewhere the caller did not ask for."""
        self._call("bash", {"command": "echo first", "session_id": self.sid})
        result = self._call(
            "bash",
            {"command": "pwd", "cwd": str(self.sub), "session_id": self.sid},
        )
        self.assertTrue(
            result["isError"],
            f"the second directory was accepted silently: {result.get('content')}",
        )
        text = result["content"][0]["text"]
        self.assertIn(str(self.ws), text, "the refusal does not name the directory")
        self.assertIn("session_id", text, "the refusal does not name the way out")

    def test_the_same_cwd_is_accepted(self) -> None:
        """Asking for the directory the session is in is not a conflict."""
        self._call("bash", {"command": "echo first", "session_id": self.sid})
        result = self._call(
            "bash",
            {"command": "pwd", "cwd": str(self.ws), "session_id": self.sid},
        )
        self.assertFalse(result["isError"], result.get("content"))

    def test_the_http_door_refuses_the_same_way(self) -> None:
        """One rule, both doors."""
        self._session()
        response = self.client.post(
            "/sessions",
            json={"session_id": self.sid, "cwd": str(self.sub)},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn(str(self.ws), response.json()["detail"])

    def test_a_live_session_is_adopted_not_replaced(self) -> None:
        """The state of a live session belongs to the session.

        The registry is a file, and a file can be stale: the agent was
        restarted, the file was rebuilt, and the pane is still there.
        The old code created a new window in the requested directory and
        killed the old one — the shell state died with it, and the
        registry recorded a directory the pane was not in.
        """
        self._call("bash", {"command": f"cd {self.sub}", "session_id": self.sid})
        self._call("bash", {"command": "echo alive", "session_id": self.sid})

        # The registry forgets the session; the pane does not.
        import agent_api  # noqa: PLC0415

        agent_api.SESSIONS.pop(self.sid, None)

        info = self._session()
        self.assertTrue(info["existed"], "the live session was replaced instead of adopted")
        self.assertEqual(
            os.path.realpath(info["cwd"]),
            os.path.realpath(str(self.sub)),
            "the registry recorded a directory the pane is not in",
        )

        # And the shell state is still there.
        result = self._call("bash", {"command": "pwd", "session_id": self.sid})
        self.assertFalse(result["isError"], result.get("content"))
        self.assertIn(
            os.path.realpath(str(self.sub)),
            result["content"][0]["text"],
            "the shell state of the adopted session was lost",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
