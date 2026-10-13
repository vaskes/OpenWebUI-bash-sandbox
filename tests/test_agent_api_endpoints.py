"""Integration tests for :mod:`agent_api` HTTP endpoints.

Uses ``fastapi.testclient.TestClient`` and patches ``agent_api._tmux`` so
endpoint logic can be exercised without a real tmux server. Patching is
per-test so failures don't leak between cases.

The trickiest part: ``exec_cmd`` generates a fresh ``__AGENT_DONE_<hex>__``
marker on every call, so the mock has to produce output that contains
*whichever* marker the endpoint just injected. The ``_tmux_mock_factory``
helper below handles this by reading the send-keys call and embedding the
real marker into a follow-up capture-pane response.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_app():
    """Reload the agent_api module with a per-call SESSIONS_FILE in a fresh
    tempdir. Returns ``(app, SESSIONS, tmpdir, workspace_real)``."""
    tmpdir = tempfile.mkdtemp(prefix="agent-test-")
    os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmpdir, "sessions.json")
    os.environ["AGENT_WORKSPACE"] = tmpdir
    os.environ["AGENT_TOKEN"] = "test-agent-token-xyz"
    os.makedirs(tmpdir, exist_ok=True)
    if "agent_api" in sys.modules:
        del sys.modules["agent_api"]
    from agent_api import SESSIONS, WORKSPACE_REAL, app  # noqa: E402

    SESSIONS.clear()
    return app, SESSIONS, tmpdir, WORKSPACE_REAL


def _make_tmux_mock():
    """Return ``(mock, schedule)``.

    ``mock`` is an AsyncMock suitable for ``patch("agent_api._tmux", new=mock)``.

    ``schedule`` is a builder: call ``schedule.on(cmd, rc=0, stdout="")`` to
    pre-record a response for a tmux invocation. When the mock is called,
    it picks the most recent matching schedule. Unmatched calls return
    ``(0, "", "")`` so post-test cleanup (lifespan shutdown) doesn't raise.

    For ``capture-pane`` after a ``send-keys`` that included a marker, the
    mock auto-extracts the marker from the send-keys call and substitutes
    it into a configurable template.
    """
    schedules: list[tuple[str, int, str, str]] = []
    last_send_keys: str = ""
    calls: list[tuple[str, ...]] = []

    def schedule(command_starts_with: str, rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        schedules.append((command_starts_with, rc, stdout, stderr))

    async def _side_effect(*args: str) -> tuple[int, str, str]:
        nonlocal last_send_keys
        calls.append(args)
        cmd = args[0] if args else ""
        if cmd == "send-keys":
            try:
                li = args.index("-l")
                end = len(args) - 1 if args and args[-1] == "Enter" else len(args)
                last_send_keys = " ".join(args[li + 1 : end])
            except ValueError:
                pass
        for needle, rc, stdout, stderr in reversed(schedules):
            if cmd.startswith(needle):
                if "{marker}" in stdout:
                    m = re.search(r"__AGENT_DONE_([0-9a-f]{8})__", last_send_keys)
                    if m:
                        stdout = stdout.replace("{marker}", m.group(0))
                return (rc, stdout, stderr)
        return (0, "", "")

    mock = _side_effect
    mock.schedule = schedule
    mock.calls = calls
    return mock


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-agent-token-xyz"}


class TestHealthz(_Base):
    def test_returns_ok(self) -> None:
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")
        self.assertEqual(r.json()["sessions"], 0)
        self.assertEqual(r.json()["workspace"], self.workspace_real)


class TestCreateSessionRejectsBadCwd(_Base):
    """Path-traversal protection — the headline security fix."""

    def test_absolute_path_outside_workspace_rejected(self) -> None:
        r = self.client.post(
            "/sessions", json={"session_id": "x", "cwd": "/etc"}, headers=self.auth
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("outside workspace", r.json()["detail"])

    def test_dotdot_traversal_rejected(self) -> None:
        r = self.client.post(
            "/sessions",
            json={"session_id": "x", "cwd": str(Path(self.tmpdir) / ".." / "etc")},
            headers=self.auth,
        )
        self.assertEqual(r.status_code, 400)

    def test_nonexistent_path_rejected(self) -> None:
        r = self.client.post(
            "/sessions", json={"session_id": "x", "cwd": "/this/does/not/exist"}, headers=self.auth
        )
        self.assertEqual(r.status_code, 400)

    def test_valid_cwd_accepted(self) -> None:
        mock = _make_tmux_mock()
        mock.schedule("has-session", rc=1)  # not found
        mock.schedule("new-session", rc=0)  # success
        with patch("agent_api._tmux", new=mock):
            r = self.client.post(
                "/sessions",
                json={"session_id": "valid", "cwd": self.workspace_real},
                headers=self.auth,
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertFalse(r.json()["existed"])


class TestCreateSessionHappyPath(_Base):
    def test_create_then_attach_returns_existed(self) -> None:
        mock = _make_tmux_mock()
        mock.schedule("has-session", rc=1)  # not found (first call)
        mock.schedule("new-session", rc=0)
        with patch("agent_api._tmux", new=mock):
            r1 = self.client.post(
                "/sessions",
                json={"session_id": "abc", "cwd": self.workspace_real},
                headers=self.auth,
            )
            self.assertEqual(r1.status_code, 200, r1.text)
            self.assertFalse(r1.json()["existed"])

        mock2 = _make_tmux_mock()
        mock2.schedule("has-session", rc=0)  # exists
        with patch("agent_api._tmux", new=mock2):
            r2 = self.client.post(
                "/sessions",
                json={"session_id": "abc", "cwd": self.workspace_real},
                headers=self.auth,
            )
            self.assertEqual(r2.status_code, 200)
            self.assertTrue(r2.json()["existed"])


class TestExecAutoCreatesSession(_Base):
    def test_exec_with_no_session_auto_creates(self) -> None:
        # The auto-create path runs synchronously inside the request, so
        # the sequence is: has-session, new-session, send-keys, capture-pane
        # (which must contain the real marker). Use {marker} placeholder.
        mock = _make_tmux_mock()
        mock.schedule("has-session", rc=1)  # not found
        mock.schedule("new-session", rc=0)
        mock.schedule("send-keys", rc=0)
        # Template — ``{marker}`` is replaced with whatever was sent above.
        mock.schedule("capture-pane", stdout="$ pwd\n/workspace\n\n{marker}=0\n")
        with patch("agent_api._tmux", new=mock):
            r = self.client.post(
                "/sessions/auto-1/exec", json={"command": "pwd", "timeout": 5}, headers=self.auth
            )
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertFalse(body["timed_out"], body)
            self.assertEqual(body["exit_code"], 0)
            self.assertIn("/workspace", body["stdout"])
            self.assertNotIn("__AGENT_DONE_", body["stdout"])


class TestExecTimeout(_Base):
    def test_timeout_returns_partial_output(self) -> None:
        import agent_api  # noqa: PLC0415

        original = agent_api.DEFAULT_TIMEOUT
        agent_api.DEFAULT_TIMEOUT = 1
        self.addCleanup(lambda: setattr(agent_api, "DEFAULT_TIMEOUT", original))

        mock = _make_tmux_mock()
        mock.schedule("has-session", rc=0)  # session exists
        mock.schedule("send-keys", rc=0)
        mock.schedule("capture-pane", stdout="partial output without marker")
        with patch("agent_api._tmux", new=mock):
            r = self.client.post(
                "/sessions/to-1/exec", json={"command": "sleep 99", "timeout": 1}, headers=self.auth
            )
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body["timed_out"])
            self.assertIsNone(body["exit_code"])


class TestSessionPersistence(_Base):
    def test_sessions_survive_simulated_restart(self) -> None:
        mock = _make_tmux_mock()
        mock.schedule("has-session", rc=1)
        mock.schedule("new-session", rc=0)
        with patch("agent_api._tmux", new=mock):
            r = self.client.post(
                "/sessions",
                json={"session_id": "persist-1", "cwd": self.workspace_real},
                headers=self.auth,
            )
            self.assertEqual(r.status_code, 200, r.text)

        sessions_file = Path(os.environ["AGENT_SESSIONS_FILE"])
        self.assertTrue(sessions_file.exists(), "SESSIONS file was not written")
        payload = json.loads(sessions_file.read_text())
        self.assertIn("persist-1", payload)

        # Simulate restart: clear in-memory dict, then call _load_sessions.
        self.sessions.clear()
        from agent_api import _load_sessions  # noqa: PLC0415

        reloaded = _load_sessions()
        self.assertIn("persist-1", reloaded)
        self.assertEqual(reloaded["persist-1"]["cwd"], self.workspace_real)


class TestDeleteSession(_Base):
    def test_delete_unknown_returns_deleted_marker(self) -> None:
        mock = _make_tmux_mock()
        mock.schedule("kill-session", rc=0)
        with patch("agent_api._tmux", new=mock):
            r = self.client.delete("/sessions/does-not-exist", headers=self.auth)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["deleted"], "does-not-exist")


class TestExecValidation(_Base):
    def test_empty_command_rejected_by_pydantic(self) -> None:
        r = self.client.post("/sessions/x/exec", json={"command": ""}, headers=self.auth)
        self.assertEqual(r.status_code, 422)

    def test_oversized_command_rejected(self) -> None:
        r = self.client.post(
            "/sessions/x/exec", json={"command": "a" * 2_000_000}, headers=self.auth
        )
        self.assertEqual(r.status_code, 422)

    def test_negative_timeout_rejected(self) -> None:
        r = self.client.post(
            "/sessions/x/exec", json={"command": "ls", "timeout": -1}, headers=self.auth
        )
        self.assertEqual(r.status_code, 422)

    def test_huge_timeout_rejected(self) -> None:
        r = self.client.post(
            "/sessions/x/exec", json={"command": "ls", "timeout": 99999}, headers=self.auth
        )
        self.assertEqual(r.status_code, 422)


class TestCaptureEndpoint(_Base):
    """The new /capture endpoint that the WebSocket polls instead of exec."""

    def test_capture_unknown_session_404(self) -> None:
        mock = _make_tmux_mock()
        mock.schedule("has-session", rc=1)  # not found
        with patch("agent_api._tmux", new=mock):
            r = self.client.get("/sessions/missing/capture", headers=self.auth)
            self.assertEqual(r.status_code, 404)

    def test_capture_returns_output(self) -> None:
        mock = _make_tmux_mock()
        mock.schedule("has-session", rc=0)
        mock.schedule("capture-pane", stdout="the output")
        with patch("agent_api._tmux", new=mock):
            r = self.client.get("/sessions/s1/capture?lines=50", headers=self.auth)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["output"], "the output")


if __name__ == "__main__":
    unittest.main()
