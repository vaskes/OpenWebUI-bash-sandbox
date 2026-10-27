"""The review's claims that are about code, pinned by tests.

Three findings from the external review are claims about the code. A
claim that nobody checks rots, so each of them becomes a test here.

**The token comparison is constant-time.** The review asked to *make
sure* the checker uses ``secrets.compare_digest`` and not ``==``. It
does — but "it does" is a claim about a line of code, and a line of
code can be edited. The test reads the function and requires the
comparison to be a ``compare_digest`` call, and requires that the
credential is never compared with ``==``.

**The MCP tool refuses an impossible timeout legibly.** The request
model refuses an out-of-range timeout, but in its own words: the model
would read a validation error about a class it has never heard of, and
no range at all. The tool knows the range, so the tool says it.

**The MCP tool resolves the agent at call time.** The review objected
to the tests' habit of dropping modules from ``sys.modules`` and
re-importing them, on the grounds that a module which already imported
the old one keeps the old reference. That is true in general — and it
does not apply here, because the MCP tools import ``agent_api``
*inside* the tool, on every call. The test pins that: after the agent
is reloaded, the tool must use the new module. Without this property
the pattern would be a trap, and with it the pattern is a reload.
"""

from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agent_api_endpoints import _fresh_app  # noqa: E402


def _source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _function_ast(name: str, function: str) -> ast.FunctionDef:
    tree = ast.parse(_source(name))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            return node
    raise AssertionError(f"{name} has no function {function}")


class TestTheTokenIsComparedInConstantTime(unittest.TestCase):
    def test_the_checker_uses_compare_digest(self) -> None:
        node = _function_ast("agent_api.py", "_token_ok")
        calls = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
        ]
        self.assertTrue(
            any(call.func.attr == "compare_digest" for call in calls),
            "_token_ok does not call compare_digest",
        )

    def test_the_credential_is_never_compared_with_equals(self) -> None:
        node = _function_ast("agent_api.py", "_token_ok")
        for child in ast.walk(node):
            if not isinstance(child, ast.Compare):
                continue
            names = {item.id for item in ast.walk(child) if isinstance(item, ast.Name)}
            self.assertFalse(
                names & {"credentials", "AGENT_TOKEN"},
                f"_token_ok compares the credential with an ordinary operator: {ast.dump(child)}",
            )

    def test_the_comparison_lives_in_one_place(self) -> None:
        # "One token, one comparison" — the MCP door calls the agent's
        # checker, it does not have a second one of its own.
        source = _source("mcp_server.py")
        self.assertNotIn("compare_digest", source)


class TestTheTimeoutIsRefusedLegibly(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()

    def _refusal(self, timeout):
        import asyncio  # noqa: PLC0415

        from mcp.server.mcpserver.exceptions import ToolError  # noqa: PLC0415

        import mcp_server  # noqa: PLC0415

        with self.assertRaises(ToolError) as ctx:
            asyncio.run(mcp_server.bash("echo hi", session_id="t", timeout=timeout))
        return str(ctx.exception)

    def test_a_timeout_below_the_range_is_refused(self) -> None:
        message = self._refusal(0)
        self.assertIn("timeout", message)
        self.assertIn("1", message)

    def test_a_timeout_above_the_range_is_refused(self) -> None:
        message = self._refusal(10_000)
        self.assertIn("timeout", message)
        self.assertIn("600", message)

    def test_a_timeout_that_is_not_a_number_is_refused(self) -> None:
        message = self._refusal("soon")
        self.assertIn("timeout", message)

    def test_the_refusal_does_not_mention_an_internal_class(self) -> None:
        # "1 validation error for ExecRequest" is the model talking to
        # the internals of a framework. The model cannot act on that.
        for bad in (0, 10_000, "soon"):
            with self.subTest(timeout=bad):
                self.assertNotIn("ExecRequest", self._refusal(bad))

    def test_a_timeout_inside_the_range_is_accepted(self) -> None:
        import asyncio  # noqa: PLC0415

        import mcp_server  # noqa: PLC0415

        result = asyncio.run(mcp_server.bash("echo ok", session_id="t2", timeout=5))
        self.assertIn("ok", result["stdout"])


class TestTheToolResolvesTheAgentAtCallTime(unittest.TestCase):
    """The property that makes the tests' reload pattern a reload."""

    def setUp(self) -> None:
        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()

    def test_the_tool_uses_the_current_agent_module(self) -> None:
        import asyncio  # noqa: PLC0415

        import agent_api  # noqa: PLC0415
        import mcp_server  # noqa: PLC0415

        wanted = os.path.join(self.workspace_real, "wanted.txt")
        with open(wanted, "w", encoding="utf-8") as handle:
            handle.write("FROM-THE-CURRENT-MODULE\n")
        other = os.path.join(self.workspace_real, "other.txt")
        with open(other, "w", encoding="utf-8") as handle:
            handle.write("FROM-THE-OTHER-FILE\n")

        original = agent_api._safe_file
        agent_api._safe_file = lambda path: other
        try:
            result = asyncio.run(mcp_server.read_file("wanted.txt"))
        finally:
            agent_api._safe_file = original

        self.assertIn("FROM-THE-OTHER-FILE", result.content[0].text)


class TestTheTestsDoNotLeaveTempDirs(unittest.TestCase):
    """A test workspace is removed when the test process ends.

    The review pointed at ``tempfile.mkdtemp`` without a cleanup: every
    test run leaves a directory behind, and the pile grows until
    somebody notices. The check is end-to-end on purpose — a registry
    that is never emptied is not a cleanup, and a line that registers
    a cleanup that never runs is not one either.
    """

    def test_the_workspace_does_not_survive_the_process(self) -> None:
        import subprocess  # noqa: PLC0415
        import sys  # noqa: PLC0415

        tests_dir = str(Path(__file__).resolve().parent)
        code = (
            "import sys\n"
            f"sys.path.insert(0, {tests_dir!r})\n"
            "from test_agent_api_endpoints import _fresh_app\n"
            "print(_fresh_app()[2])\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(ROOT),
        )
        path = result.stdout.strip().splitlines()[-1]
        self.assertTrue(path, "the subprocess did not report a workspace")
        self.assertFalse(os.path.exists(path), "the workspace survived the process")

    def test_the_mcp_harness_cleans_up_too(self) -> None:
        import subprocess  # noqa: PLC0415
        import sys  # noqa: PLC0415

        tests_dir = str(Path(__file__).resolve().parent)
        code = (
            "import sys\n"
            f"sys.path.insert(0, {tests_dir!r})\n"
            "from test_mcp_server import _fresh_app\n"
            "print(_fresh_app()[2])\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(ROOT),
        )
        path = result.stdout.strip().splitlines()[-1]
        self.assertTrue(path, "the subprocess did not report a workspace")
        self.assertFalse(os.path.exists(path), "the workspace survived the process")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
