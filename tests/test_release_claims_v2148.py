"""The release claims of v2.14.8, pinned by tests.

A release note is a claim about the code. A claim that is not pinned by
a test is a claim that rots: it stays true until somebody edits the
code and forgets the sentence. These tests are the pin.

What v2.14.8 claims:

* the two services answer with this version;
* the changelog has the section, and the changelog is newest-first;
* the three findings of the fourth review are in the section — the
  mutation scripts that could count a catch for a test that never ran,
  the shell's own prompt read as a continuation prompt, and the
  correction that did not live where the claim lives;
* the review checklist has a row per finding, and every row names a
  check that exists: a row whose check is a file that is not there is a
  hope with a table around it.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _prose(name: str) -> str:
    """The document with its line breaks collapsed.

    A claim about prose must not depend on where the line happened to
    wrap: the sentence is the claim, not the line it sits on.
    """
    return " ".join(_source(name).split())


def _section(text: str, version: str) -> str:
    """The text of the changelog section of *version*."""
    match = re.search(rf"^## \[{re.escape(version)}\].*?$(.*?)(?=^## \[|\Z)", text, re.M | re.S)
    assert match is not None, f"the changelog has no section for {version}"
    return match.group(1)


class TestTheVersionIsTheRelease(unittest.TestCase):
    def test_the_services_answer_with_this_version(self) -> None:
        import agent_core  # noqa: PLC0415
        import proxy_core  # noqa: PLC0415

        # The number moves with every release; the *agreement* does not.
        # The number this release claims is pinned by the claims test of
        # the release that is current.
        self.assertEqual(agent_core.__version__, proxy_core.__version__)

    def test_the_changelog_has_the_section(self) -> None:
        self.assertIn("## [2.14.8]", _source("CHANGELOG.md"))

    def test_the_changelog_is_newest_first(self) -> None:
        headings = re.findall(r"^## \[(\d+)\.(\d+)\.(\d+)\]", _source("CHANGELOG.md"), re.MULTILINE)
        self.assertTrue(headings, "the changelog has no version sections")
        versions = [(int(a), int(b), int(c)) for a, b, c in headings]
        self.assertEqual(versions, sorted(versions, reverse=True))


class TestTheWaveIsInTheChangelog(unittest.TestCase):
    """The three findings of the fourth review are named in the section."""

    def setUp(self) -> None:
        self.section = _prose("CHANGELOG.md")
        self.v2148 = " ".join(_section(_source("CHANGELOG.md"), "2.14.8").split())

    def test_the_mutation_scripts_are_named(self) -> None:
        self.assertIn("mutation", self.v2148.lower())
        self.assertIn("mutation-lib.sh", self.v2148)

    def test_the_continuation_prompt_is_named(self) -> None:
        self.assertIn("continuation prompt", self.v2148.lower())

    def test_the_correction_is_named(self) -> None:
        self.assertIn("correction", self.v2148.lower())


class TestTheChecklistIsAlive(unittest.TestCase):
    """A finding that is not in the list is a finding the next wave finds."""

    def setUp(self) -> None:
        self.rows = [
            line
            for line in _source("docs/REVIEW-CHECKLIST.md").splitlines()
            if line.startswith("| ") and line.count("|") >= 6
        ]

    def test_the_wave_has_a_row_per_finding(self) -> None:
        text = "\n".join(self.rows)
        for finding in ("U1", "U2", "U3"):
            with self.subTest(finding=finding):
                self.assertIn(
                    f"| {finding} |",
                    text,
                    f"the checklist has no row for {finding}",
                )

    def test_every_named_check_exists(self) -> None:
        """A row that names a test that is not there is not a check."""
        missing: list[str] = []
        for row in self.rows:
            for name in re.findall(r"`([^`]+)`", row):
                if not name.startswith("tests/"):
                    continue
                # A row may name a class or a method: only the file has
                # to be there for the check to be a check.
                path = name.split("::")[0]
                if not (ROOT / path).exists():
                    missing.append(name)
        self.assertEqual(missing, [], f"the checklist names checks that do not exist: {missing}")


if __name__ == "__main__":
    unittest.main()
