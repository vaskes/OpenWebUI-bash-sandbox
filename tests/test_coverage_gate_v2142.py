"""The coverage gate measures the sources, and it is able to fail.

The gate used to be ``pytest --cov=. --cov-fail-under=70``. That
combination **cannot fail**: ``--cov=.`` measures the test files as well,
and the test files are ~95% covered by the act of running them, so the
TOTAL was ~95% whatever happened to the sources. The sources themselves
are covered at 89% — and a gate that sits at 70 while the real number
is 89 does not measure the thing it claims to measure.

Two things make the gate mean something, and both are pinned here:

* the tests are not part of the measurement — the coverage configuration
  omits them, and omits nothing else;
* the threshold lives in exactly **one** place — the coverage
  configuration — so the CI command, the Makefile and the configuration
  cannot disagree about it. A second copy of the number is a second
  number that rots.

The number is not written into this test: a test that pins the threshold
is a second copy of it. What is pinned is that the threshold exists, that
it is a real gate (not the meaningless 70, not an impossible 100), and
that it is alone.
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PYPROJECT = ROOT / "pyproject.toml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = ROOT / "Makefile"

#: The files that are allowed to hold the threshold. Exactly one of them
#: does.
THRESHOLD_HOMES = (PYPROJECT, CI_WORKFLOW, MAKEFILE)


def _config() -> dict:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


class TestTheCoverageGateMeasuresTheSources(unittest.TestCase):
    def test_the_tests_are_not_part_of_the_measurement(self) -> None:
        """``omit`` covers the test directory — in the run and in the report."""
        config = _config()
        coverage = config.get("tool", {}).get("coverage", {})
        self.assertIn("run", coverage, "pyproject.toml has no [tool.coverage.run] section")
        self.assertIn("report", coverage, "pyproject.toml has no [tool.coverage.report] section")
        for section in ("run", "report"):
            omit = coverage[section].get("omit", [])
            self.assertIn(
                "tests/*",
                omit,
                f"[tool.coverage.{section}] does not omit the tests — "
                "the measurement counts the test files and the gate cannot fail",
            )

    def test_only_the_tests_are_omitted(self) -> None:
        """An omit pattern that swallows a source module is a hole in the gate."""
        config = _config()
        coverage = config.get("tool", {}).get("coverage", {})
        for section in ("run", "report"):
            for pattern in coverage.get(section, {}).get("omit", []):
                self.assertTrue(
                    pattern.startswith("tests"),
                    f"[tool.coverage.{section}] omits {pattern!r} — "
                    "only the test directory may be excluded from the measurement",
                )

    def test_the_threshold_is_a_real_gate(self) -> None:
        """Not the meaningless 70, not the impossible 100."""
        report = _config().get("tool", {}).get("coverage", {}).get("report", {})
        self.assertIn("fail_under", report, "the coverage configuration has no threshold")
        value = report["fail_under"]
        self.assertIsInstance(value, (int, float), "the threshold must be a number")
        self.assertGreaterEqual(
            value, 80, "the sources are covered at 89% — a gate below 80 does not measure them"
        )
        self.assertLessEqual(value, 95, "a gate above the measured coverage can never be met")

    def test_the_threshold_lives_in_one_place_only(self) -> None:
        """One number, one home. A second copy rots."""
        holders = []
        for path in THRESHOLD_HOMES:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r"fail[-_]under", text):
                holders.append(path.name)
        self.assertEqual(
            holders,
            ["pyproject.toml"],
            f"the coverage threshold is named in {holders} — it must have exactly one home",
        )

    def test_the_ci_command_does_not_pass_a_threshold(self) -> None:
        """A threshold on the command line overrides the configuration."""
        text = CI_WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn(
            "--cov-fail-under",
            text,
            "the CI command passes its own threshold — the configuration's is ignored",
        )
        self.assertNotIn(
            "coverage report --fail-under",
            text,
            "the CI fallback passes its own threshold — the configuration's is ignored",
        )

    def test_the_makefile_does_not_pass_a_threshold(self) -> None:
        """The same for the command an operator types."""
        text = MAKEFILE.read_text(encoding="utf-8")
        self.assertNotIn("--cov-fail-under", text, "make test-cov passes its own threshold")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
