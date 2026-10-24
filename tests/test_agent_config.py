"""Tests for :mod:`agent_config` — the stdout and binary budgets.

Two things are worth pinning here, and neither is obvious from the code:

* a size value outside its documented range must abort start-up, not be
  clamped. A silently clamped limit is indistinguishable from a limit that
  was never applied, and the whole point of a configurable budget is that
  the operator can trust what it says;
* the tmux scrollback depth is *derived* from the stdout budget. A pane
  smaller than the budget loses the beginning of the output silently — no
  truncation marker, no error — so the two numbers have to move together.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_config  # noqa: E402


class TestParseSize(unittest.TestCase):
    def test_plain_bytes(self) -> None:
        self.assertEqual(agent_config.parse_size("1048576", name="X"), 1048576)

    def test_kilobytes(self) -> None:
        self.assertEqual(agent_config.parse_size("512k", name="X"), 512 * 1024)

    def test_megabytes(self) -> None:
        self.assertEqual(agent_config.parse_size("2m", name="X"), 2 * 1024 * 1024)

    def test_gigabytes(self) -> None:
        self.assertEqual(agent_config.parse_size("1g", name="X"), 1024 * 1024 * 1024)

    def test_case_and_space_insensitive(self) -> None:
        self.assertEqual(agent_config.parse_size("  512KB ", name="X"), 512 * 1024)

    def test_explicit_b_suffix(self) -> None:
        self.assertEqual(agent_config.parse_size("4096b", name="X"), 4096)

    def test_empty_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            agent_config.parse_size("", name="AGENT_STDOUT_LIMIT")

    def test_not_a_number_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            agent_config.parse_size("lots", name="AGENT_STDOUT_LIMIT")

    def test_unknown_suffix_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            agent_config.parse_size("512t", name="AGENT_STDOUT_LIMIT")

    def test_below_minimum_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            agent_config.parse_size("16k", name="AGENT_STDOUT_LIMIT", minimum=32 * 1024)
        self.assertIn("AGENT_STDOUT_LIMIT", str(ctx.exception))
        self.assertIn("below the minimum", str(ctx.exception))

    def test_above_maximum_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            agent_config.parse_size("2048k", name="AGENT_STDOUT_LIMIT", maximum=1024 * 1024)
        self.assertIn("above the maximum", str(ctx.exception))

    def test_zero_is_allowed_only_where_it_means_unlimited(self) -> None:
        self.assertEqual(agent_config.parse_size("0", name="X", minimum=0), 0)
        with self.assertRaises(ValueError):
            agent_config.parse_size("0", name="X", minimum=1)


class TestDefaults(unittest.TestCase):
    def test_stdout_default_is_512k(self) -> None:
        self.assertEqual(agent_config.STDOUT_LIMIT_BYTES, 512 * 1024)

    def test_binary_default_is_2048k(self) -> None:
        self.assertEqual(agent_config.BINARY_LIMIT_BYTES, 2048 * 1024)

    def test_stdout_range_is_32k_to_1024k(self) -> None:
        self.assertEqual(agent_config.STDOUT_LIMIT_MIN_BYTES, 32 * 1024)
        self.assertEqual(agent_config.STDOUT_LIMIT_MAX_BYTES, 1024 * 1024)


class TestScrollbackDerivation(unittest.TestCase):
    """The pane must be able to hold the budget it is promised."""

    def test_history_covers_the_budget(self) -> None:
        # A line of a 200-column pane holds 200 bytes, so the scrollback has
        # to be at least budget/200 lines deep or the output overflows.
        needed = -(-agent_config.STDOUT_LIMIT_BYTES // agent_config.PANE_WIDTH)
        self.assertGreaterEqual(agent_config.HISTORY_LIMIT_LINES, needed)

    def test_capture_covers_the_whole_scrollback(self) -> None:
        # A capture that cannot reach the whole scrollback truncates what
        # the pane still holds — the same silent loss, one layer up.
        self.assertGreaterEqual(
            agent_config.CAPTURE_DEPTH_LINES,
            agent_config.HISTORY_LIMIT_LINES + agent_config.PANE_HEIGHT,
        )

    def test_history_has_room_for_the_non_payload_lines(self) -> None:
        needed = -(-agent_config.STDOUT_LIMIT_BYTES // agent_config.PANE_WIDTH)
        self.assertGreater(agent_config.HISTORY_LIMIT_LINES - needed, 0)

    def test_derivation_holds_for_every_allowed_budget(self) -> None:
        for limit in (32 * 1024, 512 * 1024, 1024 * 1024):
            needed = -(-limit // agent_config.PANE_WIDTH)
            self.assertGreaterEqual(needed + agent_config.HISTORY_MARGIN_LINES, needed)
            self.assertGreater(agent_config.HISTORY_MARGIN_LINES, 0)
            # and the depth the agent would derive for that budget
            self.assertGreaterEqual(
                needed + agent_config.HISTORY_MARGIN_LINES + agent_config.PANE_HEIGHT,
                needed,
            )


class TestEnvOverrides(unittest.TestCase):
    """The budgets are read from the environment, and a bad value is fatal."""

    def _run_config(self, env: dict[str, str]) -> subprocess.CompletedProcess:
        code = (
            "import agent_config, json;"
            "print(json.dumps({'stdout': agent_config.STDOUT_LIMIT_BYTES,"
            " 'binary': agent_config.BINARY_LIMIT_BYTES,"
            " 'history': agent_config.HISTORY_LIMIT_LINES}))"
        )
        child_env = {**os.environ, **env}
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            env=child_env,
            timeout=60,
            check=False,
        )

    def test_stdout_limit_is_configurable(self) -> None:
        result = self._run_config({"AGENT_STDOUT_LIMIT": "64k"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"stdout": 65536', result.stdout)

    def test_binary_limit_is_configurable(self) -> None:
        result = self._run_config({"AGENT_BINARY_LIMIT": "10m"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"binary": 10485760', result.stdout)

    def test_out_of_range_stdout_limit_aborts_startup(self) -> None:
        result = self._run_config({"AGENT_STDOUT_LIMIT": "2048k"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AGENT_STDOUT_LIMIT", result.stderr)

    def test_unparseable_limit_aborts_startup(self) -> None:
        result = self._run_config({"AGENT_STDOUT_LIMIT": "half a megabyte"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AGENT_STDOUT_LIMIT", result.stderr)

    def test_binary_limit_zero_means_unlimited(self) -> None:
        result = self._run_config({"AGENT_BINARY_LIMIT": "0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"binary": 0', result.stdout)
