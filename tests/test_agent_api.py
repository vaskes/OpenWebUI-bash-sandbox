"""Unit tests for the agent pure functions.

These tests have no external dependencies (no FastAPI, no tmux, no network)
and are safe to run anywhere Python 3.10+ is available. They target
:mod:`agent_core` directly to keep the test surface independent of the
FastAPI app defined in :mod:`agent_api`.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Make the project root importable when running ``python -m unittest`` from
# inside the tests/ directory or via a discovered runner.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_core import SESSION_ID_MAX_LEN, TMUX_PREFIX, __version__ as AGENT_CORE_VERSION, safe_id, tmux_name  # noqa: E402


class TestSafeId(unittest.TestCase):
    """Session-id sanitization is the security boundary — anything that
    reaches ``tmux`` as a session name must be constrained to a safe
    character set."""

    def test_alphanumeric_unchanged(self) -> None:
        self.assertEqual(safe_id("chat_abc123"), "chat_abc123")

    def test_special_chars_replaced_with_underscore(self) -> None:
        self.assertEqual(safe_id("chat/with spaces!"), "chat_with_spaces_")

    def test_empty_string_falls_back_to_default(self) -> None:
        self.assertEqual(safe_id(""), "default")

    def test_only_special_chars_become_underscores(self) -> None:
        self.assertEqual(safe_id("$$$"), "___")

    def test_truncates_long_input(self) -> None:
        long_input = "a" * 200
        result = safe_id(long_input)
        self.assertEqual(len(result), SESSION_ID_MAX_LEN)
        self.assertEqual(result, "a" * SESSION_ID_MAX_LEN)

    def test_unicode_replaced(self) -> None:
        self.assertEqual(safe_id("тест"), "____")

    def test_preserves_dash_and_underscore(self) -> None:
        self.assertEqual(safe_id("a-b_c"), "a-b_c")

    def test_max_length_boundary(self) -> None:
        boundary = "a" * SESSION_ID_MAX_LEN
        self.assertEqual(safe_id(boundary), boundary)

    def test_max_length_plus_one(self) -> None:
        too_long = "a" * (SESSION_ID_MAX_LEN + 1)
        result = safe_id(too_long)
        self.assertEqual(len(result), SESSION_ID_MAX_LEN)


class TestTmuxName(unittest.TestCase):
    """tmux session name is ``agent_<safe_id>`` — verify prefix and
    sanitization compose correctly."""

    def test_prefix_added(self) -> None:
        self.assertEqual(tmux_name("chat1"), f"{TMUX_PREFIX}chat1")

    def test_sanitization_applied_to_name(self) -> None:
        self.assertEqual(tmux_name("chat/1"), f"{TMUX_PREFIX}chat_1")

    def test_empty_id_uses_default(self) -> None:
        self.assertEqual(tmux_name(""), f"{TMUX_PREFIX}default")

    def test_prefix_is_constant(self) -> None:
        # If TMUX_PREFIX ever changes, the tmux session-name schema changes
        # and any existing sessions become orphans. Lock it down.
        self.assertEqual(TMUX_PREFIX, "agent_")


class TestVersion(unittest.TestCase):
    """Sanity-check the package version is present and looks like semver."""

    def test_version_is_semver(self) -> None:
        parts = AGENT_CORE_VERSION.split(".")
        self.assertEqual(len(parts), 3)
        for p in parts:
            self.assertTrue(p.isdigit(), f"non-numeric version part: {p!r}")


if __name__ == "__main__":
    unittest.main()
