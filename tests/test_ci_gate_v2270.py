"""The coverage step of CI must not be able to hide a failing suite.

**What was wrong.** The step read:

    pytest --cov=... tests/ || coverage run ... && coverage report

The shell parses that as ``(A || B) && C``. So when **pytest failed**, the
fallback ran; if the fallback passed, ``coverage report`` ran and the step
ended with exit code 0. The step was green over a red suite — and the
comment above it claimed the fallback exists for the case where pytest is
*missing*, which is not what ``||`` does.

**Why it matters.** The step is a required check. A required check that
turns red pytest into green is not a gate: it is a light that stays on.
Nothing downstream can tell the difference between "the suite passed" and
"the suite failed and the fallback covered for it".

**What this pins.** The fallback may run only when pytest is **absent**.
The presence check is the condition, and the script contains no ``||`` at
all: a shell fallback that fires on failure is exactly what ``||`` buys,
and the step does not need one.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

COVERAGE_STEP = "Run unit tests with coverage"
STDLIB_STEP = "The suite also runs on the stdlib"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _step(name: str) -> str:
    """The body of one workflow step.

    The steps are read as text, not as YAML: PyYAML is not a dependency
    of this project, and a claim about a shell script is a claim about
    the text a shell will read.
    """
    text = _workflow()
    marker = f"- name: {name}\n"
    if marker not in text:
        raise AssertionError(f"the workflow has no step named {name!r}")
    rest = text[text.index(marker) + len(marker) :]
    end = re.search(r"\n      - (name|uses|run):", rest)
    return rest[: end.start()] if end else rest


def _script(step: str) -> str:
    """The shell of a step, with the comments taken out.

    A comment is not shell: a sentence about ``||`` in a comment must not
    be able to fail a check about ``||`` in the script.
    """
    return "\n".join(
        line for line in step.splitlines() if line.strip() and not line.strip().startswith("#")
    )


class TestTheCoverageStepCannotHideAFailingSuite(unittest.TestCase):
    def test_the_fallback_is_guarded_by_pytests_absence(self) -> None:
        script = _script(_step(COVERAGE_STEP))
        self.assertIn(
            "command -v pytest",
            script,
            "the fallback is not guarded by pytest's absence — it would run "
            "on a pytest that failed",
        )
        self.assertRegex(
            script,
            r"if command -v pytest[^\n]*; then",
            "the fallback is not behind an `if` — the presence check has to "
            "decide, not just exist in the file",
        )

    def test_the_script_has_no_or_shortcut(self) -> None:
        """No ``||`` anywhere in the step.

        ``A || B && C`` is ``(A || B) && C``: a red A is covered by B, and
        if B passes the step is green. That is the whole defect, and it is
        written with one character.
        """
        script = _script(_step(COVERAGE_STEP))
        self.assertNotIn(
            "||",
            script,
            "the coverage step still short-circuits on failure — a red pytest "
            "must fail the step, not be covered by a fallback",
        )

    def test_the_stdlib_promise_has_its_own_unconditional_step(self) -> None:
        """The "no pytest required" promise is walked by its own step.

        That step is the second net under the first one: it runs the same
        suite on the stdlib, so a failure that the first step could hide
        still turns the job red. It must not be conditional, or the net
        has a hole in it.
        """
        step = _step(STDLIB_STEP)
        self.assertIn("unittest discover", step, "the stdlib step no longer runs the suite")
        self.assertNotIn(
            "if:",
            step,
            "the stdlib step became conditional — the net under the first step has a hole in it",
        )

    def test_the_workflow_still_runs_the_suite(self) -> None:
        script = _script(_step(COVERAGE_STEP))
        self.assertIn("pytest", script, "the coverage step no longer runs pytest")
        self.assertIn("coverage report", script, "the coverage report is gone")


if __name__ == "__main__":
    unittest.main()
