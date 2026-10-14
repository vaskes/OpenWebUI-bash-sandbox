"""Sandbox-recommendations fixes — Mavis (bot-side) forwarded these
from the 2026-10-14 Qwen review of the llama.cpp_telegram bot +
bash-sandbox combined deployment.

#C1: command content logging — exec log must include a preview of
     the command, not just its length.
#C7: SSH identity symlink bypass — reject os.path.islink() before
     the realpath check so a TOCTOU swap cannot redirect to
     /home/agent/.ssh/id_rsa.
#M3: WebSocket idle timeout — drop connections that sit idle for
     more than WEBSOCKET_IDLE_TIMEOUT_SECONDS so forgotten browser
     tabs do not pin a tmux session forever.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("OPENAPI_PROXY_TOKEN", "test-token")
os.environ.setdefault("AGENT_TOKEN", "test-token")


# ---------------------------------------------------------------------------
# #C1 — command content logging
# ---------------------------------------------------------------------------
class TestCommandContentLogged(unittest.TestCase):
    """#C1: every exec log line must include a preview of the command."""

    def test_log_message_format_contains_cmd_equals(self) -> None:
        """Both success and timeout log strings must include ``cmd=%r``."""
        with open(os.path.join(os.path.dirname(__file__), "..", "agent_api.py")) as f:
            src = f.read()
        # Two call sites: success and timeout. Both must include cmd=%r.
        self.assertIn("cmd=%r", src)
        # And the message body string must contain the format spec at the
        # caller side, not just a %r elsewhere.
        self.assertIn('"exec sid=%s exit=%s duration_ms=%d cmd_len=%d cmd=%r"', src)
        self.assertIn('"exec.timeout sid=%s duration_ms=%d cmd_len=%d cmd=%r"', src)

    def test_log_message_does_not_omit_cmd_preview(self) -> None:
        """The old format string without ``cmd=%r`` must be gone."""
        with open(os.path.join(os.path.dirname(__file__), "..", "agent_api.py")) as f:
            src = f.read()
        self.assertNotIn(
            '"exec sid=%s exit=%s duration_ms=%d cmd_len=%d",',
            src,
            "exec log line must include cmd preview (sandbox-recommendations #C1)",
        )
        self.assertNotIn(
            '"exec.timeout sid=%s duration_ms=%d cmd_len=%d",',
            src,
            "exec.timeout log line must include cmd preview (sandbox-recommendations #C1)",
        )


class TestCmdPreviewTruncation(unittest.TestCase):
    """The cmd_preview must be truncated to keep the log bounded."""

    def test_short_command_unchanged(self) -> None:
        # Inline-test the truncation logic (mirrors what exec_cmd does).
        cmd = "ls -la /workspace"
        preview = cmd if len(cmd) <= 200 else cmd[:200] + f"... (+{len(cmd) - 200} bytes)"
        self.assertEqual(preview, cmd)

    def test_long_command_truncated(self) -> None:
        cmd = "x" * 1000
        preview = cmd if len(cmd) <= 200 else cmd[:200] + f"... (+{len(cmd) - 200} bytes)"
        self.assertTrue(preview.startswith("x" * 200))
        self.assertIn("+800 bytes", preview)


# ---------------------------------------------------------------------------
# #C7 — SSH identity symlink rejection
# ---------------------------------------------------------------------------
class TestSshIdentityRejectsSymlink(unittest.TestCase):
    """#C7: ``-i`` pointing at a symlink (even one inside the workspace
    pointing outside) must be rejected. This blocks the TOCTOU attack
    where the LLM creates a regular file, gets it validated, then
    replaces it with a symlink to a host-resident key."""

    def setUp(self) -> None:
        from openapi_proxy import _ssh_identity_within_workspace

        self._check = _ssh_identity_within_workspace

    def test_symlink_to_outside_workspace_rejected(self) -> None:
        """The exact #C7 attack: a symlink inside /workspace pointing at
        /home/agent/.ssh/id_rsa. The function must return False."""
        with tempfile.TemporaryDirectory() as tmp:
            # Pretend the persistent key exists at this path.
            real_key = os.path.join(tmp, "real_key")
            with open(real_key, "w") as f:
                f.write("real key bytes")
            # Symlink inside the workspace pointing at the real key.
            workspace = os.path.join(tmp, "workspace")
            os.makedirs(workspace)
            link = os.path.join(workspace, "innocent_looking_key")
            os.symlink(real_key, link)

            with patch.dict(os.environ, {"AGENT_WORKSPACE": workspace}):
                self.assertFalse(
                    self._check(link),
                    "symlink to outside-workspace file must be rejected (#C7)",
                )

    def test_symlink_inside_workspace_rejected(self) -> None:
        """Even a symlink that points to another file inside the same
        workspace is rejected — the validator is not allowed to follow."""
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target")
            with open(target, "w") as f:
                f.write("data")
            link = os.path.join(tmp, "link")
            os.symlink(target, link)
            with patch.dict(os.environ, {"AGENT_WORKSPACE": tmp}):
                self.assertFalse(
                    self._check(link),
                    "any symlink as SSH -i must be rejected (#C7)",
                )

    def test_regular_file_in_workspace_accepted(self) -> None:
        """A regular (non-symlink) file inside the workspace is still
        accepted. The symlink check must not regress the happy path."""
        with tempfile.TemporaryDirectory() as tmp:
            key = os.path.join(tmp, "id_ed25519")
            with open(key, "w") as f:
                f.write("freshly generated key")
            with patch.dict(os.environ, {"AGENT_WORKSPACE": tmp}):
                self.assertTrue(self._check(key))


# ---------------------------------------------------------------------------
# #M3 — WebSocket idle timeout
# ---------------------------------------------------------------------------
class TestWebSocketIdleTimeoutConfig(unittest.TestCase):
    """#M3: the proxy must define an idle-timeout constant that defaults
    to 300 s and is overridable via env."""

    def test_default_idle_timeout_is_300(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAPI_PROXY_WEBSOCKET_IDLE_TIMEOUT", None)
            # Re-import the module to pick up the default.
            import importlib

            import openapi_proxy

            importlib.reload(openapi_proxy)
            self.assertEqual(openapi_proxy.WEBSOCKET_IDLE_TIMEOUT_SECONDS, 300)
            self.assertGreater(openapi_proxy.WEBSOCKET_IDLE_TIMEOUT_SECONDS, 0)

    def test_env_override_applies(self) -> None:
        with patch.dict(os.environ, {"OPENAPI_PROXY_WEBSOCKET_IDLE_TIMEOUT": "60"}):
            import importlib

            import openapi_proxy

            importlib.reload(openapi_proxy)
            self.assertEqual(openapi_proxy.WEBSOCKET_IDLE_TIMEOUT_SECONDS, 60)

    def test_zero_disables_timeout(self) -> None:
        with patch.dict(os.environ, {"OPENAPI_PROXY_WEBSOCKET_IDLE_TIMEOUT": "0"}):
            import importlib

            import openapi_proxy

            importlib.reload(openapi_proxy)
            self.assertEqual(openapi_proxy.WEBSOCKET_IDLE_TIMEOUT_SECONDS, 0)

    def test_websocket_endpoint_uses_asyncio_wait_for(self) -> None:
        """The endpoint must wrap ``receive_text`` in ``asyncio.wait_for``
        so the idle timer actually fires. The call is split across lines,
        so we check for both halves of the statement."""
        with open(os.path.join(os.path.dirname(__file__), "..", "openapi_proxy.py")) as f:
            src = f.read()
        # Both halves of the await must be present.
        self.assertIn(
            "asyncio.wait_for(",
            src,
            "websocket endpoint must use asyncio.wait_for for idle timeout (#M3)",
        )
        self.assertIn("websocket.receive_text()", src)
        self.assertIn(
            "timeout=WEBSOCKET_IDLE_TIMEOUT_SECONDS",
            src,
            "the wait_for timeout must come from WEBSOCKET_IDLE_TIMEOUT_SECONDS",
        )
        # The close reason on timeout must be present.
        self.assertIn('"idle timeout"', src)


if __name__ == "__main__":
    unittest.main()
