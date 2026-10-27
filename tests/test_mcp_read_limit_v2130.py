"""The model-facing read budget: one MCP read may not cost the model more
than it can use.

The MCP door and the HTTP door are the same sandbox, but they answer
different questions. The HTTP door serves a *file*: the bytes land in a
file on the caller's disk, and the deployment runs that limit at 50 MiB
on purpose. The MCP door puts the bytes into a **prompt** — and a text
model asked to reason about fifty megabytes has lost the thread, while
the process holds the file in memory and base64-encodes it on the way
out (a third more).

So the MCP read gets a budget of its own (``AGENT_MCP_MAX_READ_BYTES``),
and the HTTP door keeps its own limit, untouched.

What is pinned here:

* the budget is read once and validated once, and a value outside its
  range is a **startup failure** rather than a silent clamp — a limit
  that is silently clamped is a limit nobody can trust;
* the effective limit is the smallest of the budget, the file policy and
  the hard ceiling;
* a read over the budget is refused, and the refusal names the file's
  size *and* the limit, because a refusal the model cannot act on is a
  refusal it retries;
* ``file_info`` reports the limit that actually applies, so the cheap
  door does not lie about the expensive one;
* the HTTP door is not touched: the very file the MCP tool refuses is
  still served by ``/files``;
* the ceiling has one home. A second copy of a limit is the copy that
  rots first.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOKEN = "test-agent-token-xyz"
MIB = 1024 * 1024
CEILING = 64 * MIB


def _budget(value: str | None) -> tuple[int, str]:
    """Read ``MCP_READ_LIMIT_BYTES`` in a fresh interpreter.

    Returns ``(value, stderr)``. A value that the agent refuses to start
    with comes back as ``(-1, stderr)`` — the refusal *is* the result
    under test, so the helper does not raise.
    """
    environ = dict(os.environ)
    if value is None:
        environ.pop("AGENT_MCP_MAX_READ_BYTES", None)
    else:
        environ["AGENT_MCP_MAX_READ_BYTES"] = value
    proc = subprocess.run(
        [sys.executable, "-c", "import agent_config; print(agent_config.MCP_READ_LIMIT_BYTES)"],
        cwd=ROOT,
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        return -1, proc.stderr
    return int(proc.stdout.strip()), proc.stderr


class TestTheBudgetIsAStartupFact(unittest.TestCase):
    """The budget is read once, validated once, and a bad value is loud."""

    def test_the_default_is_one_mebibyte(self) -> None:
        value, stderr = _budget(None)
        self.assertEqual(value, MIB, stderr)

    def test_the_budget_can_be_raised(self) -> None:
        value, stderr = _budget("2m")
        self.assertEqual(value, 2 * MIB, stderr)

    def test_zero_means_no_budget_of_its_own(self) -> None:
        # The same convention as AGENT_BINARY_LIMIT=0 ("unlimited"): the
        # other two limits decide then.
        value, stderr = _budget("0")
        self.assertEqual(value, 0, stderr)

    def test_a_value_over_the_ceiling_is_a_startup_failure(self) -> None:
        # Not a clamp. A clamped value is indistinguishable from a limit
        # that was never applied, and the operator would never learn that
        # the setting they wrote down is not the setting in force.
        value, stderr = _budget("128m")
        self.assertEqual(value, -1)
        self.assertIn("AGENT_MCP_MAX_READ_BYTES", stderr)
        self.assertIn("maximum", stderr)

    def test_garbage_is_a_startup_failure(self) -> None:
        value, stderr = _budget("one megabyte")
        self.assertEqual(value, -1)
        self.assertIn("AGENT_MCP_MAX_READ_BYTES", stderr)

    def test_the_ceiling_is_sixty_four_mebibytes(self) -> None:
        value, stderr = _budget("64m")
        self.assertEqual(value, CEILING, stderr)


class TestTheChain(unittest.TestCase):
    """Three limits, and the smallest one wins."""

    def _limit(self, policy: int, budget: int) -> int:
        import mcp_server  # noqa: PLC0415

        original_policy = mcp_server._binary_limit
        original_budget = mcp_server.MCP_READ_LIMIT_BYTES
        mcp_server._binary_limit = lambda: policy
        mcp_server.MCP_READ_LIMIT_BYTES = budget
        try:
            return mcp_server._read_limit()
        finally:
            mcp_server._binary_limit = original_policy
            mcp_server.MCP_READ_LIMIT_BYTES = original_budget

    def test_the_budget_wins_when_the_policy_is_larger(self) -> None:
        # The deployment runs the file policy at 50 MiB. The budget is
        # what a model can use, and it is smaller.
        self.assertEqual(self._limit(policy=50 * MIB, budget=MIB), MIB)

    def test_the_policy_still_wins_when_it_is_smaller(self) -> None:
        # The budget may only narrow, never widen.
        self.assertEqual(self._limit(policy=1024, budget=MIB), 1024)

    def test_the_ceiling_wins_when_both_are_off(self) -> None:
        self.assertEqual(self._limit(policy=0, budget=0), CEILING)

    def test_the_ceiling_wins_when_the_policy_is_huge(self) -> None:
        self.assertEqual(self._limit(policy=CEILING * 100, budget=0), CEILING)

    def test_the_budget_wins_over_a_huge_policy(self) -> None:
        self.assertEqual(self._limit(policy=CEILING * 100, budget=2 * MIB), 2 * MIB)


class TestTheCeilingHasOneHome(unittest.TestCase):
    """A second copy of a limit is the copy that rots first."""

    def test_the_ceiling_is_defined_in_the_configuration(self) -> None:
        tree = ast.parse((ROOT / "agent_config.py").read_text(encoding="utf-8"))
        names = {
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertIn("MCP_READ_CEILING_BYTES", names)

    def test_the_mcp_server_does_not_define_its_own(self) -> None:
        tree = ast.parse((ROOT / "mcp_server.py").read_text(encoding="utf-8"))
        defined = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                defined.update(target.id for target in node.targets if isinstance(target, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                defined.add(node.target.id)
        for name in ("MCP_READ_CEILING_BYTES", "MCP_MAX_READ_BYTES", "BINARY_LIMIT_BYTES"):
            with self.subTest(constant=name):
                self.assertNotIn(name, defined)


def _fresh_app():
    """Reload the agent and the MCP module against a temp workspace."""
    tmpdir = tempfile.mkdtemp(prefix="agent-mcp-budget-")
    os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmpdir, "sessions.json")
    os.environ["AGENT_WORKSPACE"] = tmpdir
    os.environ["AGENT_TOKEN"] = TOKEN
    # The default budget (1 MiB) and the default file policy (2 MiB).
    os.environ.pop("AGENT_MCP_MAX_READ_BYTES", None)
    os.environ.pop("AGENT_BINARY_LIMIT", None)
    for name in ("agent_api", "mcp_server", "agent_config"):
        sys.modules.pop(name, None)
    from agent_api import SESSIONS, app  # noqa: E402
    from mcp_server import mcp  # noqa: E402

    SESSIONS.clear()
    return app, mcp, tmpdir


class TestTheBudgetInTheWire(unittest.TestCase):
    """What the model actually sees."""

    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        self.app, self.mcp, self.tmpdir = _fresh_app()
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8090")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.auth = {"Authorization": f"Bearer {TOKEN}"}
        self.ws = Path(self.tmpdir)

    def write(self, name: str, data: bytes) -> Path:
        target = self.ws / name
        target.write_bytes(data)
        return target

    def call(self, tool: str, arguments: dict) -> dict:
        response = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            headers={**self.auth, "Accept": "application/json, text/event-stream"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("result", body, json.dumps(body))
        return body["result"]

    def test_a_file_over_the_budget_is_refused(self) -> None:
        self.write("big.bin", b"x" * (MIB + 1))
        response = self.call("read_file", {"path": "big.bin"})
        self.assertTrue(response["isError"], response.get("content"))
        text = response["content"][0]["text"]
        self.assertIn("over the limit", text)
        # The refusal names the size and the limit, so the model can
        # decide what to do instead of retrying the same call.
        self.assertIn(str(MIB + 1), text)
        self.assertIn(str(MIB), text)

    def test_a_file_under_the_budget_comes_back(self) -> None:
        self.write("small.txt", b"the file says hello")
        response = self.call("read_file", {"path": "small.txt"})
        self.assertFalse(response["isError"], response.get("content"))
        self.assertIn("the file says hello", response["content"][0]["text"])

    def test_file_info_reports_the_limit_that_applies(self) -> None:
        # The cheap door must not announce a limit the expensive door
        # does not enforce.
        self.write("note.txt", b"12345")
        response = self.call("file_info", {"path": "note.txt"})
        self.assertFalse(response["isError"], response.get("content"))
        self.assertEqual(response["structuredContent"]["limit"], MIB)

    def test_the_http_door_is_not_affected(self) -> None:
        # The same file the MCP tool refuses is still served by /files:
        # there the bytes land in a file, not in a prompt.
        payload = b"y" * (MIB + 1)
        self.write("big.bin", payload)
        response = self.client.get("/files", params={"path": "big.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.content), len(payload))


if __name__ == "__main__":
    unittest.main()
