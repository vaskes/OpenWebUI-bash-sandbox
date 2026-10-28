"""Tests for the stdin handoff.

One defect, and a second one of the same family.

**A command that reads stdin used to eat the protocol.** The agent types
the command and the bookkeeping lines that follow it into the same
terminal, so the rest of the block is still sitting in the input buffer
when the command starts. A command that reads stdin — ``read x``,
``apt-get`` — reads those lines instead of waiting, and the completion
marker never appears. The caller waited out the whole limit and got
nothing back.

**The startup reaper formatted a list with ``%d``.** That raised inside
the logging machinery, which swallowed the message and printed a
formatting error instead of the line that says which sessions were
dropped.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_core import (  # noqa: E402
    CONTINUATION_PROMPT,
    build_exec_block,
    command_can_be_grouped,
    unquoted_standalone_braces,
)

BEGIN = "__AGENT_BEGIN_deadbeef__"
EC = "__AGENT_EC_deadbeef__"
DONE = "__AGENT_DONE_deadbeef__"


# ---------------------------------------------------------------------------
# The block the agent types
# ---------------------------------------------------------------------------
class TestTheCommandCannotEatTheProtocol(unittest.TestCase):
    """The command must not be able to read the bookkeeping lines."""

    def test_the_command_runs_with_its_stdin_closed(self) -> None:
        block, grouped = build_exec_block("read x", BEGIN, EC, DONE)
        self.assertTrue(grouped)
        self.assertIn("} < /dev/null", block)
        # The redirect closes stdin for the whole group, so the command
        # reads EOF instead of the lines that follow it.
        self.assertLess(block.index("read x"), block.index("} < /dev/null"))

    def test_the_protocol_follows_the_command(self) -> None:
        block, _ = build_exec_block("read x", BEGIN, EC, DONE)
        # The completion marker is printed after the command, and the exit
        # code is captured between them.
        self.assertLess(block.index("read x"), block.index(f"{EC}=$?"))
        self.assertLess(block.index(f"{EC}=$?"), block.index(DONE))
        self.assertTrue(block.rstrip().endswith(f"printf '\\n{DONE}=%d\\n' \"${{{EC}}}\""))

    def test_the_exit_code_is_captured_after_the_group(self) -> None:
        block, _ = build_exec_block("read x", BEGIN, EC, DONE)
        # The group is closed and redirected first; the status of the
        # group is the status of the command inside it.
        self.assertIn(f"}} < /dev/null; {EC}=$?", block)

    def test_a_standalone_brace_blocks_the_group(self) -> None:
        # A `}` that stands alone as a word would close our group early
        # and turn the rest of the command into a different command.
        block, grouped = build_exec_block("echo }", BEGIN, EC, DONE)
        self.assertFalse(grouped)
        self.assertNotIn("} < /dev/null", block)

    def test_an_unbalanced_brace_blocks_the_group(self) -> None:
        block, grouped = build_exec_block("echo {", BEGIN, EC, DONE)
        self.assertFalse(grouped)
        self.assertNotIn("} < /dev/null", block)

    def test_a_quoted_brace_does_not_block_the_group(self) -> None:
        # Inside quotes a brace is text, and bash reads it as text.
        _, grouped = build_exec_block("echo '}'", BEGIN, EC, DONE)
        self.assertTrue(grouped)
        _, grouped = build_exec_block('echo "}"', BEGIN, EC, DONE)
        self.assertTrue(grouped)

    def test_a_brace_inside_a_word_does_not_block_the_group(self) -> None:
        # `find -exec {} ;` is a word with braces in it, not a delimiter.
        _, grouped = build_exec_block("find . -name '*.py' -exec echo {} \\;", BEGIN, EC, DONE)
        self.assertTrue(grouped)

    def test_a_parameter_expansion_does_not_block_the_group(self) -> None:
        _, grouped = build_exec_block("echo ${HOME}", BEGIN, EC, DONE)
        self.assertTrue(grouped)

    def test_the_fallback_keeps_the_old_shape(self) -> None:
        # A command that cannot be grouped runs exactly as it did before:
        # the command, then the bookkeeping, then the marker.  The
        # continuation prompt is installed on the first line — the
        # shell must know it *before* it reads the command, or a
        # command that leaves it waiting would show the default `>`
        # instead of the marker.
        block, grouped = build_exec_block("echo }", BEGIN, EC, DONE)
        self.assertFalse(grouped)
        lines = block.splitlines()
        self.assertEqual(lines[0], f"printf '\\n{BEGIN}\\n'; PS2=\"{CONTINUATION_PROMPT} \"")
        self.assertEqual(lines[1], "echo }")
        self.assertEqual(lines[2], f"{EC}=$?")
        self.assertIn(DONE, lines[3])

    def test_the_scanner_reports_only_what_it_is_sure_about(self) -> None:
        self.assertEqual(unquoted_standalone_braces("echo hi"), [])
        self.assertEqual(unquoted_standalone_braces("echo a}b"), [])
        self.assertEqual(unquoted_standalone_braces("find . -exec {} \\;"), [])
        self.assertEqual(unquoted_standalone_braces("echo '}'"), [])
        self.assertEqual(unquoted_standalone_braces("echo \\}"), [])
        self.assertEqual(unquoted_standalone_braces("echo }"), ["}"])
        self.assertEqual(unquoted_standalone_braces("{ echo hi; }"), ["{", "}"])
        self.assertTrue(command_can_be_grouped("echo hi"))
        self.assertFalse(command_can_be_grouped("echo }"))


# ---------------------------------------------------------------------------
# The output must not grow a line
# ---------------------------------------------------------------------------
class TestTheOutputKeepsItsShape(unittest.TestCase):
    """The group is protocol, the caller's stdout must not show it."""

    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        from test_agent_api_endpoints import _fresh_app  # noqa: PLC0415

        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-agent-token-xyz"}

    def test_the_group_open_line_is_cut_away(self) -> None:
        from agent_api import _extract_command_output  # noqa: PLC0415

        # The shell parses the whole group before it runs any of it, so
        # the output arrives *after* the line that closes the group.
        capture = (
            f"ubuntu@host:/workspace$ printf '\\n{BEGIN}\\n'; PS2=\"$PS1\"\n"
            f"{BEGIN}\n"
            "ubuntu@host:/workspace$ {\n"
            "ubuntu@host:/workspace$ echo hello\n"
            f"ubuntu@host:/workspace$ }} < /dev/null; {EC}=$?; printf '\\n{DONE}=%d\\n' \"${{{EC}}}\"\n"
            "hello\n"
        )
        output, exit_code, head = _extract_command_output(capture, BEGIN, DONE, EC, True)
        self.assertEqual(output, "ubuntu@host:/workspace$ echo hello\nhello")
        self.assertTrue(head)

    def test_the_bookkeeping_line_is_dropped_but_the_output_is_kept(self) -> None:
        from agent_api import _extract_command_output  # noqa: PLC0415

        capture = (
            f"{BEGIN}\n"
            "$ {\n"
            "$ echo hello\n"
            f"$ }} < /dev/null; {EC}=$?; printf '\\n{DONE}=%d\\n' \"${{{EC}}}\"\n"
            "hello\n"
            f"{DONE}=0\n"
        )
        output, exit_code, head = _extract_command_output(capture, BEGIN, DONE, EC, True)
        self.assertEqual(output, "$ echo hello\nhello")
        self.assertEqual(exit_code, 0)

    def test_the_output_without_a_group_is_untouched(self) -> None:
        from agent_api import _extract_command_output  # noqa: PLC0415

        capture = (
            f"ubuntu@host:/workspace$ printf '\\n{BEGIN}\\n'\n"
            f"{BEGIN}\n"
            "ubuntu@host:/workspace$ echo hello\n"
            "hello\n"
            f"ubuntu@host:/workspace$ {EC}=$?\n"
        )
        output, exit_code, head = _extract_command_output(capture, BEGIN, DONE, EC, False)
        self.assertEqual(output, "ubuntu@host:/workspace$ echo hello\nhello")
        self.assertTrue(head)

    def test_the_two_forms_give_the_same_stdout(self) -> None:
        # The caller's stdout must not depend on how the command was
        # wrapped. Same command, same output — byte for byte.
        from agent_api import _extract_command_output  # noqa: PLC0415

        plain = f"{BEGIN}\n$ echo hello\nhello\n$ {EC}=$?\n"
        grouped = (
            f"{BEGIN}\n"
            "$ {\n"
            "$ echo hello\n"
            f"$ }} < /dev/null; {EC}=$?; printf '\\n{DONE}=%d\\n' \"${{{EC}}}\"\n"
            "hello\n"
        )
        plain_out, _e, _h = _extract_command_output(plain, BEGIN, DONE, EC, False)
        grouped_out, _e2, _h2 = _extract_command_output(grouped, BEGIN, DONE, EC, True)
        self.assertEqual(plain_out, grouped_out)

    def test_a_scrolled_pane_keeps_the_command_line(self) -> None:
        # If the group's opening line scrolled out of the pane, the first
        # line left is the command itself — and it must stay.
        from agent_api import _extract_command_output  # noqa: PLC0415

        capture = f"{BEGIN}\nubuntu@host:/workspace$ echo hello\nhello\n"
        output, _exit_code, _head = _extract_command_output(capture, BEGIN, DONE, EC, True)
        self.assertEqual(output, "ubuntu@host:/workspace$ echo hello\nhello")


# ---------------------------------------------------------------------------
# Live: a command that reads stdin
# ---------------------------------------------------------------------------
@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class TestACommandThatReadsStdin(unittest.TestCase):
    """Against a real tmux: the command must not eat the protocol."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="agent-stdin-")
        os.environ["AGENT_SESSIONS_FILE"] = os.path.join(self.tmpdir, "sessions.json")
        os.environ["AGENT_WORKSPACE"] = self.tmpdir
        os.environ["AGENT_TOKEN"] = "test-agent-token-xyz"
        if "agent_api" in sys.modules:
            del sys.modules["agent_api"]
        import agent_api

        self.agent_api = agent_api
        self._counter = 0

    def _run(self, command: str, timeout: int = 30) -> dict:
        import asyncio

        self._counter += 1
        sid = f"stdin{self._counter}"
        return asyncio.run(
            self.agent_api.exec_cmd(
                sid,
                self.agent_api.ExecRequest(session_id=sid, command=command, timeout=timeout),
            )
        )

    def test_read_returns_at_once(self) -> None:
        result = self._run("read x")
        self.assertFalse(result["timed_out"], "the command never finished")
        self.assertIsNotNone(result["exit_code"])
        self.assertLess(result["duration_ms"], 10_000)

    def test_read_reports_the_exit_code_of_the_eof(self) -> None:
        # `read` gets EOF, so it fails the way a failed read does.
        result = self._run("read x")
        self.assertEqual(result["exit_code"], 1)

    def test_cat_returns_at_once(self) -> None:
        result = self._run("cat")
        self.assertFalse(result["timed_out"], "the command never finished")
        self.assertEqual(result["exit_code"], 0)

    def test_the_output_does_not_grow_a_brace_line(self) -> None:
        result = self._run("echo hello")
        self.assertFalse(result["timed_out"])
        self.assertIn("hello", result["stdout"])
        self.assertNotIn("{", result["stdout"])
        self.assertNotIn("}", result["stdout"])
        self.assertNotIn("/dev/null", result["stdout"])

    def test_the_command_line_is_still_echoed(self) -> None:
        # The stdout contract includes the shell's redraw of the command
        # line. The group must not change that.
        result = self._run("echo hello")
        self.assertIn("echo hello", result["stdout"])

    def test_the_shell_state_survives_the_group(self) -> None:
        # A brace group runs in the current shell, so `cd` still counts.
        result = self._run("cd /tmp")
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["exit_code"], 0)
        result = self._run("pwd")
        self.assertIn("/tmp", result["stdout"])

    def test_a_pipeline_still_works(self) -> None:
        result = self._run("echo one | tr 'a-z' 'A-Z'")
        self.assertFalse(result["timed_out"])
        self.assertIn("ONE", result["stdout"])

    def test_a_heredoc_still_works(self) -> None:
        result = self._run("cat <<EOF\ninside the heredoc\nEOF")
        self.assertFalse(result["timed_out"])
        self.assertIn("inside the heredoc", result["stdout"])

    def test_a_command_with_a_stray_brace_still_runs(self) -> None:
        # Such a command is not grouped — it runs exactly as it did
        # before, so it must not lose its meaning.
        result = self._run("echo '}'")
        self.assertFalse(result["timed_out"])
        self.assertIn("}", result["stdout"])


if __name__ == "__main__":
    unittest.main()
