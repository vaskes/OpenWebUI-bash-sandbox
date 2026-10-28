"""The tripwires have to run where somebody will notice.

The mutation checks in ``tools/`` are the deepest tripwires of this
repository: each one breaks a property of the code on purpose and
expects the named test to go red. Two things can go wrong with them,
and both are silent:

* the **pattern** drifts away from the code — the mutation does not
  apply, the script writes ``SKIP`` into a line nobody reads, and the
  property is not protected by anything. (Four of them had drifted
  before v2.14.1; a test now watches the patterns.)
* the **script** is never run. There is no way to notice a rotten
  check except by running it by hand, and a check that runs only when
  somebody remembers it is a check that decays.

So the scripts run in CI, on every push, and the privilege mode's
warning is checked in the log of a container that really has sudo in
it — the claim the documentation makes about the *shipped image*, not
about a function.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The one canonical way to name the whole family of scripts. The CI
#: job and the Makefile target must use the same one, so a new script
#: is picked up without anybody remembering to add it to a list.
SCRIPT_GLOB = "tools/mutation-check-*.sh"


def _source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _prose(name: str) -> str:
    """The document with its line breaks collapsed.

    A claim about prose must not depend on where the line happened to
    wrap: the sentence is the claim, not the line it sits on.
    """
    return " ".join(_source(name).split())


class TestTheMutationChecksRunInCI(unittest.TestCase):
    def test_the_workflow_runs_the_whole_family(self) -> None:
        workflow = _source(".github/workflows/ci.yml")
        self.assertIn(
            SCRIPT_GLOB,
            workflow,
            "the mutation checks are not run in CI — a check that nobody "
            "runs decays, and a decayed check protects nothing",
        )

    def test_the_workflow_has_a_job_for_them(self) -> None:
        workflow = _source(".github/workflows/ci.yml")
        self.assertIn("Mutation checks", workflow)
        self.assertIn("mutation-checks:", workflow)

    def test_the_scripts_there_are_the_scripts_that_run(self) -> None:
        """The family is named by a glob, not by a list.

        A script named one by one has to be added to a list somewhere —
        and a list is where things rot. The glob means a new script
        joins the CI job on its own.
        """
        on_disk = sorted(p.name for p in (ROOT / "tools").glob("mutation-check-*.sh"))
        self.assertTrue(on_disk, "there are no mutation scripts at all")
        workflow = _source(".github/workflows/ci.yml")
        for name in on_disk:
            self.assertNotIn(
                name,
                workflow,
                f"{name} is named in the workflow — a script named one by "
                "one has to be added to a list, and a list is where the "
                "family rots",
            )

    def test_the_makefile_runs_them_too(self) -> None:
        makefile = _source("Makefile")
        self.assertIn("mutation:", makefile, "there is no `make mutation`")
        self.assertIn(SCRIPT_GLOB, makefile)

    def test_contributing_says_how_to_run_them(self) -> None:
        self.assertIn(
            "## Mutation checks",
            _source("CONTRIBUTING.md"),
            "the contribution guide does not tell a newcomer how to run the mutation checks",
        )
        self.assertRegex(
            _source("CONTRIBUTING.md"),
            r"make mutation\s+# or:\s+for s in tools/mutation-check-\*\.sh",
            "the contribution guide does not say how to run the mutation checks",
        )


class TestThePrivilegeWarningIsCheckedLive(unittest.TestCase):
    """The claim is about the shipped image, not about a function."""

    def test_the_workflow_looks_for_the_warning_in_the_log(self) -> None:
        workflow = _source(".github/workflows/ci.yml")
        self.assertIn(
            "the privilege mode is ON",
            workflow,
            "the privilege mode is never checked in the log of a container that has sudo in it",
        )

    def test_the_workflow_names_the_hollow_root_too(self) -> None:
        workflow = _source(".github/workflows/ci.yml")
        self.assertIn("the root filesystem is READ-ONLY", workflow)
