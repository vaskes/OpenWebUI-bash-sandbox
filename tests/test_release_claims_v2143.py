"""The release claims of v2.14.3, pinned by tests.

A release note is a claim about the code. A claim that is not pinned by
a test is a claim that rots: it stays true until somebody edits the
code and forgets the sentence. These tests are the pin.

What v2.14.3 claims:

* the two services answer with this version;
* the changelog has the section, and the changelog is newest-first;
* the warning about the privilege mode is a plain function — it is
  called as one, and it returns nothing;
* the start-up calls it, so the warning is not decoration;
* the documentation still promises what the code does: a warning at
  start-up when ``sudo`` is present.
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

        self.assertEqual(agent_core.__version__, "2.14.3")
        self.assertEqual(proxy_core.__version__, "2.14.3")

    def test_the_changelog_has_the_section(self) -> None:
        self.assertIn("## [2.14.3]", _source("CHANGELOG.md"))

    def test_the_changelog_is_newest_first(self) -> None:
        headings = re.findall(r"^## \[(\d+)\.(\d+)\.(\d+)\]", _source("CHANGELOG.md"), re.MULTILINE)
        self.assertTrue(headings, "the changelog has no version sections")
        versions = [(int(a), int(b), int(c)) for a, b, c in headings]
        self.assertEqual(versions, sorted(versions, reverse=True))


class TestTheWarningIsWhatTheCodeDoes(unittest.TestCase):
    def test_the_warning_is_not_dressed_as_a_context_manager(self) -> None:
        text = _source("agent_api.py")
        self.assertNotIn(
            "@asynccontextmanager\ndef _warn_about_the_privilege_mode",
            text,
            "the warning is a plain function — a context manager in costume "
            "does its work by accident, and `async with` on it raises TypeError",
        )

    def test_the_startup_calls_the_warning(self) -> None:
        text = _source("agent_api.py")
        self.assertIn(
            "    _warn_about_the_privilege_mode()",
            text,
            "nothing calls the warning at start-up, so it prints nothing",
        )

    def test_the_warning_says_something(self) -> None:
        text = _source("agent_api.py")
        self.assertIn("the privilege mode is ON", text)
        self.assertIn("the escalation would NOT work", text)


class TestTheDocumentationStillPromisesIt(unittest.TestCase):
    """The promise the review read is the promise the code keeps."""

    def test_security_md_says_the_agent_warns_at_startup(self) -> None:
        self.assertIn(
            "logs a warning at startup when `sudo` is present",
            _prose("docs/SECURITY.md"),
        )

    def test_configuration_md_says_the_agent_warns_at_startup(self) -> None:
        self.assertIn(
            "logs a warning at startup when `sudo` is present",
            _prose("docs/CONFIGURATION.md"),
        )
