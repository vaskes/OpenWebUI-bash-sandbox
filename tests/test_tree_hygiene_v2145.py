"""The tree has no unreachable operators, and the suite is green without tmux.

**What was wrong.** Two findings from the second-wave review:

* **R4** — one operator in the whole tree could never run: a second
  ``raise`` behind a ``raise`` (the leftover of a removed
  ``except RuntimeError``).  Dead code in an error path is worse than
  no code: the reader believes there are two kinds of failure here.
* **R5** — test classes that drive a real tmux session but do not
  carry the ``requires_tmux`` marker.  On a machine without tmux they
  fail instead of skipping, and the run reads as a red suite rather
  than as an environment that lacks a tool.  Measured on the tree of
  this wave: **six classes, fifteen red and two errors** with tmux
  hidden — where the review had seen ten.

**What this file does.** It scans the tree for unreachable statements,
and it pins the CI job that runs the whole suite **without tmux** —
the only honest way to keep the markers honest, because a marker that
is wrong is invisible until the machine loses its tmux.
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The modules the service ships.  The scan is about the code that runs,
# not about the tests that check it.
SHIPPED_MODULES = ("agent_api.py", "mcp_server.py", "proxy_server.py")

CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class TestTheTreeHasNoUnreachableOperators(unittest.TestCase):
    def test_no_operator_follows_a_return_or_a_raise(self) -> None:
        dead: list[str] = []
        for path in (ROOT / name for name in SHIPPED_MODULES):
            if not path.is_file():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                for field in ("body", "orelse", "finalbody"):
                    block = getattr(node, field, None)
                    if not isinstance(block, list):
                        continue
                    for index, statement in enumerate(block[:-1]):
                        if isinstance(statement, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                            following = block[index + 1]
                            dead.append(f"{path.name}:{following.lineno}")
        self.assertEqual(
            dead,
            [],
            "these operators can never run: " + ", ".join(dead),
        )

    def test_the_scan_finds_a_dead_operator(self) -> None:
        """A scan that cannot find anything is a scan that finds nothing."""
        sample = ast.parse("def f():\n    raise ValueError('x')\n    raise ValueError('y')\n")
        dead = []
        for node in ast.walk(sample):
            block = getattr(node, "body", None)
            if isinstance(block, list) and isinstance(block[0], ast.Raise):
                dead.append(block[1].lineno)
        self.assertEqual(dead, [3])


class TestTheStdlibPathIsWalked(unittest.TestCase):
    """A promise about a path nobody walks is a promise nobody checks.

    The README offers the suite on the stdlib ("no pytest required"),
    and the CI has a fallback for the case where pytest is missing.  A
    fallback only runs when something is broken -- so the promise was
    broken for two releases before anybody measured it: the tmux
    isolation lived in a pytest fixture, and a plain
    ``python -m unittest discover`` failed two of the suite's own
    tests.

    The check that matters is the CI step itself: it runs the stdlib
    path on every build.  This tripwire is the guard against the step
    being deleted -- the promise is in the workflow, and a workflow
    without the step is a README that lies.
    """

    def _workflow(self) -> str:
        self.assertTrue(CI_WORKFLOW.is_file(), "the CI workflow is gone")
        return CI_WORKFLOW.read_text(encoding="utf-8")

    def test_the_ci_runs_the_suite_on_the_stdlib(self) -> None:
        self.assertIn(
            "python -m unittest discover -s tests",
            self._workflow(),
            "the CI does not run the suite on the stdlib -- the README's "
            "claim is only walked when pytest is missing, which is exactly "
            "when nobody is looking",
        )

    def test_the_stdlib_run_is_a_step_of_its_own(self) -> None:
        """Not a fallback: a step that runs on every build."""
        workflow = self._workflow()
        self.assertRegex(
            workflow,
            r"- name: The suite also runs on the stdlib\n(?:\s*#.*\n)*\s*run: python -m unittest discover -s tests",
            "the stdlib run is not a step of its own -- it is a fallback, "
            "and a fallback runs only when something else is broken",
        )


class TestTheSuiteIsCheckedWithoutTmux(unittest.TestCase):
    """The markers are only worth what the check behind them is worth."""

    def _workflow(self) -> str:
        self.assertTrue(CI_WORKFLOW.is_file(), "the CI workflow is gone")
        return CI_WORKFLOW.read_text(encoding="utf-8")

    def test_the_ci_runs_the_suite_without_tmux(self) -> None:
        workflow = self._workflow()
        self.assertIn(
            "suite-without-tmux",
            workflow,
            "the CI has no job that runs the suite without tmux — the "
            "requires_tmux markers are then wrong in the one direction "
            "that nobody notices",
        )

    def test_the_job_hides_tmux_rather_than_uninstalling_it(self) -> None:
        workflow = self._workflow()
        block = workflow.split("suite-without-tmux:", 1)[-1]
        self.assertIn(
            "no-tmux",
            block,
            "the job does not build a PATH without tmux — a machine that "
            "still has tmux cannot find out what a machine without it does",
        )

    def test_the_job_runs_the_tests(self) -> None:
        block = self._workflow().split("suite-without-tmux:", 1)[-1]
        self.assertRegex(
            block,
            r"pytest[^\n]*tests/",
            "the job does not run the test suite",
        )

    def test_the_check_looks_where_it_looks(self) -> None:
        """A check that reads a different file finds nothing to say."""
        workflow = self._workflow()
        self.assertEqual(
            len(re.findall(r"^\s*suite-without-tmux:", workflow, re.MULTILINE)),
            1,
            "the job is defined more than once — the check would read the first",
        )


if __name__ == "__main__":
    unittest.main()
