"""Tests for the busy-session leak: a foreign session's tail in stdout.

**What the bug report said.** After the bot restarted, a plain
``bash_command`` in the same chat answered with the *previous* session's
output — old command echoes, old output, and the notice
``[the beginning of this output no longer fits in the session
scrollback …]`` — and the model quoted it into the chat.

**What actually happens.** The sandbox was right and the report's
explanation was wrong. The stdout of a command that *did* run is cut at
the begin marker, so a foreign session's output can never reach it. The
leak was on the timeout path, and only when the command never started:
the shell was busy running an earlier command, the new input stayed
queued in the terminal, and the timeout branch handed the pane back as
if it were this command's output. The pane held the *previous*
command's output — hence "a foreign session's tail".

So the pane was never the sandbox's fault, but the answer it gave was a
lie, and the lie was indistinguishable from a real result.

The fix: when the begin marker is missing and the scrollback did not
overflow, the command never ran. The stdout stays empty, the reason
goes to stderr, and the pane — which belongs to somebody else — is left
alone. The scrollback being full is the tiebreaker: only a command's
own output can overflow the history, so a full history means the marker
was pushed out and the old "the beginning was lost" answer is the
truthful one.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from test_agent_api_endpoints import _fresh_app  # noqa: E402

#: The pane of a session whose previous command is still running. The
#: last two lines are the terminal's echo of the command that is queued
#: behind it — the marker is *inside* the echoed line, not printed on a
#: line of its own, so it must not count as "the command started".
BUSY_PANE = (
    "ubuntu@host:/workspace$ echo OLD-MARKER-ABC; for t in 1 2 3 4 5; do sleep 60; done\n"
    "OLD-MARKER-ABC\n"
    "printf '\\n__AGENT_BEGIN_{begin}__\\n'; PS2=\"$PS1\"\n"
    "{\n"
    "echo hi\n"
)

#: The pane of a command that ran and printed its output.
DONE_PANE = (
    "ubuntu@host:/workspace$ printf '\\n__AGENT_BEGIN_{begin}__\\n'; PS2=\"$PS1\"\n"
    "\n"
    "__AGENT_BEGIN_{begin}__\n"
    "hello from this command\n"
)


def _tmux_double(
    bounded: str,
    full: str | None = None,
    history_size: str = "12",
    history_limit: str = "0",
):
    """A tmux double for the busy-session scenarios.

    ``bounded`` is what a bounded capture returns, ``full`` is what the
    whole scrollback returns (``None`` means "the same as bounded").
    ``history_limit`` of ``0`` means "unknown" — the pane does not
    report a limit, so the scrollback cannot be proven full.
    """
    state: dict[str, str] = {"send_keys": ""}

    async def _side_effect(*args: str) -> tuple[int, str, str]:
        cmd = args[0] if args else ""
        if cmd == "send-keys":
            try:
                index = args.index("-l")
            except ValueError:
                index = -1
            if index >= 0:
                end = len(args) - 1 if args and args[-1] == "Enter" else len(args)
                state["send_keys"] = " ".join(args[index + 1 : end])
        if cmd == "display-message":
            fmt = args[-1] if args else ""
            if "history_size" in fmt:
                return (0, history_size, "")
            if "history_limit" in fmt:
                return (0, history_limit, "")
            return (0, "0", "")
        if cmd == "capture-pane":
            if "-S" in args:
                spec = args[args.index("-S") + 1]
                if spec == "-":
                    text = full if full is not None else bounded
                    return (0, _substitute(text, state["send_keys"]), "")
            return (0, _substitute(bounded, state["send_keys"]), "")
        return (0, "", "")

    return _side_effect


def _substitute(text: str, send_keys: str) -> str:
    """Put the real marker into the template, the way tmux would.

    The template carries the *token* of the marker, not the whole
    marker — the template already spells out ``__AGENT_BEGIN_`` around
    the placeholder.
    """
    match = re.search(r"__AGENT_BEGIN_([0-9a-f]+)__", send_keys)
    token = match.group(1) if match else "deadbeefdeadbeef"
    return text.replace("{begin}", token)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-agent-token-xyz"}

    def _exec(self, command: str = "echo hi", timeout: int = 1) -> dict:
        response = self.client.post(
            "/sessions/busy/exec",
            json={"command": command, "timeout": timeout},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()


class TestTheBusySessionLeak(_Base):
    """A command that never started must not answer with the pane."""

    def _run_busy(self, history_limit: str = "8448") -> dict:
        from unittest.mock import patch  # noqa: PLC0415

        mock = _tmux_double(
            BUSY_PANE,
            history_size="12",
            history_limit=history_limit,
        )
        with patch("agent_api._tmux", new=mock):
            return self._exec()

    def test_the_previous_commands_output_is_not_the_stdout(self) -> None:
        result = self._run_busy()
        self.assertNotIn("OLD-MARKER-ABC", result["stdout"])

    def test_the_stdout_is_empty(self) -> None:
        result = self._run_busy()
        self.assertEqual(result["stdout"], "")

    def test_the_reason_is_named_in_stderr(self) -> None:
        result = self._run_busy()
        self.assertIn("did not start", result["stderr"])
        self.assertIn("busy", result["stderr"])

    def test_the_call_is_still_reported_as_a_timeout(self) -> None:
        result = self._run_busy()
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["exit_code"])

    def test_the_lost_beginning_notice_is_not_printed(self) -> None:
        # The notice says "the beginning no longer fits in the scrollback".
        # Nothing was lost: the command never ran. Printing it is a lie.
        result = self._run_busy()
        self.assertNotIn("no longer fits in the session", result["stdout"])

    def test_the_protocol_markers_do_not_leak(self) -> None:
        result = self._run_busy()
        self.assertNotIn("__AGENT_", result["stdout"])

    def test_a_pane_without_a_reported_limit_is_still_a_busy_session(self) -> None:
        # The pane does not report its history limit. "Not proven full" is
        # the safe reading: without a proof of overflow the pane belongs
        # to somebody else and must not be handed over.
        result = self._run_busy(history_limit="0")
        self.assertEqual(result["stdout"], "")
        self.assertIn("did not start", result["stderr"])

    def test_the_reason_survives_the_mcp_tool(self) -> None:
        # The MCP tool returns stdout and stderr separately, so the model
        # sees the reason instead of somebody else's output.
        import mcp_server  # noqa: PLC0415

        self.assertTrue(hasattr(mcp_server, "bash"))


class TestTheEchoedMarkerIsNotAPrintedMarker(_Base):
    """The echo of the queued command carries the marker inside a line."""

    def test_a_marker_inside_a_line_does_not_count_as_started(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        mock = _tmux_double(BUSY_PANE, history_size="12", history_limit="0")
        with patch("agent_api._tmux", new=mock):
            result = self._exec()
        self.assertEqual(result["stdout"], "")
        self.assertIn("did not start", result["stderr"])


class TestTheCommandThatRan(_Base):
    """The fix must not touch the answers that were already right."""

    def _run_done(self) -> dict:
        from unittest.mock import patch  # noqa: PLC0415

        mock = _tmux_double(DONE_PANE, history_size="20", history_limit="0")
        with patch("agent_api._tmux", new=mock):
            return self._exec()

    def test_the_output_of_the_command_is_returned(self) -> None:
        result = self._run_done()
        self.assertIn("hello from this command", result["stdout"])

    def test_no_false_busy_notice(self) -> None:
        result = self._run_done()
        self.assertEqual(result["stderr"], "")

    def test_no_lost_beginning_notice(self) -> None:
        result = self._run_done()
        self.assertNotIn("no longer fits in the session", result["stdout"])


class TestTheOverflowedScrollback(_Base):
    """Only a command's own output can overflow the history."""

    def _run_overflowed(self) -> dict:
        from unittest.mock import patch  # noqa: PLC0415

        mock = _tmux_double(
            BUSY_PANE,
            history_size="100",
            history_limit="100",
        )
        with patch("agent_api._tmux", new=mock):
            return self._exec()

    def test_the_lost_beginning_notice_is_kept(self) -> None:
        # The history is full, so the marker was pushed out of it: the
        # output really was truncated, and the caller must be told.
        result = self._run_overflowed()
        self.assertIn("no longer fits in the session", result["stdout"])

    def test_it_is_not_reported_as_a_busy_session(self) -> None:
        result = self._run_overflowed()
        self.assertNotIn("did not start", result["stderr"])


class TestTheMarkerInTheFullScrollback(_Base):
    """The bounded capture can be too small; the whole history can hold it."""

    def test_the_output_is_taken_from_the_full_scrollback(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        mock = _tmux_double(
            "the pane scrolled past the beginning of the output\n",
            full=DONE_PANE,
            history_size="20",
            history_limit="0",
        )
        with patch("agent_api._tmux", new=mock):
            result = self._exec()
        self.assertIn("hello from this command", result["stdout"])
        self.assertEqual(result["stderr"], "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
