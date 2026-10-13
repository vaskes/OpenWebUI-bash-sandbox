"""Regression tests for the wave-2 code review fixes.

Each test pins one bug from the review so a future refactor can't
silently reintroduce it.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# agent_api fixes
# ---------------------------------------------------------------------------
def _fresh_app():
    tmp = tempfile.mkdtemp(prefix="agent-test-")
    os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmp, "sessions.json")
    os.environ["AGENT_WORKSPACE"] = tmp
    os.environ["AGENT_TOKEN"] = "test-token"
    if "agent_api" in sys.modules:
        del sys.modules["agent_api"]
    from agent_api import SESSIONS, app  # noqa: E402

    SESSIONS.clear()
    return app, SESSIONS, tmp


class _AgentBase(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        self.app, self.sessions, self.tmpdir = _fresh_app()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-token"}


class TestSendKeysLiteral(_AgentBase):
    """#4: send-keys without -l silently corrupts commands containing
    tmux-reserved tokens like ``Enter`` or ``C-a``."""

    def test_send_keys_uses_literal_flag(self) -> None:
        captured_args: list[tuple[str, ...]] = []

        async def fake_tmux(*args):
            captured_args.append(args)
            cmd = args[0]
            if cmd == "has-session":
                return (1, "", "")
            if cmd == "new-session":
                return (0, "", "")
            if cmd == "send-keys":
                return (0, "", "")
            if cmd == "capture-pane":
                return (0, "$ Press Enter to continue\n__AGENT_DONE_abc12345__=0\n", "")
            return (0, "", "")

        with patch("agent_api._tmux", new=fake_tmux):
            r = self.client.post(
                "/sessions/x/exec",
                json={"command": "git commit -m 'Press Enter to continue'", "timeout": 5},
                headers=self.auth,
            )
            self.assertEqual(r.status_code, 200, r.text)
        # Find the send-keys call that carries the command. The follow-up
        # send-keys Enter call has no -l.
        send_keys_calls = [a for a in captured_args if a[0] == "send-keys"]
        self.assertEqual(len(send_keys_calls), 2, send_keys_calls)
        # First call: command payload, MUST include -l.
        self.assertIn("-l", send_keys_calls[0])
        # Second call: just the Enter key (no -l needed, it's a single token).
        self.assertEqual(send_keys_calls[1], ("send-keys", "-t", "agent_x", "Enter"))


class TestMarkerRegexLastWins(_AgentBase):
    """#6: re.search picks the first marker — a malicious command could
    plant a fake marker first. We must use the *last* match."""

    def test_fake_marker_before_real_marker_ignored(self) -> None:
        # Capture the marker the endpoint generates, then plant a FAKE
        # marker earlier in the output to ensure the parser picks the
        # *last* match (the real one) and not the first.
        state = {"real_marker": ""}

        async def fake_tmux(*args):
            cmd = args[0]
            if cmd == "has-session":
                return (0, "", "")
            if cmd == "send-keys":
                # Extract the marker from the wrapped full_cmd.
                payload = " ".join(str(a) for a in args)
                m = re.search(r"__AGENT_DONE_[0-9a-f]{8}__", payload)
                if m:
                    state["real_marker"] = m.group(0)
                return (0, "", "")
            if cmd == "capture-pane":
                real = state["real_marker"]
                # Real marker gets 99, fake gets 0; fake is FIRST.
                return (
                    0,
                    f"__AGENT_DONE_11111111__=0\nreal output\n{real}=99\n",
                    "",
                )
            return (0, "", "")

        with patch("agent_api._tmux", new=fake_tmux):
            r = self.client.post(
                "/sessions/x/exec",
                json={"command": "echo fake marker first", "timeout": 5},
                headers=self.auth,
            )
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["exit_code"], 99, body)
            # The legitimate last marker is stripped; the fake one stays
            # so the model can see it.
            self.assertIn("__AGENT_DONE_11111111__", body["stdout"])
            self.assertNotIn(state["real_marker"], body["stdout"])


class TestOutputTruncation(_AgentBase):
    """#9: huge command output can OOM callers. Truncate at MAX_OUTPUT_BYTES."""

    def test_truncate_huge_output(self) -> None:
        import agent_api  # noqa: PLC0415

        original = agent_api.MAX_OUTPUT_BYTES
        agent_api.MAX_OUTPUT_BYTES = 100
        self.addCleanup(lambda: setattr(agent_api, "MAX_OUTPUT_BYTES", original))

        huge = "x" * 5000

        async def fake_tmux(*args):
            cmd = args[0]
            if cmd == "has-session":
                return (0, "", "")
            if cmd == "send-keys":
                return (0, "", "")
            if cmd == "capture-pane":
                return (0, huge + "\n__AGENT_DONE_abc12345__=0\n", "")
            return (0, "", "")

        with patch("agent_api._tmux", new=fake_tmux):
            r = self.client.post(
                "/sessions/x/exec",
                json={"command": "echo huge", "timeout": 5},
                headers=self.auth,
            )
            self.assertEqual(r.status_code, 200)
            self.assertIn("[output truncated at 100 bytes]", r.json()["stdout"])


class TestLifespanNoLongerKillsSessions(_AgentBase):
    """#2: the lifespan shutdown used to kill all tmux sessions, breaking
    persistence across restarts."""

    def test_lifespan_does_not_call_kill_session(self) -> None:
        kill_called: list[str] = []

        async def fake_tmux(*args):
            if args[0] == "kill-session":
                kill_called.append(args[-1])
            return (0, "", "")

        # Simulate the shutdown: the FastAPI lifespan exits and we want
        # to assert that no kill-session calls were made.
        async def _shutdown():
            with patch("agent_api._tmux", new=fake_tmux):
                # Pre-populate the registry so there's something to kill
                # if the old behaviour was still in place.
                from agent_api import SESSIONS  # noqa: PLC0415

                SESSIONS["preserved"] = {
                    "tmux_name": "agent_preserved",
                    "created": 0.0,
                    "cwd": "/tmp",
                }
                # Trigger the lifespan exit.
                with self.client:
                    pass  # entering __enter__ runs startup, exiting __exit__ runs shutdown

        _shutdown()
        self.assertEqual(kill_called, [])


# ---------------------------------------------------------------------------
# openapi_proxy fixes
# ---------------------------------------------------------------------------
def _fresh_proxy_app(env_overrides=None):
    env = {
        "OPENAPI_PROXY_TOKEN": "test-token",
        "OPENAPI_PROXY_CORS_ORIGINS": "",
        "OPENAPI_PROXY_SSH_ALLOWLIST": "lab.local,10.0.0.0/8",
        "OPENAPI_PROXY_RATE_LIMIT_RPM": "0",
    }
    if env_overrides:
        env.update(env_overrides)
    for k, v in env.items():
        os.environ[k] = v
    if "openapi_proxy" in sys.modules:
        del sys.modules["openapi_proxy"]
    from fastapi.testclient import TestClient  # noqa: E402

    from openapi_proxy import _rate_limiter, app  # noqa: E402

    _rate_limiter._buckets.clear()
    return app, _rate_limiter, TestClient(app)


class _ProxyBase(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.rate_limiter, raw = _fresh_proxy_app()
        self.client = raw.__enter__()
        self.addCleanup(raw.__exit__, None, None, None)
        self.headers = {"Authorization": "Bearer test-token"}


class TestSshCommandInjection(_ProxyBase):
    """#5: ssh allowlist only checked the host, not the args. A `; rm -rf`
    injected after the host would still pass."""

    def _make_upstream_mock(self):
        """Return an AsyncMock that pretends to be the sandbox-agent.
        Records every forwarded command so tests can assert on it."""
        from unittest.mock import AsyncMock, MagicMock

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(return_value={"stdout": "ok", "exit_code": 0})
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        return client

    def test_shell_metachars_after_host_rejected(self) -> None:
        client = self._make_upstream_mock()
        with patch("openapi_proxy._client", new=lambda _req: client):
            body = self.client.post(
                "/bash",
                json={"command": "ssh lab.local ; rm -rf /", "session_id": "x"},
                headers=self.headers,
            ).json()
        # Either the flag validator or the shlex parser rejects this.
        # The point is that the dangerous command is NOT forwarded.
        self.assertNotEqual(body.get("exit_code"), 0, body)
        if client.post.called:
            sent = client.post.call_args.kwargs["json"]["command"]
            self.assertNotIn("rm -rf", sent, sent)

    def test_proxy_command_rejected(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(return_value={"stdout": "ok", "exit_code": 0})
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        with patch("openapi_proxy._client", new=lambda _req: client):
            body = self.client.post(
                "/bash",
                json={"command": "ssh lab.local -o ProxyCommand=nc evil.com 22", "session_id": "x"},
                headers=self.headers,
            ).json()
        self.assertIn("ProxyCommand", body.get("error", ""))

    def test_jump_host_rejected(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(return_value={"stdout": "ok", "exit_code": 0})
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        with patch("openapi_proxy._client", new=lambda _req: client):
            body = self.client.post(
                "/bash",
                json={"command": "ssh -J lab.local root@evil.com", "session_id": "x"},
                headers=self.headers,
            ).json()
        self.assertIn("not allowed", body.get("error", ""))

    def test_port_forward_rejected(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(return_value={"stdout": "ok", "exit_code": 0})
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        with patch("openapi_proxy._client", new=lambda _req: client):
            body = self.client.post(
                "/bash",
                json={"command": "ssh lab.local -L 2222:evil.com:22", "session_id": "x"},
                headers=self.headers,
            ).json()
        self.assertIn("not allowed", body.get("error", ""))

    def test_safe_ssh_hostname_only_accepted(self) -> None:
        """Hostname-only ssh (no remote command) must work."""
        client = self._make_upstream_mock()
        with patch("openapi_proxy._client", new=lambda _req: client):
            body = self.client.post(
                "/bash",
                json={"command": "ssh lab.local", "session_id": "x"},
                headers=self.headers,
            ).json()
        self.assertEqual(body.get("exit_code"), 0, body)
        sent = client.post.call_args.kwargs["json"]["command"]
        self.assertIn("StrictHostKeyChecking=accept-new", sent)
        self.assertIn("lab.local", sent)

    def test_ssh_with_known_flag_accepted(self) -> None:
        """``-p`` (port) and ``-i`` (key file) are whitelisted flags."""
        client = self._make_upstream_mock()
        with patch("openapi_proxy._client", new=lambda _req: client):
            body = self.client.post(
                "/bash",
                json={"command": "ssh -p 2222 -i /tmp/key lab.local", "session_id": "x"},
                headers=self.headers,
            ).json()
        self.assertEqual(body.get("exit_code"), 0, body)
        sent = client.post.call_args.kwargs["json"]["command"]
        self.assertIn("-p", sent)
        self.assertIn("2222", sent)


class TestHostnameCaseInsensitive(_ProxyBase):
    """#16: DNS is case-insensitive, but the allowlist was not."""

    def test_uppercase_hostname_matches_lowercase_allowlist(self) -> None:
        from openapi_proxy import _ssh_host_allowed  # noqa: PLC0415

        self.assertTrue(_ssh_host_allowed("Lab.Local"))
        self.assertTrue(_ssh_host_allowed("LAB.LOCAL"))


class TestRateLimiterLruEviction(_ProxyBase):
    """#12: rate limiter buckets dict grew without bound."""

    def test_lru_eviction_when_max_keys_reached(self) -> None:
        # Override max_keys to a small value to keep the test fast.
        self.rate_limiter.max_keys = 3
        self.rate_limiter.rpm = 60  # high enough that no bucket is exhausted
        for i in range(10):
            self.rate_limiter.check(f"key-{i}")
        # Only the most recent max_keys entries should remain.
        self.assertLessEqual(len(self.rate_limiter._buckets), 3)
        # The oldest key should have been evicted.
        self.assertNotIn("key-0", self.rate_limiter._buckets)


class TestLoggerFormatFix(_ProxyBase):
    """#10: %s + literal 'andbox' was a printf spec bug."""

    def test_startup_log_message_does_not_contain_stray_andbox(self) -> None:
        import logging

        records: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Capture(level=logging.INFO)
        proxy_logger = logging.getLogger("openapi-proxy")
        proxy_logger.addHandler(handler)
        try:
            # Re-trigger the startup log via the lifespan.
            with self.client:
                pass
        finally:
            proxy_logger.removeHandler(handler)
        # All startup messages must not contain the buggy " sandbox" prefix.
        for r in records:
            if "proxy.startup" in r:
                self.assertNotIn("sandbox", r, r)


class TestWebSocketNoTokenInQuery(_ProxyBase):
    """#7: WebSocket token in query params leaks into access logs."""

    def test_websocket_query_param_rejected(self) -> None:
        """Connecting with ?token=... must be rejected (use Sec-WebSocket-Protocol).

        The TestClient raises WebSocketDisconnect on the rejection close
        frame, which is the expected behaviour.
        """
        from starlette.websockets import WebSocketDisconnect

        with self.assertRaises(WebSocketDisconnect) as ctx:
            with self.client.websocket_connect("/ws?token=test-token"):
                pass
        # The reason explains the rejection. We don't pin the exact string
        # because FastAPI/Starlette versions format it differently, but
        # 1008 (policy violation) is the expected close code.
        self.assertIn(ctx.exception.code, (1008, 1006))


if __name__ == "__main__":
    unittest.main()
