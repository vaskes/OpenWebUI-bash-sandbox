"""Tests for the timeout race between the proxy and the agent.

**The proxy and the agent used the same number.** The proxy asked the
agent for a 60-second command and gave up waiting after 60 seconds too,
so the answer that said "this timed out" arrived at the very moment the
proxy stopped listening. The caller got a transport error with an empty
reason, and the operator was sent to the network instead of to the
clock.

The limit is now a knob, and the proxy waits strictly longer than the
limit it asks for.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# The timeout race
# ---------------------------------------------------------------------------
class TestTheTimeoutRace(unittest.TestCase):
    """The proxy must wait longer than the limit it asks the agent for."""

    def test_the_proxy_waits_longer_than_the_exec_limit(self) -> None:
        import openapi_proxy  # noqa: PLC0415

        self.assertGreater(
            openapi_proxy.PROXY_REQUEST_TIMEOUT_SECONDS,
            openapi_proxy.AGENT_EXEC_TIMEOUT,
            "the proxy must outlive the exec limit, or the timeout answer is lost",
        )

    def test_the_wait_is_the_limit_plus_a_grace(self) -> None:
        import openapi_proxy  # noqa: PLC0415
        from proxy_core import PROXY_WAIT_GRACE_SECONDS  # noqa: PLC0415

        self.assertAlmostEqual(
            openapi_proxy.PROXY_REQUEST_TIMEOUT_SECONDS,
            openapi_proxy.AGENT_EXEC_TIMEOUT + PROXY_WAIT_GRACE_SECONDS,
        )

    def test_the_exec_limit_is_configurable(self) -> None:
        from proxy_core import parse_exec_timeout  # noqa: PLC0415

        self.assertEqual(parse_exec_timeout("60"), 60)
        self.assertEqual(parse_exec_timeout(" 120 "), 120)

    def test_an_out_of_range_limit_refuses_to_start(self) -> None:
        from proxy_core import parse_exec_timeout  # noqa: PLC0415

        for bad in ("0", "1", "601", "-30", "sixty", "", "60s"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    parse_exec_timeout(bad)

    def test_the_error_message_names_the_variable_and_the_range(self) -> None:
        from proxy_core import parse_exec_timeout  # noqa: PLC0415

        with self.assertRaises(ValueError) as ctx:
            parse_exec_timeout("601")
        message = str(ctx.exception)
        self.assertIn("AGENT_EXEC_TIMEOUT", message)
        self.assertIn("601", message)

    def test_the_proxy_forwards_the_configured_limit(self) -> None:
        import asyncio  # noqa: PLC0415
        from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

        import openapi_proxy  # noqa: PLC0415

        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"stdout": "ok", "exit_code": 0})
        mock_client.post = AsyncMock(return_value=mock_resp)
        asyncio.run(openapi_proxy._do_exec(mock_client, "echo hi", "s1"))
        self.assertEqual(mock_client.post.call_args.kwargs["json"]["timeout"], 60)

    def test_a_timeout_names_the_cause(self) -> None:
        import asyncio  # noqa: PLC0415
        from unittest.mock import AsyncMock  # noqa: PLC0415

        import httpx  # noqa: PLC0415

        import openapi_proxy  # noqa: PLC0415

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ReadTimeout(""))
        body = asyncio.run(openapi_proxy._do_exec(mock_client, "sleep 100", "s1"))
        self.assertIn("error", body)
        # The reason must not be empty: an empty reason sends the
        # operator to the network instead of to the clock.
        self.assertNotEqual(body["error"].strip(), "sandbox-agent request failed:")
        self.assertIn("timed out", body["error"])
        self.assertIn("ReadTimeout", body["error"])

    def test_a_transport_error_names_the_cause(self) -> None:
        import asyncio  # noqa: PLC0415
        from unittest.mock import AsyncMock  # noqa: PLC0415

        import httpx  # noqa: PLC0415

        import openapi_proxy  # noqa: PLC0415

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("[Errno 111] refused"))
        body = asyncio.run(openapi_proxy._do_exec(mock_client, "ls", "s1"))
        self.assertIn("error", body)
        self.assertIn("ConnectError", body["error"])
        self.assertIn("refused", body["error"])


# ---------------------------------------------------------------------------
# The knob has to be a real knob in every stack
# ---------------------------------------------------------------------------
class TestTheExecLimitIsAKnobEverywhere(unittest.TestCase):
    COMPOSE_FILES = (
        "docker-compose.yml",
        "docker-compose.production.yml",
        "docker-compose.ci.yml",
    )

    def test_every_stack_passes_the_knob_to_the_proxy(self) -> None:
        for name in self.COMPOSE_FILES:
            with self.subTest(compose=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn("${AGENT_EXEC_TIMEOUT:-60}", text)

    def test_the_knob_is_documented_in_the_env_example(self) -> None:
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("AGENT_EXEC_TIMEOUT", text)


if __name__ == "__main__":
    unittest.main()
