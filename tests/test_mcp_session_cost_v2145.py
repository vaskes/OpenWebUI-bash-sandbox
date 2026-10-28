"""The MCP tool asks tmux only when tmux can change the answer.

**What was wrong.** The ``bash`` tool opened (or adopted) the session
on *every* call, even when the call named no directory and the
session was already known.  ``create_session`` answers two questions
with two processes: "does the pane exist?" and "which directory is it
in?" — and the second one is the only reason the call was there, since
a directory the session cannot honour has to be refused.

So every command the model ran cost two tmux processes for a question
the registry already held.

**What the fix does.** The tool asks tmux only when the answer can
change what happens: a directory was named, or the session is unknown.
The refusal of an impossible directory is untouched — that path still
goes through ``create_session`` and still answers with the directory
the session is really in.
"""

from __future__ import annotations

import asyncio
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
    import shutil  # noqa: PLC0415

    return shutil.which("tmux") is not None


requires_tmux = unittest.skipUnless(_has_tmux(), "tmux is not installed")


@requires_tmux
class TestTheToolAsksTmuxOnlyWhenItMust(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()
        self.sid = "cost-" + uuid.uuid4().hex[:8]
        import agent_api  # noqa: PLC0415

        self.agent_api = agent_api

    def _calls(self):
        """Count the calls the tool makes to the session door."""
        from unittest.mock import patch  # noqa: PLC0415

        import mcp_server  # noqa: PLC0415

        calls: list = []
        real = self.agent_api.create_session

        async def counting(body, *args, **kwargs):
            calls.append(body)
            return await real(body, *args, **kwargs)

        with patch.object(self.agent_api, "create_session", counting):
            result = asyncio.run(
                mcp_server.bash("echo hello", session_id=self.sid),
            )
        return calls, result

    def test_a_known_session_is_not_opened_again(self) -> None:
        # The first call opens the session — it is unknown.
        calls, _ = self._calls()
        self.assertEqual(len(calls), 1, "the first call did not open the session")

        # The second call knows the session and names no directory:
        # there is nothing tmux could say that would change the answer.
        calls, result = self._calls()
        self.assertEqual(calls, [], "the tool asked tmux for a question it had the answer to")
        self.assertIn("hello", result["stdout"])

    def test_a_named_directory_still_asks(self) -> None:
        """The refusal of an impossible directory is the reason for the ask."""
        from unittest.mock import patch  # noqa: PLC0415

        import mcp_server  # noqa: PLC0415

        calls: list = []
        real = self.agent_api.create_session

        async def counting(body, *args, **kwargs):
            calls.append(body)
            return await real(body, *args, **kwargs)

        with patch.object(self.agent_api, "create_session", counting):
            asyncio.run(mcp_server.bash("echo hello", session_id=self.sid))

        calls.clear()
        sub = Path(self.tmpdir) / "sub"
        sub.mkdir(exist_ok=True)
        from mcp.server.mcpserver.exceptions import ToolError  # noqa: PLC0415

        with patch.object(self.agent_api, "create_session", counting):
            with self.assertRaises(ToolError) as ctx:
                asyncio.run(
                    mcp_server.bash("echo hello", session_id=self.sid, cwd=str(sub)),
                )
        self.assertEqual(len(calls), 1, "the tool did not ask about the directory it was given")
        # The refusal still names the way out — that is the point of the ask.
        self.assertIn("session_id", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
