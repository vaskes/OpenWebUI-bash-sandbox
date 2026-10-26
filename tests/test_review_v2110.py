"""Tripwires for the v2.11.0 review wave.

The 2026-10-26 Qwen review of the whole repo produced a list of
findings. Each one that turned out to be real got a tripwire here, and
each tripwire was checked by mutation — a test that cannot be made red
is a decoration, not a check.

Nothing in this file tests the *implementation* of a fix; it tests the
*claim* the fix makes. The claims are written down in the commit
messages and in docs/SECURITY.md, and those are the things that rot.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("OPENAPI_PROXY_TOKEN", "test-token")
os.environ.setdefault("AGENT_TOKEN", "test-token")


# ---------------------------------------------------------------------------
# H1 — the command preview that goes into the log must not carry secrets
# ---------------------------------------------------------------------------
class TestSecretRedaction(unittest.TestCase):
    """The agent logs a preview of every command it runs. A command that
    carries a token must not write that token into the log file."""

    def setUp(self) -> None:
        from agent_core import redact_secrets  # noqa: PLC0415

        self._redact = redact_secrets

    def test_an_assignment_with_a_secret_name_is_redacted(self) -> None:
        out = self._redact("export OPENAI_API_KEY=sk-proj-abc123")
        self.assertNotIn("sk-proj-abc123", out)
        self.assertIn("OPENAI_API_KEY", out, "the name must survive — the log is an audit trail")
        self.assertIn("[REDACTED]", out)

    def test_a_camel_case_secret_name_is_redacted(self) -> None:
        out = self._redact("curl -H apiKey:abc123 https://example.com")
        self.assertNotIn("abc123", out)

    def test_a_password_flag_is_redacted(self) -> None:
        for command in (
            "mysql -uroot --password hunter2 db",
            "mysql -uroot --password=hunter2 db",
        ):
            out = self._redact(command)
            self.assertNotIn("hunter2", out, command)
            self.assertIn("--password", out, command)

    def test_an_authorization_header_is_redacted(self) -> None:
        out = self._redact('curl -H "Authorization: Bearer sk-abc123" https://api.example.com')
        self.assertNotIn("sk-abc123", out)
        self.assertIn("Authorization", out)

    def test_sshpass_password_is_redacted(self) -> None:
        out = self._redact("sshpass -p topsecret ssh host")
        self.assertNotIn("topsecret", out)

    def test_a_private_key_block_is_redacted(self) -> None:
        out = self._redact(
            "cat key.pem\n-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n"
            "-----END OPENSSH PRIVATE KEY-----"
        )
        self.assertNotIn("AAAA", out)
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", out)

    def test_innocent_words_are_not_redacted(self) -> None:
        """A redactor that eats everything is not a redactor. The name
        match is per *segment*, so ``monkey`` and ``KEYBOARD`` are not
        secrets."""
        for command in (
            "echo monkey=banana",
            "echo KEYBOARD=qwerty",
            "echo hello world",
            "git commit -m 'fix the parser'",
            "curl https://example.com/path?a=1&b=2",
        ):
            self.assertEqual(self._redact(command), command, command)

    def test_an_empty_string_is_untouched(self) -> None:
        self.assertEqual(self._redact(""), "")


class _AgentTestBase(unittest.TestCase):
    """Boots the agent app against a fake tmux and records what the
    endpoint typed into the terminal."""

    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        import tempfile  # noqa: PLC0415

        tmp = tempfile.mkdtemp(prefix="agent-test-")
        os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmp, "sessions.json")
        os.environ["AGENT_WORKSPACE"] = tmp
        os.environ["AGENT_TOKEN"] = "test-token"
        if "agent_api" in sys.modules:
            del sys.modules["agent_api"]
        from agent_api import SESSIONS, app  # noqa: E402

        SESSIONS.clear()
        self.client = TestClient(app)
        self.auth = {"Authorization": "Bearer test-token"}
        self.sent: list[str] = []

    def exec_command(self, command: str):
        """Run one command against the fake tmux. Returns the response."""
        sent = self.sent

        async def fake_tmux(*args):
            cmd = args[0]
            if cmd == "send-keys":
                sent.append(" ".join(str(a) for a in args))
                return (0, "", "")
            if cmd == "capture-pane":
                # Answer with the marker the endpoint actually sent, so the
                # exec finishes instead of running into the timeout.
                marker = ""
                for payload in sent:
                    match = re.search(r"__AGENT_DONE_[0-9a-f]+__", payload)
                    if match:
                        marker = match.group(0)
                return (0, f"$ echo hi\n{marker}=0\n", "")
            return (0, "", "")

        with patch("agent_api._tmux", new=fake_tmux):
            return self.client.post(
                "/sessions/x/exec",
                json={"command": command, "timeout": 5},
                headers=self.auth,
            )


class TestTheLoggedPreviewIsRedacted(_AgentTestBase):
    """The redaction must be on the path that actually logs — not only in
    a helper nobody calls."""

    def _exec(self, command: str) -> list[logging.LogRecord]:
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("sandbox-agent")
        old_level = logger.level
        logger.setLevel(logging.INFO)
        handler = _Capture()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, old_level)

        response = self.exec_command(command)
        self.assertEqual(response.status_code, 200, response.text)
        return records

    def test_the_secret_does_not_reach_the_log(self) -> None:
        records = self._exec("export API_KEY=sk-live-9f8e7d6c")
        messages = "\n".join(record.getMessage() for record in records)
        self.assertNotIn("sk-live-9f8e7d6c", messages)
        self.assertIn("[REDACTED]", messages)

    def test_the_command_is_still_auditable(self) -> None:
        """Redaction must not turn the audit trail into noise: the command
        is still named in the log."""
        records = self._exec("export API_KEY=sk-live-9f8e7d6c")
        messages = "\n".join(record.getMessage() for record in records)
        self.assertIn("API_KEY", messages)
        self.assertIn("cmd_len=", messages)


# ---------------------------------------------------------------------------
# M2 — the completion markers must not be guessable
# ---------------------------------------------------------------------------
class TestMarkerLength(_AgentTestBase):
    """The exec protocol names its bookkeeping lines in the terminal. A
    command that prints a line of its own must not be able to
    impersonate one. 32 bits was the width; 64 is the claim."""

    def test_the_marker_carries_at_least_64_bits(self) -> None:
        from agent_core import MARKER_HEX_LEN  # noqa: PLC0415

        self.assertGreaterEqual(MARKER_HEX_LEN, 16)

    def test_a_fresh_token_has_that_length(self) -> None:
        from agent_core import MARKER_HEX_LEN, new_marker_token  # noqa: PLC0415

        token = new_marker_token()
        self.assertEqual(len(token), MARKER_HEX_LEN)
        self.assertTrue(re.fullmatch(r"[0-9a-f]+", token))

    def test_two_tokens_differ(self) -> None:
        from agent_core import new_marker_token  # noqa: PLC0415

        self.assertNotEqual(new_marker_token(), new_marker_token())

    def test_the_command_carries_three_wide_markers(self) -> None:
        """The end-to-end claim: what the endpoint actually types into
        the terminal names all three markers with 16 hex characters."""
        response = self.exec_command("echo hello")
        self.assertEqual(response.status_code, 200, response.text)
        payload = "\n".join(self.sent)
        for prefix in ("__AGENT_DONE_", "__AGENT_BEGIN_", "__AGENT_EC_"):
            self.assertRegex(payload, rf"{re.escape(prefix)}[0-9a-f]{{16}}__")
        # And nothing narrower: an 8-hex marker would match the pattern
        # above only if the width grew, so pin the width explicitly.
        for prefix in ("__AGENT_DONE_", "__AGENT_BEGIN_", "__AGENT_EC_"):
            for token in re.findall(rf"{re.escape(prefix)}([0-9a-f]+)__", payload):
                self.assertEqual(len(token), 16, f"{prefix}{token}")
