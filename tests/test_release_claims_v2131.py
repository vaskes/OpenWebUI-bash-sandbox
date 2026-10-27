"""The release claims of v2.13.1, pinned by tests.

A release note is a claim about the code. A claim that is not pinned by
a test is a claim that rots: it stays true until somebody edits the
code and forgets the sentence. These tests are the pin.

What v2.13.1 claims:

* the two services answer with this version;
* the changelog has the section, and the changelog is newest-first;
* the code carries the fix — a command that never started answers with
  an empty stdout and a named reason;
* the caller is told about it: the handoff, the readme and the
  architecture reference say what a busy session answers with.
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

        self.assertEqual(agent_core.__version__, "2.13.1")
        self.assertEqual(proxy_core.__version__, "2.13.1")

    def test_the_changelog_has_the_section(self) -> None:
        self.assertIn("## [2.13.1]", _source("CHANGELOG.md"))

    def test_the_changelog_is_newest_first(self) -> None:
        headings = re.findall(r"^## \[(\d+)\.(\d+)\.(\d+)\]", _source("CHANGELOG.md"), re.MULTILINE)
        self.assertTrue(headings, "the changelog has no version sections")
        versions = [(int(a), int(b), int(c)) for a, b, c in headings]
        self.assertEqual(versions, sorted(versions, reverse=True))


class TestTheFixIsInTheCode(unittest.TestCase):
    def test_the_busy_notice_is_in_the_agent(self) -> None:
        self.assertIn("_BUSY_NOTICE", _source("agent_api.py"))

    def test_the_notice_says_the_command_did_not_start(self) -> None:
        self.assertIn("the command did not start", _source("agent_api.py"))

    def test_the_full_pane_is_consulted(self) -> None:
        # Without the whole scrollback the marker cannot be found, and a
        # command that did run looks like one that never started.
        self.assertIn("_capture_full_pane", _source("agent_api.py"))

    def test_the_scrollback_is_the_tiebreaker(self) -> None:
        self.assertIn("_scrollback_is_full", _source("agent_api.py"))


class TestTheCallerIsTold(unittest.TestCase):
    def test_the_handoff_says_what_a_busy_session_answers(self) -> None:
        handoff = _prose("docs/HANDOFF-mcp.md")
        self.assertIn("A busy session answers with a reason, not with the pane.", handoff)

    def test_the_readme_says_what_a_busy_session_answers(self) -> None:
        readme = _prose("README.md")
        self.assertIn("A busy session answers with a reason, not with the pane.", readme)

    def test_the_architecture_names_the_failure_mode(self) -> None:
        architecture = _prose("docs/ARCHITECTURE.md")
        self.assertIn("the session is busy", architecture)

    def test_the_readme_does_not_claim_a_different_number_of_differences(self) -> None:
        # The list of differences between the doors is a claim about the
        # number of bullets that follow it. It said "four" over three
        # bullets until the busy session became the fourth.
        readme = _source("README.md")
        claimed = re.search(
            r"^(One|Two|Three|Four|Five|Six) things about the MCP door differ",
            readme,
            re.MULTILINE,
        )
        self.assertIsNotNone(claimed, "the readme does not say how many differences there are")
        start = claimed.start()
        end = readme.index("## Monitoring", start)
        section = readme[start:end]
        words = {
            "One": 1,
            "Two": 2,
            "Three": 3,
            "Four": 4,
            "Five": 5,
            "Six": 6,
        }
        bullets = len(re.findall(r"^- \*\*", section, re.MULTILINE))
        self.assertEqual(
            words[claimed.group(1)],
            bullets,
            f"the readme claims {claimed.group(1)} differences and lists {bullets}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
