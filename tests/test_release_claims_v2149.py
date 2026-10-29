"""The release claims of v2.14.9, pinned by tests.

A release note is a claim about the code. A claim that is not pinned by
a test is a claim that rots: it stays true until somebody edits the
code and forgets the sentence. These tests are the pin.

What v2.14.9 claims:

* the two services answer with this version;
* the changelog has the section, and the changelog is newest-first;
* the documentation audit is in the section — the configuration table
  that advertised a timeout the proxy does not ship, the docs that
  claimed the compose file lacks the bind address, the production
  stack's token that was documented nowhere, the handoff contract that
  was pinned to a version seven releases old and did not name the 409,
  the metrics table that did not name two of the nine metrics, the
  template's knobs that could not be turned and the knobs it did not
  offer, and the checklist that counted three waves in a file listing
  four;
* the stack refuses a template placeholder as a token, on every path
  and not only through ``make``;
* the drift tripwire and the placeholder tripwire exist, and the
  mutation script that breaks their properties on purpose is there too.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VERSION = "2.14.9"


def _newest_section_version() -> str:
    """The version of the newest ``## [X.Y.Z]`` section of the changelog.

    The version a release claims is the version the changelog's **newest**
    section describes — not a literal written into this file. A literal
    here is a number that rots: it goes stale on the next release, and
    then it is either edited (so the test says nothing) or left (so the
    suite is red for a reason that has nothing to do with the code).
    """
    match = re.search(r"^## \[([0-9]+\.[0-9]+\.[0-9]+)\]", _source("CHANGELOG.md"), re.M)
    assert match is not None, "the changelog has no version section"
    return match.group(1)


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
        # The shipped version is the release the changelog's newest section
        # describes — not a literal pinned in this file, which would rot on
        # the next release.
        self.assertEqual(agent_core.__version__, _newest_section_version())

    def test_the_changelog_has_the_section(self) -> None:
        self.assertIn(f"## [{VERSION}]", _source("CHANGELOG.md"))

    def test_the_changelog_is_newest_first(self) -> None:
        headings = re.findall(r"^## \[(\d+)\.(\d+)\.(\d+)\]", _source("CHANGELOG.md"), re.MULTILINE)
        self.assertTrue(headings, "the changelog has no version sections")
        versions = [(int(a), int(b), int(c)) for a, b, c in headings]
        self.assertEqual(versions, sorted(versions, reverse=True))


class TestTheAuditIsInTheChangelog(unittest.TestCase):
    """Every finding of the documentation audit is named in the section."""

    def setUp(self) -> None:
        self.section = " ".join(_section(_source("CHANGELOG.md"), VERSION).split())

    def test_the_timeout_table_is_named(self) -> None:
        self.assertIn("AGENT_EXEC_TIMEOUT", self.section)

    def test_the_bind_address_claim_is_named(self) -> None:
        self.assertIn("SANDBOX_BIND_HOST", self.section)

    def test_the_production_token_is_named(self) -> None:
        self.assertIn("AGENT_STACK_TOKEN", self.section)

    def test_the_handoff_contract_is_named(self) -> None:
        self.assertIn("HANDOFF-binary-transport", self.section)
        self.assertIn("409", self.section)

    def test_the_metrics_table_is_named(self) -> None:
        self.assertIn("sandbox_agent_exec_refused_total", self.section)
        self.assertIn("sandbox_agent_exec_stuck_resets_total", self.section)

    def test_the_template_knobs_are_named(self) -> None:
        self.assertIn("OPENAPI_PROXY_UPSTREAM_TOKEN", self.section)
        self.assertIn("AGENT_LOG_LEVEL", self.section)

    def test_the_placeholder_rule_is_named(self) -> None:
        self.assertIn("CHANGE_ME", self.section)
        self.assertIn("503", self.section)

    def test_the_checklist_count_is_named(self) -> None:
        self.assertIn("REVIEW-CHECKLIST", self.section)


class TestTheTripwiresExist(unittest.TestCase):
    """A claim that names no check is not a claim anybody keeps."""

    def test_the_drift_tripwire_exists(self) -> None:
        self.assertTrue((ROOT / "tests" / "test_doc_drift_v2149.py").exists())

    def test_the_placeholder_tripwire_exists(self) -> None:
        self.assertTrue((ROOT / "tests" / "test_placeholder_token_v2149.py").exists())

    def test_the_mutation_script_exists(self) -> None:
        script = ROOT / "tools" / "mutation-check-v2149.sh"
        self.assertTrue(script.exists(), "the wave's mutation script is missing")
        self.assertIn("mutation-lib.sh", script.read_text(encoding="utf-8"))

    def test_the_shared_rule_module_exists(self) -> None:
        self.assertTrue((ROOT / "token_rules.py").exists())


class TestTheDocumentationClaimsAreTrue(unittest.TestCase):
    """The claims the release makes about the docs, checked against them."""

    def test_the_configuration_table_ships_the_shipped_timeout(self) -> None:
        configuration = _source("docs/CONFIGURATION.md")
        row = next(
            line
            for line in configuration.splitlines()
            if line.startswith("|") and "AGENT_EXEC_TIMEOUT" in line
        )
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        self.assertEqual(cells[1].strip("`"), "60")

    def test_the_docs_do_not_say_the_compose_lacks_the_bind_host(self) -> None:
        for name in ("README.md", "docs/CONFIGURATION.md"):
            with self.subTest(document=name):
                self.assertNotIn("not written into the compose file", _source(name))

    def test_the_review_checklist_counts_the_waves_it_lists(self) -> None:
        """The prose count must match the table it introduces.

        The count may be written as a digit or as a word — the claim is
        that it matches the table, not how it is spelled.
        """
        checklist = _source("docs/REVIEW-CHECKLIST.md")
        waves = {int(m) for m in re.findall(r"^\| (\d+) ", checklist, re.MULTILINE)}
        self.assertTrue(waves, "the checklist has no wave rows")
        prose = " ".join(checklist.split())
        words = {
            1: "one",
            2: "two",
            3: "three",
            4: "four",
            5: "five",
            6: "six",
            7: "seven",
            8: "eight",
        }
        count = len(waves)
        accepted = {f"{count} numbered review waves", f"{words.get(count)} numbered review waves"}
        self.assertTrue(
            any(phrase in prose for phrase in accepted),
            f"the checklist lists {count} numbered waves, but its prose does not say so "
            f"(expected one of: {sorted(accepted)})",
        )


if __name__ == "__main__":
    unittest.main()
