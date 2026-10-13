"""Unit tests for :mod:`proxy_core` constants.

The full OpenAPI app pulls in httpx + FastAPI which aren't available in the
unit-test environment. These tests target the stable surface — the prefix
constants that define the public command syntax the LLM sees.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from proxy_core import (  # noqa: E402
    DEFAULT_SESSION_ID,
    EXEC_TIMEOUT_SECONDS,
    SEARXNG_TIMEOUT_SECONDS,
    SSH_PREFIX,
    WEBSOCKET_POLL_INTERVAL,
    WEBSOCKET_SESSION_ID,
    WEB_SEARCH_PREFIX,
    __version__ as PROXY_CORE_VERSION,
)


class TestPrefixConstants(unittest.TestCase):
    """The command prefixes are part of the public API. If they change, every
    prompt that uses ``web_search`` or ``ssh`` will silently break."""

    def test_web_search_prefix(self) -> None:
        self.assertEqual(WEB_SEARCH_PREFIX, "web_search ")

    def test_ssh_prefix(self) -> None:
        self.assertEqual(SSH_PREFIX, "ssh ")

    def test_both_prefixes_have_trailing_space(self) -> None:
        self.assertTrue(WEB_SEARCH_PREFIX.endswith(" "))
        self.assertTrue(SSH_PREFIX.endswith(" "))

    def test_both_prefixes_are_lowercase(self) -> None:
        self.assertEqual(WEB_SEARCH_PREFIX, WEB_SEARCH_PREFIX.lower())
        self.assertEqual(SSH_PREFIX, SSH_PREFIX.lower())


class TestSessionDefaults(unittest.TestCase):
    def test_default_session_id(self) -> None:
        self.assertEqual(DEFAULT_SESSION_ID, "openwebui")

    def test_websocket_session_id_distinct(self) -> None:
        # The WebSocket session must not collide with the default REST session
        # so a stray WebSocket reconnect can't poison an in-flight chat.
        self.assertNotEqual(WEBSOCKET_SESSION_ID, DEFAULT_SESSION_ID)


class TestTimeouts(unittest.TestCase):
    def test_all_timeouts_positive(self) -> None:
        self.assertGreater(EXEC_TIMEOUT_SECONDS, 0)
        self.assertGreater(SEARXNG_TIMEOUT_SECONDS, 0)
        self.assertGreater(WEBSOCKET_POLL_INTERVAL, 0)

    def test_exec_timeout_longer_than_poll(self) -> None:
        # Sanity: a single WebSocket poll must complete well within the exec
        # timeout, or the terminal UI feels laggy.
        self.assertGreater(EXEC_TIMEOUT_SECONDS, WEBSOCKET_POLL_INTERVAL * 10)


class TestVersion(unittest.TestCase):
    def test_version_is_semver(self) -> None:
        parts = PROXY_CORE_VERSION.split(".")
        self.assertEqual(len(parts), 3)
        for p in parts:
            self.assertTrue(p.isdigit(), f"non-numeric version part: {p!r}")


if __name__ == "__main__":
    unittest.main()
