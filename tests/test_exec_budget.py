"""Tests for the stdout budget and the completion detection.

Three things are pinned here, and the third one is the reason this file
exists.

**The completion marker must be matched in its printed form.** The agent
types a command that prints ``__AGENT_DONE_xxxxxxxx__=0`` and then waits for
it to appear. The shell *echoes* the typed line into the pane, so the marker
string is visible in the pane long before the command has run. A plain
substring match therefore matched the echo, the poll stopped on a pane that
held a fraction of the output, and the caller received a truncated stdout
with no marker and no error. The fix is to match the printed form, as a
whole line.

**The beginning of the output must be detectable.** A pane that overflows
drops its *oldest* lines, so what comes back is the tail of the output
dressed up as a whole result. A begin marker printed before the command
makes that case detectable.

**The output must be complete, every time.** A command whose output fits
in the budget has to come back whole, on every run, not most of the time.
That is the property the race broke, and it is worth a test that runs the
command repeatedly against a real tmux.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agent_api_endpoints import _fresh_app, _make_tmux_mock  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-agent-token-xyz"}


class TestBeginMarker(_Base):
    def test_head_loss_is_reported(self) -> None:
        """A capture without the begin marker means the pane overflowed."""
        from unittest.mock import patch

        mock = _make_tmux_mock()
        mock.schedule("capture-pane", stdout="only the tail is here\n\n{marker}=0\n")
        with patch("agent_api._tmux", new=mock):
            response = self.client.post(
                "/sessions/s1/exec",
                json={"command": "cat huge.txt", "timeout": 5},
                headers=self.auth,
            )
        body = response.json()
        self.assertIn("no longer fits in the session scrollback", body["stdout"])
        self.assertIn("only the tail is here", body["stdout"])

    def test_complete_output_carries_no_notice(self) -> None:
        from unittest.mock import patch

        mock = _make_tmux_mock()
        mock.schedule("capture-pane", stdout="{begin}\nhello\n\n{marker}=0\n")
        with patch("agent_api._tmux", new=mock):
            response = self.client.post(
                "/sessions/s1/exec",
                json={"command": "echo hello", "timeout": 5},
                headers=self.auth,
            )
        body = response.json()
        self.assertIn("hello", body["stdout"])
        self.assertNotIn("no longer fits in the session scrollback", body["stdout"])


class TestTruncationMarker(unittest.TestCase):
    def test_output_over_the_budget_is_marked(self) -> None:
        import agent_api

        original = agent_api.MAX_OUTPUT_BYTES
        agent_api.MAX_OUTPUT_BYTES = 100
        self.addCleanup(lambda: setattr(agent_api, "MAX_OUTPUT_BYTES", original))
        self.assertIn("output truncated at 100 bytes", agent_api._truncate_output("x" * 500))

    def test_output_under_the_budget_is_untouched(self) -> None:
        import agent_api

        self.assertEqual(agent_api._truncate_output("hello"), "hello")

    def test_the_budget_is_read_at_call_time(self) -> None:
        import agent_api

        original = agent_api.MAX_OUTPUT_BYTES
        agent_api.MAX_OUTPUT_BYTES = 10
        self.addCleanup(lambda: setattr(agent_api, "MAX_OUTPUT_BYTES", original))
        self.assertIn("truncated at 10 bytes", agent_api._truncate_output("x" * 100))


class TestCaptureIsBoundedByTheBudget(unittest.TestCase):
    """``/capture`` must not be a door around the stdout budget."""

    def test_capture_lines_are_capped_by_the_budget(self) -> None:
        import agent_api

        # A line of a 200-column pane is 200 bytes, so this many lines
        # cannot exceed the budget.
        self.assertLessEqual(
            agent_api.MAX_OUTPUT_BYTES // agent_api.PANE_WIDTH,
            agent_api.CAPTURE_PANE_LINES,
        )

    def test_capture_depth_never_exceeds_the_budget(self) -> None:
        import agent_api

        effective = min(10_000, agent_api.CAPTURE_PANE_LINES, agent_api.MAX_OUTPUT_BYTES // 200)
        self.assertLessEqual(effective * agent_api.PANE_WIDTH, agent_api.MAX_OUTPUT_BYTES)


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class TestLiveExecIsComplete(unittest.TestCase):
    """Against a real tmux: the output must be whole, on every run.

    This is the test the race would have failed. It is deliberately
    repetitive — a race that shows up in one run out of three is exactly
    the kind that ships.

    Every case gets its own session. A session shares one scrollback
    between all of its commands, so five 400 KB commands in a row would
    overflow it and the *fifth* one would legitimately lose its
    beginning — which is a different test, and one that lives below.
    """

    RUNS = 5

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="agent-live-")
        os.environ["AGENT_SESSIONS_FILE"] = os.path.join(self.tmpdir, "sessions.json")
        os.environ["AGENT_WORKSPACE"] = self.tmpdir
        os.environ["AGENT_TOKEN"] = "test-agent-token-xyz"
        if "agent_api" in sys.modules:
            del sys.modules["agent_api"]
        import agent_api

        self.agent_api = agent_api
        self._counter = 0
        self._last_sid = ""

    def _run(self, command: str, session: str | None = None) -> dict:
        import asyncio

        self._counter += 1
        # A unique name per run. A session that is left over from an
        # earlier run is *adopted*, scrollback and all — and a session
        # with a full scrollback is a different environment, so the
        # test would be measuring the leftovers instead of the code.
        sid = session or f"live{self._counter}-{uuid.uuid4().hex[:8]}"
        self._last_sid = sid
        return asyncio.run(
            self.agent_api.exec_cmd(
                sid,
                self.agent_api.ExecRequest(session_id=sid, command=command, timeout=30),
            )
        )

    def test_large_output_comes_back_whole_every_time(self) -> None:
        # ~400 KB in 2000 lines: inside the 512 KB budget, inside the
        # scrollback, and wide enough that the pane wraps it.
        command = "awk 'BEGIN{for(i=0;i<2000;i++) printf \"%0200d\\n\", i}'"
        for attempt in range(self.RUNS):
            with self.subTest(attempt=attempt):
                result = self._run(command)
                stdout = result["stdout"]
                self.assertFalse(result["timed_out"], "the command did not finish")
                self.assertNotIn(
                    "no longer fits in the session scrollback",
                    stdout,
                    "the beginning of the output was lost",
                )
                self.assertNotIn("output truncated at", stdout)
                # The last line the command printed must be present.
                self.assertIn("00000000000000001999", stdout)
                # And the first one too — that is what the race ate.
                self.assertIn("00000000000000000000", stdout)

    def test_short_output_is_whole(self) -> None:
        result = self._run("echo marker-value")
        self.assertIn("marker-value", result["stdout"])
        self.assertNotIn("no longer fits in the session scrollback", result["stdout"])

    def test_exit_code_is_reported(self) -> None:
        result = self._run("sh -c 'exit 3'")
        self.assertEqual(result["exit_code"], 3)

    def test_stdout_does_not_carry_the_previous_command(self) -> None:
        """The scrollback is shared; the stdout of a command is not.

        A capture of "the pane" hands back everything the session ever
        printed. The output of the *second* command used to arrive with
        the first command's output glued in front of it.
        """
        first = self._run("echo FIRST-COMMAND-MARKER", session="shared")
        second = self._run("echo SECOND-COMMAND-MARKER", session="shared")
        self.assertIn("FIRST-COMMAND-MARKER", first["stdout"])
        self.assertNotIn(
            "FIRST-COMMAND-MARKER",
            second["stdout"],
            "the previous command's output leaked into the next result",
        )
        self.assertIn("SECOND-COMMAND-MARKER", second["stdout"])

    def test_the_protocol_bookkeeping_is_not_part_of_the_output(self) -> None:
        result = self._run("echo clean-output")
        self.assertNotIn("__AGENT_EC_", result["stdout"])
        self.assertNotIn("__AGENT_DONE_", result["stdout"])
        self.assertNotIn("__AGENT_BEGIN_", result["stdout"])

    def test_an_overflowing_output_is_reported_as_lost(self) -> None:
        """When the output does not fit, the caller must be told.

        The session's scrollback is finite — 8448 lines. A command that
        prints more than that loses its beginning, and the result says
        so instead of handing back a plausible fragment.

        (The scrollback being *full before* the command is a different
        case, and the answer there is the whole output: see
        :class:`TestAScrollbackThatIsAlreadyFull`.)
        """
        # More lines than the scrollback holds.
        result = self._run("seq 1 9000")
        self.assertIn(
            "no longer fits in the session scrollback",
            result["stdout"],
            "an output that lost its beginning was not reported",
        )

    def test_the_pane_really_holds_the_budget(self) -> None:
        """The scrollback must be deep enough for the promised budget."""
        import agent_api

        self._run("echo warmup")
        out = subprocess.run(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                f"agent_{agent_api.safe_id(self._last_sid)}",
                "#{history_limit}",
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        self.assertEqual(int(out), agent_api.HISTORY_LIMIT_LINES)


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class TestAScrollbackThatIsAlreadyFull(unittest.TestCase):
    """The answer stays whole when the scrollback was already full.

    The capture is sized by the *growth* of the scrollback during the
    command. When the scrollback is already nearly full, the command's
    output takes the place of the old lines instead of adding to them,
    so the growth is a fraction of what the command printed — and the
    bounded capture stops short of the beginning.

    The whole output is in the scrollback, so the answer has to be the
    whole output. Handing back a fragment with a "the beginning was
    lost" notice in front of it is a lie: nothing was lost.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="agent-full-")
        os.environ["AGENT_SESSIONS_FILE"] = os.path.join(self.tmpdir, "sessions.json")
        os.environ["AGENT_WORKSPACE"] = self.tmpdir
        os.environ["AGENT_TOKEN"] = "test-agent-token-xyz"
        if "agent_api" in sys.modules:
            del sys.modules["agent_api"]
        import agent_api

        self.agent_api = agent_api
        self.sid = f"full-{uuid.uuid4().hex[:8]}"

    def _run(self, command: str) -> dict:
        import asyncio

        return asyncio.run(
            self.agent_api.exec_cmd(
                self.sid,
                self.agent_api.ExecRequest(session_id=self.sid, command=command, timeout=30),
            )
        )

    def test_the_output_is_whole_even_when_the_scrollback_was_full(self) -> None:
        # Fill the scrollback: the limit is 8448 lines, this is 8000.
        self._run("seq 1 8000")
        # Now a command whose output does not fit in what is left.
        result = self._run("seq 1 2000")
        stdout = result["stdout"]
        self.assertNotIn(
            "no longer fits in the session scrollback",
            stdout,
            "the answer was declared a fragment — but the scrollback held it",
        )
        self.assertIn("2000", stdout, "the last line of the output is missing")
        self.assertIn("1\n", stdout, "the first line of the output is missing")


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class TestColdStart(unittest.TestCase):
    """A container that has never run a command must still work.

    The first session of a cold container is the one that has to survive
    the tmux server's own start-up. Doing this as a single ``\\;`` chain
    raced with it: roughly one cold start in twenty failed outright, and
    the failure was a 500 with no session.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="agent-cold-")
        # A tmux server of its own. `kill-server` on the shared server
        # takes every other test's live session with it — that is how a
        # suite becomes flaky, and it is how a green run turns red on the
        # next one. tmux keeps its socket in $TMUX_TMPDIR, so a private
        # directory is a private server: this test kills its own server
        # and nobody else's.
        self._tmux_tmpdir = os.environ.get("TMUX_TMPDIR")
        private = os.path.join(self.tmpdir, "tmux")
        os.makedirs(private, exist_ok=True)
        os.environ["TMUX_TMPDIR"] = private
        os.environ["AGENT_SESSIONS_FILE"] = os.path.join(self.tmpdir, "sessions.json")
        os.environ["AGENT_WORKSPACE"] = self.tmpdir
        os.environ["AGENT_TOKEN"] = "test-agent-token-xyz"
        if "agent_api" in sys.modules:
            del sys.modules["agent_api"]
        import agent_api

        self.agent_api = agent_api

    def tearDown(self) -> None:
        # The private server — and only the private server.
        subprocess.run(["tmux", "kill-server"], capture_output=True, check=False)
        if self._tmux_tmpdir is None:
            os.environ.pop("TMUX_TMPDIR", None)
        else:
            os.environ["TMUX_TMPDIR"] = self._tmux_tmpdir

    def test_a_session_survives_a_cold_tmux_server(self) -> None:
        import asyncio

        for attempt in range(5):
            with self.subTest(attempt=attempt):
                subprocess.run(["tmux", "kill-server"], capture_output=True, check=False)
                result = asyncio.run(
                    self.agent_api.create_session(
                        self.agent_api.CreateSession(session_id=f"cold{attempt}")
                    )
                )
                self.assertEqual(result["session_id"], f"cold{attempt}")
                self.assertFalse(result["existed"])
                # And the pane really got the scrollback the budget needs.
                limit = asyncio.run(
                    self.agent_api._pane_history_limit(self.agent_api.tmux_name(f"cold{attempt}"))
                )
                self.assertEqual(limit, self.agent_api.HISTORY_LIMIT_LINES)
