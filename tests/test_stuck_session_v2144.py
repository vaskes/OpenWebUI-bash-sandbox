"""Tests for the session that stops executing commands.

**What was wrong.** Two shapes, both measured against a live shell.

*A command that outlives its timeout keeps the session.* The agent
answers ``timed_out: true`` and the caller moves on, but the command is
still running in the session's shell — so the session stays occupied and
every later command in it is refused as "the session is busy". The trap
was systematic: the proxy capped every command at 60 seconds while the
agent allowed an hour, so *every* long command armed it. A caller that
timed out was told "timed out" and left the session unusable.

*A command whose text is not a complete shell command sticks the shell
for good.* An unterminated quote, an unterminated here-document and a
trailing backslash all leave the interactive shell waiting for the rest
of the line — and the protocol that follows is swallowed as part of it.
Every later command is queued behind the unfinished one, and nothing in
the system frees the session.

**What the fix does.** A command that would stick the shell is refused
*before* it is typed, with the reason. A shell that is stuck anyway is
interrupted, and the session becomes usable again. The refusal for a
busy session names the way out. And the exec limit is what the
deployment says it is — the paths that start the stack name the
deployment's env file, so a limit cannot be dropped on the way in.
"""

from __future__ import annotations

import os
import re
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

from test_agent_api_endpoints import _fresh_app  # noqa: E402


def _has_tmux() -> bool:
    """Is tmux on this machine?  The classes that drive a real tmux
    session are skipped when it is not — a missing tool is an
    environment, not a defect."""
    import shutil  # noqa: PLC0415

    return shutil.which("tmux") is not None


requires_tmux = unittest.skipUnless(_has_tmux(), "tmux is not installed")


#: The pane of a shell that waits for the rest of an unfinished command.
#: The last line is the shell's *continuation prompt*: the marker the
#: agent's protocol installs, and the one thing that cannot be printed
#: by a program.
STUCK_PANE = (
    "ubuntu@host:/workspace$ {\n"
    'ubuntu@host:/workspace$ echo "unterminated\n'
    "ubuntu@host:/workspace$ } < /dev/null; __AGENT_EC_x__=$?; printf 'x'\n"
    "__AGENT_CONT__"
)

#: The pane of a shell that waits for the rest of a line *before* the
#: protocol ever ran in it: the shell's own continuation prompt.
STUCK_PANE_DEFAULT = 'root@host:/# echo "unterminated\n> '

#: The pane of a session that runs something. The last line is the
#: terminal's echo of the running command — not a prompt.
BUSY_PANE = "ubuntu@host:/workspace$ sleep 20\nubuntu@host:/workspace$ sleep 20"

#: The pane of a *running* command whose output ends with a dollar sign.
#: A builtin loop forks nothing, so tmux reports the shell itself as the
#: foreground process, and its output looks exactly like a prompt. The
#: old rule ("the last line looks like a prompt") called this a stuck
#: shell and interrupted a working command.
RUNNING_PANE = "ubuntu@host:/workspace$ while true; do echo 'tick$'; done\ntick$\ntick$"


class _Double:
    """A tmux double for the stuck-session scenarios.

    It remembers every call: a double that was never called means the
    request went somewhere other than the double — to the live tmux
    server — and the test would be measuring the wrong thing while
    looking green.

    ``runs`` says whether the command actually ran. When it did not, the
    pane shows the scenario; when it did, the pane carries the markers of
    the exec protocol, so the agent sees a completed command.
    """

    def __init__(
        self,
        pane: str = "",
        command: str = "bash",
        runs: bool = True,
        interrupt_frees: bool = True,
    ) -> None:
        self.pane = pane
        self.command = command
        self.runs = runs
        # Whether the interrupt frees the shell. A real shell forgets the
        # line it was reading; a shell that does not is a different
        # case, and the answer must not claim a reset that did not
        # happen.
        self.interrupt_frees = interrupt_frees
        self.calls: list[tuple[str, ...]] = []
        self.sent: list[str] = []
        self.interrupts = 0
        self.interrupted = False

    async def __call__(self, *args: str) -> tuple[int, str, str]:
        self.calls.append(args)
        cmd = args[0] if args else ""
        if cmd == "send-keys":
            if args and args[-1] == "C-c":
                self.interrupts += 1
                # A real shell that is interrupted forgets the line it
                # was reading and comes back to a prompt.  The double
                # has to show that, or the "did the interrupt work?"
                # check can never be true.
                self.interrupted = True
                return (0, "", "")
            try:
                index = args.index("-l")
            except ValueError:
                index = -1
            if index >= 0:
                end = len(args) - 1 if args and args[-1] == "Enter" else len(args)
                self.sent.append(" ".join(args[index + 1 : end]))
        if cmd == "display-message":
            fmt = args[-1] if args else ""
            if "pane_current_command" in fmt:
                return (0, self.command, "")
            if "history_size" in fmt:
                return (0, "12", "")
            if "history_limit" in fmt:
                return (0, "8448", "")
            return (0, "", "")
        if cmd == "capture-pane":
            return (0, self._capture(), "")
        return (0, "", "")

    def _capture(self) -> str:
        if self.interrupted and self.interrupt_frees:
            lines = self.pane.split("\n")
            while lines and not lines[-1].strip():
                lines.pop()
            if lines:
                lines[-1] = "ubuntu@host:/workspace$"
            return "\n".join(lines)
        if not self.runs or not self.sent:
            return self.pane
        begin = re.search(r"__AGENT_BEGIN_([0-9a-f]+)__", self.sent[-1])
        done = re.search(r"__AGENT_DONE_([0-9a-f]+)__", self.sent[-1])
        if not begin or not done:
            return self.pane
        return (
            "ubuntu@host:/workspace$ printf '\\n"
            f'__AGENT_BEGIN_{begin.group(1)}__\\n\'; PS2="__AGENT_CONT__ "\n'
            f"\n__AGENT_BEGIN_{begin.group(1)}__\n"
            "hello\n"
            f"\n__AGENT_DONE_{done.group(1)}__=0\n"
            "ubuntu@host:/workspace$"
        )


def _patched_tmux(app, double):
    """Patch the tmux call in the module that serves *this* app.

    The globals of the endpoint function are the exact namespace the
    request runs in. Patching them cannot go wrong the way patching a
    module attribute can: the harness reloads ``agent_api`` for every
    test, so the module in ``sys.modules`` and the app under test can be
    two different objects — and then the patch does not reach the
    request, and the test quietly talks to the live tmux server.
    """
    from unittest.mock import patch  # noqa: PLC0415

    endpoint = None
    for route in app.routes:
        func = getattr(route, "endpoint", None)
        if func is not None and getattr(route, "path", "") == "/sessions/{session_id}/exec":
            endpoint = func
            break
    assert endpoint is not None, "the exec route is not on the app under test"
    return patch.dict(endpoint.__globals__, {"_tmux": double})


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-agent-token-xyz"}

    def _exec(self, command: str, timeout: int = 1, double: _Double | None = None) -> dict:
        double = double or _Double()
        with _patched_tmux(self.app, double):
            response = self.client.post(
                "/sessions/stuck/exec",
                json={"command": command, "timeout": timeout},
                headers=self.auth,
            )
        self.assertEqual(response.status_code, 200)
        return response.json()


class TestTheGate(_Base):
    """A command that would stick the shell is refused before it is typed."""

    def test_an_unterminated_quote_is_refused(self) -> None:
        result = self._exec('echo "unterminated')
        self.assertTrue(result["refused"])
        self.assertIn("quote or bracket open", result["stderr"])
        self.assertEqual(result["stdout"], "")
        self.assertFalse(result["timed_out"])

    def test_an_unterminated_here_document_is_refused(self) -> None:
        result = self._exec("cat <<'EOF'\nbody without a terminator")
        self.assertTrue(result["refused"])
        self.assertIn("leave the shell waiting", result["stderr"])

    def test_a_trailing_backslash_is_refused(self) -> None:
        result = self._exec("echo \\")
        self.assertTrue(result["refused"])
        self.assertIn("line continuation", result["stderr"])

    def test_the_command_is_never_typed(self) -> None:
        """A refused command must not reach the shell.

        The refusal is the whole point: a command that is typed and then
        recognised as broken is a command that already poisoned the
        session.
        """
        double = _Double()
        result = self._exec('echo "unterminated', double=double)
        self.assertTrue(result["refused"])
        self.assertEqual(double.calls, [], "the shell was touched for a refused command")
        self.assertEqual(double.sent, [], "the command was typed into the shell")
        self.assertEqual(double.interrupts, 0)

    def test_a_valid_command_is_not_refused(self) -> None:
        for command in (
            "echo hi",
            "ls -l | grep x | wc -l",
            "awk '{print $1}' file.txt",
            "cd /workspace && ls",
            "cat <<'EOF'\nhello\nEOF",
            "for i in 1 2 3; do echo $i; done",
            "echo $(date)",
            "python3 -c \"print('a')\"",
            "find . -name '*.py' -exec grep -l x {} \\;",
            'echo "it\'s"',
            "git commit -m 'Press Enter to continue'",
        ):
            with self.subTest(command=command):
                double = _Double()
                result = self._exec(command, double=double)
                self.assertNotIn("refused", result, f"{command!r} was refused")
                self.assertEqual(result["exit_code"], 0)
                self.assertTrue(double.sent, f"{command!r} was never typed")

    def test_a_valid_command_is_not_refused_when_the_cwd_is_gone(self) -> None:
        """Bash's own noise is not the command's problem.

        Measured: with a working directory that was removed underneath
        the process, ``bash -n`` says "shell-init: error retrieving
        current directory" on stderr.  The first version of the gate
        read *any* stderr as a trap, and it refused a perfectly valid
        ``git commit -m 'Press Enter to continue'`` — the whole suite
        went red on a command that has nothing wrong with it.

        The parse now runs in ``/``, and only a here-document warning
        counts as a trap.
        """
        from agent_core import command_would_stick  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            gone = os.path.join(tmp, "gone")
            os.mkdir(gone)
            previous = os.getcwd()
            os.chdir(gone)
            os.rmdir(gone)
            try:
                self.assertIsNone(command_would_stick("git commit -m 'Press Enter to continue'"))
                self.assertIsNone(command_would_stick("echo hello"))
            finally:
                os.chdir(previous)


class TestTheStuckShellIsFreed(_Base):
    """A shell that waits for the rest of a command is interrupted."""

    def test_a_stuck_shell_is_interrupted_and_the_answer_says_so(self) -> None:
        double = _Double(pane=STUCK_PANE, runs=False)
        result = self._exec("echo hi", timeout=1, double=double)
        self.assertEqual(double.interrupts, 1, "the shell was not interrupted")
        self.assertIn("waiting for the rest of an unfinished command", result["stderr"])
        self.assertIn("has been reset", result["stderr"])
        self.assertEqual(result["stdout"], "")

    def test_a_busy_session_is_not_interrupted(self) -> None:
        """A running command must not be killed by the heal."""
        double = _Double(pane=BUSY_PANE, command="sleep", runs=False)
        result = self._exec("echo hi", timeout=1, double=double)
        self.assertEqual(double.interrupts, 0, "a running command was interrupted")
        self.assertIn("the session is busy", result["stderr"])

    def test_a_shell_running_a_compound_command_is_not_interrupted(self) -> None:
        """tmux reports the shell for a compound command — the pane's last
        line is what tells the two apart."""
        pane = "ubuntu@host:/workspace$ while true; do sleep 1; done\nstill running"
        double = _Double(pane=pane, command="bash", runs=False)
        result = self._exec("echo hi", timeout=1, double=double)
        self.assertEqual(double.interrupts, 0)
        self.assertIn("the session is busy", result["stderr"])

    def test_a_running_command_whose_output_ends_with_a_dollar_is_not_interrupted(self) -> None:
        """The measured case: a builtin loop, the shell as foreground.

        ``while true; do echo 'tick$'; done`` forks nothing, so tmux
        reports the *shell* — and the last line of the pane ends with
        ``$``, exactly like a prompt.  The old rule read that as "the
        shell is waiting", interrupted a working command, and told the
        caller the shell had been reset.  The continuation prompt is
        the marker now, and a program cannot print it by accident.
        """
        double = _Double(pane=RUNNING_PANE, command="bash", runs=False)
        result = self._exec("echo hi", timeout=1, double=double)
        self.assertEqual(double.interrupts, 0, "a working command was interrupted")
        self.assertIn("the session is busy", result["stderr"])

    def test_a_shell_waiting_with_the_default_prompt_is_interrupted(self) -> None:
        """A shell that was waiting before the protocol ever ran.

        Its continuation prompt is the shell's own ``>`` — the marker
        was never installed in that shell.  That case is still healed.
        """
        double = _Double(pane=STUCK_PANE_DEFAULT, command="bash", runs=False)
        result = self._exec("echo hi", timeout=1, double=double)
        self.assertEqual(double.interrupts, 1, "the shell was not interrupted")
        self.assertIn("has been reset", result["stderr"])

    def test_an_interrupt_that_frees_nothing_is_not_reported_as_a_reset(self) -> None:
        """The answer must not claim a reset that did not happen.

        The shell was interrupted, and it is still waiting.  The caller
        has to be told the session is busy — a "has been reset" would
        send it back into the same dead end with a wrong story.
        """
        double = _Double(pane=STUCK_PANE, command="bash", runs=False, interrupt_frees=False)
        result = self._exec("echo hi", timeout=1, double=double)
        self.assertEqual(double.interrupts, 1, "the shell was not interrupted")
        self.assertNotIn("has been reset", result["stderr"])
        self.assertIn("the session is busy", result["stderr"])

    def test_the_busy_refusal_names_the_way_out(self) -> None:
        double = _Double(pane=BUSY_PANE, command="sleep", runs=False)
        result = self._exec("echo hi", timeout=1, double=double)
        self.assertIn("session_id", result["stderr"])
        self.assertIn("the command did not start", result["stderr"])


class TestTheChainIsWhole(unittest.TestCase):
    """The limit is the deployment's, and every start path has to say so.

    The proxy asks the agent for ``AGENT_EXEC_TIMEOUT`` and ignores the
    timeout the caller sent, so the deployment's number *is* the limit
    every command gets.  The compose interpolates
    ``${AGENT_EXEC_TIMEOUT:-60}``: a path that starts the stack without
    the deployment's env file falls back to a minute — silently, and with
    the caller still believing it may run for an hour.  In production
    that is exactly what happened: the side-car ran with a 60-second
    limit for a whole release, because the deploy path did not pass the
    operator's env file.
    """

    def test_the_agent_and_the_proxy_agree_on_the_ceiling(self) -> None:
        from agent_api import MAX_TIMEOUT  # noqa: PLC0415
        from proxy_core import EXEC_TIMEOUT_MAX_SECONDS  # noqa: PLC0415

        self.assertEqual(MAX_TIMEOUT, EXEC_TIMEOUT_MAX_SECONDS)

    def test_the_shipped_default_is_unchanged(self) -> None:
        """The default is the deployment's to raise, not this release's."""
        from proxy_core import EXEC_TIMEOUT_SECONDS  # noqa: PLC0415

        self.assertEqual(EXEC_TIMEOUT_SECONDS, 60.0)

    def test_the_unit_names_the_env_file(self) -> None:
        """The boot path must not start the stack without the settings."""
        unit = (ROOT / "agent-stack.service.example").read_text(encoding="utf-8")
        self.assertIn("COMPOSE_ENV_FILE", unit)
        self.assertIn("AGENT_EXEC_TIMEOUT", unit)

    def test_the_documentation_says_the_limit_is_the_deployments(self) -> None:
        text = (ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
        self.assertIn("AGENT_EXEC_TIMEOUT", text)
        self.assertIn("env file", text)
        self.assertIn("COMPOSE_ENV_FILE", text)
        self.assertIn("the session is busy", text)


class TestTheRefusalNamesTheRightThing(unittest.TestCase):
    """Three different problems, three different words.

    A model that reads "is not valid shell syntax" about an unfinished
    construct concludes it has a typo, and tries another line — instead
    of sending the construct whole.  The refusal has to say which of
    the three it is.
    """

    def test_an_unfinished_construct_is_not_called_a_syntax_error(self) -> None:
        from agent_core import command_would_stick  # noqa: PLC0415

        reason = command_would_stick("if true; then")
        self.assertIsNotNone(reason)
        self.assertIn("is not finished", reason)
        self.assertIn("the whole construct", reason)
        self.assertNotIn("is not valid shell syntax", reason)

    def test_an_unclosed_quote_says_that_the_quote_is_open(self) -> None:
        from agent_core import command_would_stick  # noqa: PLC0415

        reason = command_would_stick('echo "unterminated')
        self.assertIsNotNone(reason)
        self.assertIn("quote or bracket open", reason)
        self.assertNotIn("is not valid shell syntax", reason)

    def test_a_real_syntax_error_is_still_a_syntax_error(self) -> None:
        from agent_core import command_would_stick  # noqa: PLC0415

        reason = command_would_stick("echo )")
        self.assertIsNotNone(reason)
        self.assertIn("is not valid shell syntax", reason)
        self.assertNotIn("is not finished", reason)


class TestTheProtocolIsTheClaim(unittest.TestCase):
    """The protocol installs the marker the heal looks for.

    "The shell is waiting for the rest of a line" is only visible if
    the shell shows something that nothing else prints.  The protocol
    puts that marker into ``PS2`` — on the first line, so the shell
    knows it *before* it reads the command that may leave it waiting.
    """

    def test_the_protocol_installs_the_marker(self) -> None:
        from agent_core import (  # noqa: PLC0415
            CONTINUATION_PROMPT,
            build_exec_block,
        )

        for command in ("echo hi", "echo }"):
            with self.subTest(command=command):
                block, _grouped = build_exec_block(command, "BEGIN", "EC", "DONE")
                first = block.splitlines()[0]
                self.assertIn(f'PS2="{CONTINUATION_PROMPT} "', first)

    def test_the_marker_is_cut_out_of_the_caller_s_output(self) -> None:
        """The marker is the shell's output, and the caller never saw it."""
        from agent_api import _strip_continuation_prompts  # noqa: PLC0415
        from agent_core import CONTINUATION_PROMPT  # noqa: PLC0415

        region = f"hello\n{CONTINUATION_PROMPT}\nworld\n"
        self.assertEqual(_strip_continuation_prompts(region), "hello\nworld\n")

    def test_a_program_that_prints_the_marker_loses_the_line(self) -> None:
        """A program may print the marker; the line is still cut away.

        The marker is rare enough that this costs nothing, and the
        alternative — a line the caller sees that it has never seen
        before — costs the caller a strange line in every output.
        """
        from agent_api import _strip_continuation_prompts  # noqa: PLC0415
        from agent_core import CONTINUATION_PROMPT  # noqa: PLC0415

        region = f"before\n{CONTINUATION_PROMPT} \nafter"
        self.assertEqual(_strip_continuation_prompts(region), "before\nafter")


@requires_tmux
class TestTheStuckShellRecovers(unittest.TestCase):
    """Against a real tmux: a stuck shell is freed and the session works.

    The poison here is typed the way the agent's own protocol types it —
    raw text into the pane — because the gate now refuses such a command
    before it is typed.  The heal has to work for a shell that got stuck
    by any other route, and this is the only way to have one.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="agent-stuck-")
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
        # The private server — and only the private server. tmux keeps its
        # socket in $TMUX_TMPDIR, so a private directory is a private
        # server.
        subprocess.run(["tmux", "kill-server"], capture_output=True, check=False)
        if self._tmux_tmpdir is None:
            os.environ.pop("TMUX_TMPDIR", None)
        else:
            os.environ["TMUX_TMPDIR"] = self._tmux_tmpdir

    def _exec(self, sid: str, command: str, timeout: int = 3) -> dict:
        import asyncio

        return asyncio.run(
            self.agent_api.exec_cmd(
                sid,
                self.agent_api.ExecRequest(command=command, timeout=timeout),
            )
        )

    def test_a_stuck_shell_is_freed_and_the_session_works_again(self) -> None:
        sid = f"stuck-{uuid.uuid4().hex[:8]}"
        import asyncio

        asyncio.run(
            self.agent_api.create_session(
                self.agent_api.CreateSession(session_id=sid, cwd=self.tmpdir)
            )
        )
        name = self.agent_api.tmux_name(sid)

        # Make the shell wait for the rest of a command. The protocol
        # installs its own continuation prompt, so a shell that is
        # waiting shows exactly that — and the heal looks for exactly
        # that.
        marker = self.agent_api.CONTINUATION_PROMPT
        for text in (f'PS2="{marker} "', 'echo "unterminated'):
            subprocess.run(["tmux", "send-keys", "-t", name, "-l", text], check=False)
            subprocess.run(["tmux", "send-keys", "-t", name, "Enter"], check=False)
        import time

        time.sleep(0.5)

        result = self._exec(sid, "echo alive", timeout=2)
        self.assertIn("unfinished command", result["stderr"])
        self.assertEqual(result["stdout"], "")

        # And the session is usable again.
        again = self._exec(sid, "echo alive", timeout=5)
        self.assertFalse(again.get("timed_out"), again)
        self.assertIn("alive", again["stdout"])
        self.assertEqual(again["exit_code"], 0)

    def test_a_running_command_is_not_interrupted(self) -> None:
        """The measured case, against a real tmux.

        A builtin loop forks nothing, so tmux reports the *shell* as
        the foreground process, and the loop's output ends with a dollar
        sign — which the old rule read as a prompt.  The heal must not
        touch a working command, and the loop must keep running.
        """
        sid = f"running-{uuid.uuid4().hex[:8]}"
        import asyncio
        import time

        asyncio.run(
            self.agent_api.create_session(
                self.agent_api.CreateSession(session_id=sid, cwd=self.tmpdir)
            )
        )
        name = self.agent_api.tmux_name(sid)

        subprocess.run(
            ["tmux", "send-keys", "-t", name, "-l", "while true; do echo 'tick$'; done"],
            check=False,
        )
        subprocess.run(["tmux", "send-keys", "-t", name, "Enter"], check=False)
        time.sleep(1.0)

        before = asyncio.run(self.agent_api._pane_tail(name))
        self.assertTrue(before, "the loop printed nothing")
        self.assertTrue(
            any("tick$" in line for line in before),
            f"the loop is not running: {before}",
        )

        freed = asyncio.run(self.agent_api._reset_a_stuck_shell(name))
        self.assertFalse(freed, "a working command was called a stuck shell")

        time.sleep(0.5)
        after = asyncio.run(self.agent_api._pane_tail(name))
        self.assertTrue(
            any("tick$" in line for line in after),
            f"the loop was killed: {after}",
        )
        self.assertFalse(
            any(line.strip() == "^C" for line in after),
            f"the shell was interrupted: {after}",
        )


if __name__ == "__main__":
    unittest.main()
