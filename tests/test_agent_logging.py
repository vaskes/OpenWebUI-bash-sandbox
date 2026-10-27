"""Tests for the agent's log lines.

A log line that raises inside the logging machinery does not fail the
process — it fails *quietly*. The message is swallowed and a formatting
error is printed instead, so the line that was supposed to say which
sessions were dropped never arrives, and the operator sees nothing at
all.
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# The startup reaper's log line
# ---------------------------------------------------------------------------
class TestTheReaperLog(unittest.TestCase):
    def test_the_line_says_how_many_and_which(self) -> None:
        from agent_api import _log_reaped_sessions  # noqa: PLC0415

        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("sandbox-agent")
        old_level = logger.level
        logger.setLevel(logging.INFO)
        handler = _Capture()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, old_level)
        try:
            _log_reaped_sessions(["alpha", "beta"])
        except TypeError as exc:  # pragma: no cover - the bug this pins
            self.fail(f"the log line raised: {exc}")
        self.assertTrue(records, "nothing was logged")
        self.assertIn("dropped 2 stale session(s)", records[0].getMessage())
        self.assertIn("alpha", records[0].getMessage())
        self.assertIn("beta", records[0].getMessage())

    def test_a_long_list_is_truncated(self) -> None:
        """A machine that lost a thousand sessions must still log a line.

        The count stays the truth; the list is cut. A line a thousand
        ids long is a line nobody reads, which is the same as no line.
        """
        from agent_api import _log_reaped_sessions  # noqa: PLC0415

        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("sandbox-agent")
        old_level = logger.level
        logger.setLevel(logging.INFO)
        handler = _Capture()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, old_level)

        _log_reaped_sessions([f"session-{index}" for index in range(1000)])
        message = records[0].getMessage()
        self.assertIn("dropped 1000 stale session(s)", message)
        self.assertIn("+990 more", message)
        self.assertLess(len(message), 500, "the line is still too long to read")

    def test_nothing_is_logged_when_nothing_was_reaped(self) -> None:
        from agent_api import _log_reaped_sessions  # noqa: PLC0415

        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("sandbox-agent")
        old_level = logger.level
        logger.setLevel(logging.INFO)
        handler = _Capture()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, old_level)
        _log_reaped_sessions([])
        self.assertFalse(records)


if __name__ == "__main__":
    unittest.main()
