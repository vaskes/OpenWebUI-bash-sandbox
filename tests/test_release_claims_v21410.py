"""The release claims of v2.14.10, pinned by tests.

A release note is a claim about the code. A claim that is not pinned by
a test is a claim that rots: it stays true until somebody edits the
code and forgets the sentence. These tests are the pin.

What v2.14.10 claims:

* the required status checks are listed in the tree, and the list is
  compared with the workflow in both directions;
* the contribution guide says where the list lives and does not keep a
  copy of the count;
* the branch protection settings are the ones the tree describes:
  force pushes and deletion refused, a linear history, administrators
  gated too, and no review required;
* the mutation library can break a fact that lives in more than one
  place — every copy, not the first;
* the shipped version is the release the changelog's newest section
  describes, and the two services agree on it.
"""

from __future__ import annotations

import os
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


class TestTheCanonicalListExists(unittest.TestCase):
    """The list of required checks has to be somewhere the tree can see."""

    def test_the_list_is_in_the_tree(self) -> None:
        self.assertTrue(
            (ROOT / "docs" / "required-status-checks.txt").is_file(),
            "the required checks are not listed in the tree — a list that "
            "lives only in GitHub's settings cannot be reviewed",
        )

    def test_the_mutation_script_is_in_the_tree(self) -> None:
        script = ROOT / "tools" / "mutation-check-branch-protection.sh"
        self.assertTrue(script.is_file(), "the mutation script for this wave is missing")
        self.assertTrue(
            os.access(script, os.X_OK),
            "the mutation script is not executable — the CI job would skip it",
        )


class TestTheGuidePointsAtTheList(unittest.TestCase):
    def test_the_guide_names_the_canonical_list(self) -> None:
        self.assertIn(
            "docs/required-status-checks.txt",
            _prose("CONTRIBUTING.md"),
            "the contribution guide does not say where the required checks live",
        )

    def test_the_guide_does_not_count_the_checks_in_prose(self) -> None:
        """`thirteen checks` is a number that rots; the file is the list."""
        text = _prose("CONTRIBUTING.md").lower()
        for phrase in ("thirteen checks", "fourteen checks", "twelve checks"):
            with self.subTest(phrase=phrase):
                self.assertNotIn(
                    phrase,
                    text,
                    "the guide counts the required checks in prose — that "
                    "number goes stale silently. Point at the list instead.",
                )


class TestTheMutationLibraryBreaksEveryCopy(unittest.TestCase):
    """A fact in two places needs both copies broken.

    `mutation_apply()` replaced the *first* occurrence of the pattern.
    When the property under test lives in two places, breaking one of
    them left the other intact: the property survived, and the script
    reported a verdict about a tree that had not really been broken.
    """

    def test_the_library_has_a_replace_every_variant(self) -> None:
        lib = _source("tools/mutation-lib.sh")
        self.assertIn(
            "mutation_apply_all()",
            lib,
            "the mutation library cannot break every copy of a fact",
        )
        self.assertIn(
            "mutate_all()",
            lib,
            "the mutation library has no wrapper for the all-copies case",
        )

    def test_the_all_copies_variant_replaces_every_occurrence(self) -> None:
        """Not a phrase check: the helper must really replace them all."""
        import subprocess  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        source = _source("tools/mutation-lib.sh")
        body = source.split("mutation_apply_all() {", 1)[1].split("\n}", 1)[0]
        # The helper is a bash function that calls python; exercise the
        # python it calls, not the bash around it.
        python = body.split("<<'PY'", 1)[1].rsplit("PY", 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "apply_all.py"
            script.write_text(python, encoding="utf-8")
            target = Path(tmp) / "sample.txt"
            target.write_text("a X b X c", encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(script), str(target), "X", "Y"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), "a Y b Y c")
            self.assertEqual(result.stdout.strip(), "2", "the helper did not report the count")

    def test_a_missing_pattern_is_an_error(self) -> None:
        """A mutation that does not apply proves nothing."""
        import subprocess  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        source = _source("tools/mutation-lib.sh")
        body = source.split("mutation_apply_all() {", 1)[1].split("\n}", 1)[0]
        python = body.split("<<'PY'", 1)[1].rsplit("PY", 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "apply_all.py"
            script.write_text(python, encoding="utf-8")
            target = Path(tmp) / "sample.txt"
            target.write_text("nothing to see", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(script), str(target), "X", "Y"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, "a pattern that is gone must fail")
            self.assertIn("PATTERN NOT FOUND", result.stderr)


class TestTheShippedVersionIsTheRelease(unittest.TestCase):
    def test_the_services_agree(self) -> None:
        import agent_core  # noqa: PLC0415
        import proxy_core  # noqa: PLC0415

        self.assertEqual(agent_core.__version__, proxy_core.__version__)

    def test_the_shipped_version_is_the_newest_changelog_section(self) -> None:
        """Not a literal written into a test file — that rots every release."""
        import agent_core  # noqa: PLC0415

        match = re.search(r"^## \[([0-9]+\.[0-9]+\.[0-9]+)\]", _source("CHANGELOG.md"), re.M)
        assert match is not None, "the changelog has no version section"
        self.assertEqual(agent_core.__version__, match.group(1))


class TestTheChangelogSectionIsThere(unittest.TestCase):
    def setUp(self) -> None:
        self.section = _section(_source("CHANGELOG.md"), "2.14.10")

    def test_the_section_names_the_canonical_list(self) -> None:
        self.assertIn("required-status-checks.txt", self.section)

    def test_the_section_names_the_tripwire(self) -> None:
        self.assertIn("test_branch_protection.py", self.section)

    def test_the_section_names_the_mutation_script(self) -> None:
        self.assertIn("mutation-check-branch-protection.sh", self.section)

    def test_the_section_names_the_library_fix(self) -> None:
        self.assertIn("mutation_apply_all", self.section)

    def test_the_section_names_the_measured_brick(self) -> None:
        self.assertIn("permanently closed", self.section)

    def test_the_section_names_the_enforce_admins_measurement(self) -> None:
        self.assertIn("enforce_admins", self.section)

    def test_the_changelog_is_newest_first(self) -> None:
        headings = re.findall(r"^## \[(\d+)\.(\d+)\.(\d+)\]", _source("CHANGELOG.md"), re.MULTILINE)
        self.assertTrue(headings, "the changelog has no version sections")
        versions = [(int(a), int(b), int(c)) for a, b, c in headings]
        self.assertEqual(versions, sorted(versions, reverse=True))


if __name__ == "__main__":
    unittest.main()
