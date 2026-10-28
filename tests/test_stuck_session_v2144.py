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

#: The pane of a shell that waits for the rest of an unfinished command.
#: The last line is nothing but a prompt: the shell is at a prompt, so
#: nothing is running, so it can be interrupted safely.
STUCK_PANE = (
    "ubuntu@host:/workspace$ {\n"
    'ubuntu@host:/workspace$ echo "unterminated\n'
    "ubuntu@host:/workspace$ } < /dev/null; __AGENT_EC_x__=$?; printf 'x'\n"
    "ubuntu@host:/workspace$"
)

#: The pane of a session that runs something. The last line is the
#: terminal's echo of the running command — not a prompt.
BUSY_PANE = "ubuntu@host:/workspace$ sleep 20\nubuntu@host:/workspace$ sleep 20"


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

    def __init__(self, pane: str = "", command: str = "bash", runs: bool = True) -> None:
        self.pane = pane
        self.command = command
        self.runs = runs
        self.calls: list[tuple[str, ...]] = []
        self.sent: list[str] = []
        self.interrupts = 0

    async def __call__(self, *args: str) -> tuple[int, str, str]:
        self.calls.append(args)
        cmd = args[0] if args else ""
        if cmd == "send-keys":
            if args and args[-1] == "C-c":
                self.interrupts += 1
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
        if not self.runs or not self.sent:
            return self.pane
        begin = re.search(r"__AGENT_BEGIN_([0-9a-f]+)__", self.sent[-1])
        done = re.search(r"__AGENT_DONE_([0-9a-f]+)__", self.sent[-1])
        if not begin or not done:
            return self.pane
        return (
            "ubuntu@host:/workspace$ printf '\\n"
            f'__AGENT_BEGIN_{begin.group(1)}__\\n\'; PS2="$PS1"\n'
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
        self.assertIn("not valid shell syntax", result["stderr"])
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

    def test_the_busy_refusal_names_the_way_out(self) -> None:
        double = _Double(pane=BUSY_PANE, command="sleep", runs=False)
        result = self._exec("echo hi", timeout=1, double=double)
        self.assertIn("session_id", result["stderr"])
        self.assertIn("the command did not start", result["stderr"])


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

        # Make the shell wait for the rest of a command. The protocol sets
        # PS2="$PS1", so a continuation looks exactly like a normal
        # prompt — that is what the heal looks for.
        for text in ('PS2="$PS1"', 'echo "unterminated'):
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


if __name__ == "__main__":
    unittest.main()
