"""The release claims of v2.14.7, pinned by tests.

A release note is a claim about the code. A claim that is not pinned by
a test is a claim that rots: it stays true until somebody edits the
code and forgets the sentence. These tests are the pin.

What v2.14.7 claims:

* the two services answer with this version;
* the changelog has the section, and the changelog is newest-first;
* the repository carries no community-conduct boilerplate: the
  Contributor Covenant template is gone, and the GitHub tab that reads
  it goes with it;
* what replaced it is already there — CONTRIBUTING.md states how a
  change is made and reviewed, and docs/SECURITY.md carries the threat
  model and the contact for security reports;
* the removal is not a silent one: the changelog says it happened.
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


class TestTheVersionIsTheRelease(unittest.TestCase):
    def test_the_services_answer_with_this_version(self) -> None:
        import agent_core  # noqa: PLC0415
        import proxy_core  # noqa: PLC0415

        self.assertEqual(agent_core.__version__, proxy_core.__version__)
        self.assertEqual(agent_core.__version__, "2.14.7")

    def test_the_changelog_has_the_section(self) -> None:
        self.assertIn("## [2.14.7]", _source("CHANGELOG.md"))

    def test_the_changelog_is_newest_first(self) -> None:
        headings = re.findall(r"^## \[(\d+)\.(\d+)\.(\d+)\]", _source("CHANGELOG.md"), re.MULTILINE)
        self.assertTrue(headings, "the changelog has no version sections")
        versions = [(int(a), int(b), int(c)) for a, b, c in headings]
        self.assertEqual(versions, sorted(versions, reverse=True))

    def test_the_removal_is_in_the_changelog(self) -> None:
        """A removal that is not recorded becomes a mystery later."""
        text = _prose("CHANGELOG.md")
        self.assertIn("CODE_OF_CONDUCT.md", text)
        self.assertIn("Removed", text)


class TestTheRepositoryCarriesNoCommunityBoilerplate(unittest.TestCase):
    """The file is gone, and it cannot come back by accident."""

    def test_the_file_is_not_in_the_repository(self) -> None:
        self.assertFalse(
            (ROOT / "CODE_OF_CONDUCT.md").exists(),
            "the community-conduct template is back in the repository",
        )

    def test_no_document_carries_the_protected_characteristic_list(self) -> None:
        """The vocabulary of the removed template is gone with it.

        The check is on the words, because those words are the claim:
        a document that names protected characteristics is the document
        that was removed, whatever it is called.
        """
        # The phrases are quoted whole, so that a normal engineering
        # phrase about an HTTP body is not caught by accident.
        words = (
            "harassment-free",
            "sex characteristics",
            "gender identity",
            "sexual orientation",
            "inclusive language",
            "body size, disability",
            "community members",
            "sexualised language",
        )
        for path in sorted(ROOT.rglob("*.md")):
            if ".git" in path.parts:
                continue
            text = " ".join(path.read_text(encoding="utf-8", errors="replace").split()).lower()
            for word in words:
                self.assertNotIn(
                    word,
                    text,
                    f"{path.name} still carries the vocabulary of the removed template",
                )

    def test_contributing_still_says_how_a_change_is_made(self) -> None:
        """What replaced the file was already there — and stays."""
        text = _prose("CONTRIBUTING.md")
        self.assertIn("Pull-request flow", text)
        self.assertIn("make test lint format", text)
        self.assertIn("Reporting bugs", text)
        self.assertIn("Security issues", text)

    def test_the_security_policy_still_carries_the_threat_model(self) -> None:
        security = _prose("docs/SECURITY.md")
        self.assertIn("Threat model", security)
        self.assertIn("Hardening checklist", security)

    def test_contributing_says_where_security_issues_go(self) -> None:
        text = _prose("CONTRIBUTING.md").replace("**", "")
        self.assertIn("Do not open a public GitHub issue for security bugs", text)
        self.assertIn("contact the maintainer directly", text)


if __name__ == "__main__":
    unittest.main()
