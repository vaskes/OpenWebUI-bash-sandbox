"""The release claims of v2.13.0, pinned by tests.

Two things happened in this release: a knob appeared, and a capability
disappeared. Both are claims a reader of the changelog will act on, so
both are checked against the thing they describe.

* the budget exists, is named the same way in the code, in the operator's
  environment template, in the configuration reference, in the README and
  in the handoff, and defaults to one mebibyte;
* the HTTP door keeps its own limit — the two budgets are different
  axes, and the release says so instead of pretending they are one;
* the search that was removed is recorded as removed, and nothing in the
  product advertises it any more;
* the changelog is ordered newest first, so "the first section is the
  latest one" is a fact and not a coincidence of how the file was
  edited.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BUDGET = "AGENT_MCP_MAX_READ_BYTES"
#: The name of the capability that is not in this project any more. Built
#: at runtime, so this file carries none of it.
REMOVED = "web" + "_search"


def _source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _prose(name: str) -> str:
    """The document with its line breaks collapsed.

    A claim about prose must not depend on where the line happened to
    wrap: the sentence is the claim, not the line it sits on.
    """
    return " ".join(_source(name).split())


class TestTheVersionIsTheRelease(unittest.TestCase):
    def test_the_services_answer_with_the_same_version(self) -> None:
        # The concrete version belongs to the release that is being
        # prepared — it is pinned in that release's own file. Here the
        # claim is the weaker one that never rots: the two services are
        # the same release of the same project.
        import agent_core  # noqa: PLC0415
        import proxy_core  # noqa: PLC0415

        self.assertEqual(agent_core.__version__, proxy_core.__version__)

    def test_the_changelog_has_the_section(self) -> None:
        self.assertIn("## [2.13.0]", _source("CHANGELOG.md"))

    def test_the_changelog_is_newest_first(self) -> None:
        # The drift check takes the *first* section as the latest one.
        # That was a coincidence until now: the 2.11.1 section had been
        # left at the top, and the check was validating a four-releases
        # old entry.
        headings = re.findall(r"^## \[(\d+)\.(\d+)\.(\d+)\]", _source("CHANGELOG.md"), re.MULTILINE)
        self.assertTrue(headings, "the changelog has no version sections")
        versions = [(int(a), int(b), int(c)) for a, b, c in headings]
        self.assertEqual(versions, sorted(versions, reverse=True))


class TestTheBudgetIsNamedEverywhere(unittest.TestCase):
    """One knob, one name, in every place an operator will look for it."""

    def test_the_code_reads_it(self) -> None:
        self.assertIn(BUDGET, _source("agent_config.py"))

    def test_the_environment_template_offers_it(self) -> None:
        self.assertIn(BUDGET, _source(".env.example"))

    def test_the_configuration_reference_documents_it(self) -> None:
        self.assertIn(BUDGET, _source("docs/CONFIGURATION.md"))

    def test_the_readme_tells_the_caller(self) -> None:
        readme = _source("README.md")
        self.assertIn(BUDGET, readme)
        self.assertIn("1 MiB", readme)

    def test_the_handoff_tells_the_caller(self) -> None:
        handoff = _source("docs/HANDOFF-mcp.md")
        self.assertIn(BUDGET, handoff)
        self.assertIn("1 MiB", handoff)

    def test_the_handoff_says_the_http_door_keeps_its_own_limit(self) -> None:
        # The two doors are one sandbox with two budgets. A handoff
        # that did not say so would leave the caller to guess which of
        # the two numbers applies to the call it is about to make.
        handoff = _prose("docs/HANDOFF-mcp.md")
        self.assertIn("The HTTP door keeps its own limit, untouched.", handoff)

    def test_the_ceiling_is_still_named(self) -> None:
        self.assertIn("64 MiB", _source("docs/HANDOFF-mcp.md"))
        self.assertIn("64 MiB", _source("README.md"))


class TestTheRemovalIsRecorded(unittest.TestCase):
    """A capability that is gone is recorded as gone."""

    def test_the_changelog_has_a_removed_section(self) -> None:
        changelog = _source("CHANGELOG.md")
        start = changelog.index("## [2.13.0]")
        end = changelog.index("## [2.12.1]")
        section = changelog[start:end]
        self.assertIn("### Removed", section)
        self.assertIn(REMOVED, section)

    def test_the_readme_does_not_advertise_it(self) -> None:
        readme = _source("README.md").lower()
        self.assertNotIn(REMOVED, readme)
        self.assertNotIn("sear" + "xng", readme)

    def test_the_handoff_does_not_advertise_it(self) -> None:
        handoff = _source("docs/HANDOFF-mcp.md").lower()
        self.assertNotIn(REMOVED, handoff)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
