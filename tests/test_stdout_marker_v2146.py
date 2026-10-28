"""The protocol's marker does not reach the caller's stdout.

**What was wrong.** The v2.14.5 wave made the waiting state observable:
the protocol installs its own continuation prompt (``PS2``), so a shell
that waits for the rest of a line can be *seen* rather than guessed
from the shape of a line.

The marker is the shell's output, so it was cut out of the caller's
stdout — but only where the line was *exactly* the marker.  A
multi-line command is a sequence of continuations, so every one of its
lines came back with the marker in front of it::

    __AGENT_CONT__ for i in 1 2; do
    __AGENT_CONT__ echo line$i
    __AGENT_CONT__ done
    line1
    line2

The lines carry the text of the command the caller has just sent — the
caller knows it already — and the marker is a protocol token that means
nothing outside.  So the model was reading the protocol's bookkeeping
instead of the command's output.

**What the fix does.** Every line that starts with the marker is cut,
not only the bare one.  What is left is the command's output.
"""

from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agent_api_endpoints import _fresh_app  # noqa: E402


def _has_tmux() -> bool:
    return shutil.which("tmux") is not None


requires_tmux = unittest.skipUnless(_has_tmux(), "tmux is not installed")


@requires_tmux
class TestTheMarkerDoesNotReachTheCaller(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient  # noqa: PLC0415

        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-agent-token-xyz"}
        self.sid = "marker-" + uuid.uuid4().hex[:8]
        import agent_api  # noqa: PLC0415

        self.agent_api = agent_api
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        import subprocess  # noqa: PLC0415

        self.agent_api.SESSIONS.pop(self.sid, None)
        subprocess.run(
            ["tmux", "kill-session", "-t", self.agent_api.tmux_name(self.sid)],
            capture_output=True,
            check=False,
        )

    def _exec(self, command: str) -> dict:
        response = self.client.post(
            f"/sessions/{self.sid}/exec",
            json={"command": command, "timeout": 20},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_multi_line_command_answers_with_its_output_only(self) -> None:
        result = self._exec("for i in 1 2; do\necho line$i\ndone")
        stdout = result["stdout"]
        self.assertNotIn(
            "__AGENT_CONT__",
            stdout,
            f"the protocol's marker reached the caller: {stdout!r}",
        )
        self.assertIn("line1", stdout)
        self.assertIn("line2", stdout)

    def test_the_command_line_is_still_echoed(self) -> None:
        """The marker is cut; the caller's own text is not.

        The stdout contract includes the shell's redraw of the command
        line.  Cutting the whole line would take the caller's own text
        with it — and that text is not protocol, it is what the caller
        asked to run.
        """
        result = self._exec("for i in 1 2; do\necho line$i\ndone")
        stdout = result["stdout"]
        self.assertIn("for i in 1 2; do", stdout, stdout)
        self.assertIn("echo line$i", stdout, stdout)

    def test_a_single_line_command_answers_with_its_output_only(self) -> None:
        result = self._exec("echo plain")
        self.assertNotIn("__AGENT_CONT__", result["stdout"], result["stdout"])
        self.assertIn("plain", result["stdout"])

    def test_the_marker_is_still_installed(self) -> None:
        """The cut does not remove the marker from the protocol.

        The marker is what makes "the shell is waiting" observable; if
        the protocol stopped installing it, the cut would be hiding
        nothing and the waiting state would be a guess again.
        """
        source = (ROOT / "agent_core.py").read_text(encoding="utf-8")
        self.assertIn('CONTINUATION_PROMPT = "__AGENT_CONT__"', source)
        # The protocol installs the marker as the shell's continuation
        # prompt; the check reads the line, not a copy of it.
        ps2_lines = [line for line in source.split("\n") if "PS2=" in line]
        self.assertTrue(ps2_lines, "the protocol installs no continuation prompt")
        self.assertTrue(
            any("CONTINUATION_PROMPT" in line for line in ps2_lines),
            "the continuation prompt is not the agent's own marker",
        )


if __name__ == "__main__":
    unittest.main()
