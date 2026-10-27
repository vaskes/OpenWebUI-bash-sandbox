"""The review findings of v2.12.0, pinned by tests.

Two reviews looked at the MCP release: an external one (the operator's
reviewer) and an internal adversarial pass. Every finding that was
judged a real defect got a test here, and each test was checked
against a mutation — a test that cannot be made red is a decoration,
and the point of this file is that the claims are true.

What is pinned:

* the file door opens the file it checked (no symlink swap between the
  containment check and the open);
* a read cannot cost the machine more than the ceiling, whatever the
  configured limit says, and a file that grows while it is read is
  refused;
* a body without a content length is counted, not trusted;
* a failing tool leaves a trace in the log for the operator;
* the MCP relay waits longer than the agent can work;
* the reaper's log line stays readable when a thousand sessions die.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class TestTheFileDoorOpensTheFileItChecked(unittest.TestCase):
    """A symlink swapped in between the check and the open must not be followed.

    The workspace is writable, so the window between "this path is
    inside the workspace" and "open this path" is real. The open must
    refuse a final component that turned into a symlink.
    """

    def test_the_open_does_not_follow_a_symlink(self) -> None:
        source = _source("agent_api.py")
        self.assertIn("os.O_NOFOLLOW", source)

    def test_the_open_is_not_a_plain_open(self) -> None:
        # A plain ``open(name, "rb")`` follows a symlink; the guard is
        # the flag, so the plain call has to be gone from the function
        # that opens a vetted path.
        source = _source("agent_api.py")
        block = source.split("def _open_for_download", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn('return open(name, "rb")', block)

    def test_the_refusal_names_the_reason(self) -> None:
        # A refusal that says nothing is a refusal the operator cannot
        # act on: the model would read "cannot read" and invent a cause.
        source = _source("agent_api.py")
        self.assertIn("errno.ELOOP", source)
        self.assertIn("replaced while it was being opened", source)


class TestAReadCannotCostTheMachineTooMuch(unittest.TestCase):
    """One MCP read has a ceiling, whatever the configured limits say.

    The chain has three links since v2.13.0 — the model-facing budget,
    the file policy and the hard ceiling — and the smallest one wins.
    These tests pin the two ends of it: the policy still decides when it
    is the smaller one, and the ceiling still decides when everything
    else is switched off.
    """

    def test_the_ceiling_exists(self) -> None:
        import mcp_server  # noqa: PLC0415

        self.assertGreater(mcp_server.MCP_READ_CEILING_BYTES, 0)

    def test_the_configured_limit_wins_when_it_is_smaller(self) -> None:
        import mcp_server  # noqa: PLC0415

        original = mcp_server._binary_limit
        mcp_server._binary_limit = lambda: 1024
        try:
            self.assertEqual(mcp_server._read_limit(), 1024)
        finally:
            mcp_server._binary_limit = original

    def test_the_ceiling_wins_when_the_limit_is_unlimited(self) -> None:
        # "0 means no limit" is a legal configuration. A read that takes
        # the configuration at its word can ask the machine for
        # everything it has, so the ceiling decides instead.
        import mcp_server  # noqa: PLC0415

        original_policy = mcp_server._binary_limit
        original_budget = mcp_server.MCP_READ_LIMIT_BYTES
        mcp_server._binary_limit = lambda: 0
        mcp_server.MCP_READ_LIMIT_BYTES = 0
        try:
            self.assertEqual(mcp_server._read_limit(), mcp_server.MCP_READ_CEILING_BYTES)
        finally:
            mcp_server._binary_limit = original_policy
            mcp_server.MCP_READ_LIMIT_BYTES = original_budget

    def test_the_ceiling_wins_when_the_limit_is_huge(self) -> None:
        import mcp_server  # noqa: PLC0415

        original_policy = mcp_server._binary_limit
        original_budget = mcp_server.MCP_READ_LIMIT_BYTES
        mcp_server._binary_limit = lambda: mcp_server.MCP_READ_CEILING_BYTES * 100
        mcp_server.MCP_READ_LIMIT_BYTES = 0
        try:
            self.assertEqual(mcp_server._read_limit(), mcp_server.MCP_READ_CEILING_BYTES)
        finally:
            mcp_server._binary_limit = original_policy
            mcp_server.MCP_READ_LIMIT_BYTES = original_budget

    def test_the_read_is_bounded_by_the_limit(self) -> None:
        # The read must ask for one byte more than the limit allows:
        # that is how a file that grew between the stat and the read is
        # caught instead of being read whole.
        source = _source("mcp_server.py")
        self.assertIn("handle.read(limit + 1)", source)


class TestABodyWithoutALengthIsCounted(unittest.TestCase):
    """A limit that reads one header is a limit on polite clients only."""

    def test_the_middleware_exists_in_both_services(self) -> None:
        for name in ("agent_api.py", "openapi_proxy.py"):
            self.assertIn("BodySizeLimit", _source(name), name)

    def test_the_middleware_counts_the_bytes(self) -> None:
        source = _source("body_limit.py")
        self.assertIn('total += len(message.get("body", b""))', source)
        self.assertIn("if total > self.limit", source)

    def test_the_refusal_is_a_413(self) -> None:
        source = _source("body_limit.py")
        self.assertIn('"status": 413', source)

    def test_the_header_alone_is_not_the_limit(self) -> None:
        # The old check was exactly this: read the header, compare it,
        # and let a chunked body through. The counting has to be there
        # as well, not instead of the header.
        source = _source("body_limit.py")
        self.assertIn("content-length", source)
        self.assertIn("counted_receive", source)


class TestAFailingToolLeavesATrace(unittest.TestCase):
    """The model gets the reason; the operator gets the traceback."""

    def test_every_tool_logs_before_it_refuses(self) -> None:
        source = _source("mcp_server.py")
        for tool in ("mcp.read_file", "mcp.file_info", "mcp.delete_file", "mcp.bash"):
            self.assertIn(f'logger.exception("{tool}', source)

    def test_every_except_handler_traces(self) -> None:
        # Not "there is a logger.exception somewhere" — *every* handler
        # that refuses. A tool that has three failure paths and traces
        # two of them is the case this has to catch, and a count cannot
        # catch it: the count stays the same when one of them is
        # downgraded to ``logger.info``.
        #
        # A handler that does not refuse is not a failure — catching
        # ``UnicodeDecodeError`` is how a binary file is recognised.
        # Only the handlers that raise ``ToolError`` have to leave a
        # trace behind.
        import ast  # noqa: PLC0415

        tree = ast.parse(_source("mcp_server.py"))
        handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
        self.assertGreaterEqual(len(handlers), 6, "the tools lost their failure paths")
        refusals = 0
        for handler in handlers:
            raises_tool_error = any(
                isinstance(node, ast.Raise)
                and isinstance(node.exc, ast.Call)
                and getattr(node.exc.func, "id", "") == "ToolError"
                for node in ast.walk(handler)
            )
            if not raises_tool_error:
                continue
            refusals += 1
            calls = [
                node
                for node in ast.walk(handler)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "exception"
            ]
            self.assertTrue(
                calls,
                f"a refusing handler does not log a traceback (line {handler.lineno})",
            )
        self.assertGreaterEqual(refusals, 6, "the tools lost their refusal paths")


class TestTheRelayOutlastsTheCommand(unittest.TestCase):
    """The MCP relay must not drop an answer the agent is still computing."""

    def test_the_relay_has_its_own_timeout(self) -> None:
        source = _source("openapi_proxy.py")
        self.assertIn("MCP_RELAY_TIMEOUT_SECONDS", source)
        self.assertIn("timeout=MCP_RELAY_TIMEOUT_SECONDS", source)

    def test_the_relay_waits_longer_than_the_agent_can_work(self) -> None:
        # The agent's hard cap lives in the agent's own module. The
        # relay's number is a second copy of that fact, so a test
        # compares the two: a drift here means the relay starts
        # dropping answers the agent is still computing.
        agent = _source("agent_api.py")
        proxy = _source("openapi_proxy.py")
        match = re.search(r"^MAX_TIMEOUT\s*=\s*(\d+)", agent, re.MULTILINE)
        self.assertIsNotNone(match, "agent_api.MAX_TIMEOUT is gone")
        relay = re.search(
            r"^MCP_RELAY_TIMEOUT_SECONDS:\s*float\s*=\s*([\d.]+)", proxy, re.MULTILINE
        )
        self.assertIsNotNone(relay, "the relay timeout constant is gone")
        self.assertGreater(
            float(relay.group(1)),
            int(match.group(1)),
            "the MCP relay would give up before the agent stops working",
        )


class TestTheReaperLogStaysReadable(unittest.TestCase):
    """A log line nobody can read is the same as no line."""

    def test_the_list_is_truncated(self) -> None:
        source = _source("agent_api.py")
        self.assertIn("reaped[:10]", source)

    def test_the_count_is_still_the_truth(self) -> None:
        source = _source("agent_api.py")
        self.assertIn("len(reaped)", source)


class TestTheSharedModuleIsInBothImages(unittest.TestCase):
    """A module the code imports must be in the image that runs it."""

    def test_the_agent_image_copies_it(self) -> None:
        self.assertIn("body_limit.py", _source("Dockerfile.sandbox"))

    def test_the_proxy_image_copies_it(self) -> None:
        self.assertIn("body_limit.py", _source("Dockerfile.openapi-proxy"))
