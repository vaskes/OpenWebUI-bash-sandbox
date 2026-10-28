"""The shell's own ``>`` is a prompt only until the protocol has run.

**What was wrong.** The agent's protocol installs its own continuation
prompt (``PS2=__AGENT_CONT__``) as the first line of every command it
types.  Before that line runs, the shell's default ``PS2`` is ``>`` — and
a *program* is free to print ``>`` as well.  The check that asks "is the
shell waiting for the rest of a line?" could not tell the two apart, so
it called a running program a waiting shell and interrupted it.

Measured on a live tmux 3.6 pane (llmhost1), with a program built only
from shell builtins — so tmux reports the *shell* as the foreground
process and cannot tell it from a shell that waits:

    while true; do echo '>'; done
      foreground = bash      last line = ">"
      _pane_is_waiting_for_input -> True      ← a working command

**What the fix does.** The shell's default prompt only means "waiting"
in a shell where the protocol has never run.  The agent records that it
has run (the begin marker is printed by the protocol's first line, so
seeing it is the proof), and from then on a bare ``>`` is a program's
output: the stuck-shell heal does not fire on it, and the caller is told
the truth — the session is busy.

The legacy case is untouched: a shell that was waiting *before* the
protocol ever ran still shows ``>``, and it is still healed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agent_api_endpoints import _fresh_app  # noqa: E402
from test_stuck_session_v2144 import (  # noqa: E402
    STUCK_PANE_DEFAULT,
    _Double,
    _patched_tmux,
)

#: The pane of a session that runs a program whose last output is a bare
#: ``>`` — the shell's own default continuation prompt, printed by a
#: program.  The foreground process is the shell itself (a builtin loop
#: forks nothing), so tmux reports the shell and cannot tell it from a
#: shell that waits for the rest of a line.
RUNNING_PANE_GT = "root@host:/# while true; do echo '>'; done\n>\n>"


class TestTheBarePrompt(unittest.TestCase):
    """The shell's default prompt, and the protocol's marker."""

    def test_the_marker_is_the_only_continuation_prompt(self) -> None:
        from agent_core import (  # noqa: PLC0415
            CONTINUATION_PROMPT,
            is_a_continuation_prompt,
            is_a_default_continuation_prompt,
        )

        self.assertTrue(is_a_continuation_prompt(CONTINUATION_PROMPT))
        self.assertFalse(is_a_continuation_prompt(">"))
        self.assertTrue(is_a_default_continuation_prompt(">"))
        self.assertFalse(is_a_default_continuation_prompt(CONTINUATION_PROMPT))

    def test_a_line_that_merely_looks_like_a_prompt_is_not_one(self) -> None:
        from agent_core import is_a_continuation_prompt  # noqa: PLC0415

        for line in ("tick$", "root@host:/#", ">", "  >  "):
            with self.subTest(line=line):
                self.assertFalse(
                    is_a_continuation_prompt(line),
                    f"{line!r} is not the protocol's marker",
                )


class TestTheHeal(unittest.TestCase):
    """What the agent does with a pane that ends with a bare ``>``."""

    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-agent-token-xyz"}

    def _session(self, sid: str, protocol: bool) -> None:
        """Put the session in the registry, as a live session would be."""
        from agent_core import tmux_name  # noqa: PLC0415

        self.sessions[sid] = {
            "tmux_name": tmux_name(sid),
            "created": 0.0,
            "cwd": self.workspace_real,
            "protocol": protocol,
        }

    def _exec(self, sid: str, command: str, double: _Double, timeout: int = 1) -> dict:
        with _patched_tmux(self.app, double):
            response = self.client.post(
                f"/sessions/{sid}/exec",
                json={"command": command, "timeout": timeout},
                headers=self.auth,
            )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_a_running_program_that_printed_a_prompt_is_not_interrupted(self) -> None:
        """The protocol ran, so a bare ">" is the program's output."""
        double = _Double(pane=RUNNING_PANE_GT, command="bash", runs=False)
        self._session("gt", protocol=True)
        result = self._exec("gt", "echo hi", double=double)
        self.assertEqual(double.interrupts, 0, "a working command was interrupted")
        self.assertIn("the session is busy", result["stderr"])

    def test_a_shell_that_waited_before_the_protocol_is_still_interrupted(self) -> None:
        """The legacy case: the protocol never ran in that shell."""
        double = _Double(pane=STUCK_PANE_DEFAULT, command="bash", runs=False)
        self._session("legacy", protocol=False)
        result = self._exec("legacy", "echo hi", double=double)
        self.assertEqual(double.interrupts, 1, "the stuck shell was not freed")
        self.assertIn("has been reset", result["stderr"])

    def test_a_new_session_has_not_run_the_protocol(self) -> None:
        """The flag starts false: a new shell has the default prompt."""
        from agent_core import tmux_name  # noqa: PLC0415

        self.sessions["fresh"] = {
            "tmux_name": tmux_name("fresh"),
            "created": 0.0,
            "cwd": self.workspace_real,
        }
        self.assertFalse(self.sessions["fresh"].get("protocol"))

    def test_the_protocol_is_noted_once_it_has_run(self) -> None:
        """The begin marker is the proof that the protocol ran."""
        double = _Double(pane="", command="bash", runs=True)
        self._session("ran", protocol=False)
        self._exec("ran", "echo hi", double=double, timeout=5)
        self.assertTrue(
            self.sessions["ran"].get("protocol"),
            "the agent does not know that the protocol ran in this shell",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
