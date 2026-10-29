"""The release claims of v2.16.0, pinned by tests.

A release note is a claim about the code. A claim that is not pinned by
a test is a claim that rots: it stays true until somebody edits the
code and forgets the sentence. These tests are the pin.

What v2.16.0 claims:

* the two services answer with this version;
* the changelog has the section, and the changelog is newest-first;
* the sandbox's storage is capped: the workspace is a tmpfs inside the
  container with a size, no host path is mounted writable, and the root
  filesystem is read-only by default;
* a full workspace is a 507, and the handoff contract -- the document
  the author of the bot reads -- promises that refusal;
* the tripwires for the storage budget exist, and the mutation script
  that breaks their properties on purpose is there too.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VERSION = "2.16.0"


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

        self.assertEqual(agent_core.__version__, proxy_core.__version__)
        self.assertEqual(agent_core.__version__, VERSION)

    def test_the_changelog_has_the_section(self) -> None:
        self.assertIn(f"## [{VERSION}]", _source("CHANGELOG.md"))

    def test_the_changelog_is_newest_first(self) -> None:
        headings = re.findall(r"^## \[(\d+)\.(\d+)\.(\d+)\]", _source("CHANGELOG.md"), re.MULTILINE)
        self.assertTrue(headings, "the changelog has no version sections")
        versions = [(int(a), int(b), int(c)) for a, b, c in headings]
        self.assertEqual(versions, sorted(versions, reverse=True))


class TestTheStorageIsCapped(unittest.TestCase):
    """The claim is about the code, not about the release note.

    The structural checks live in ``tests/test_workspace_budget_v2160.py``
    and are not repeated here: a fact that lives in two places rots in
    the one nobody looks at. What this file pins is the *release*: the
    version, the documentation of the knob, the 507 in the code, and the
    existence of the tripwires and the mutation script that hold the
    properties.
    """

    def test_the_knob_is_documented(self) -> None:
        self.assertIn("AGENT_WORKSPACE_SIZE", _source(".env.example"))
        self.assertIn("AGENT_WORKSPACE_SIZE", _source("docs/CONFIGURATION.md"))
        self.assertIn("AGENT_WORKSPACE_SIZE", _source("README.md"))

    def test_the_door_answers_507(self) -> None:
        source = _source("agent_api.py")
        self.assertIn("status_code=507", source)
        self.assertIn("errno.ENOSPC", source)

    def test_the_tripwires_exist(self) -> None:
        for name in (
            "tests/test_workspace_budget_v2160.py",
            "tests/test_upload_full_workspace_v2160.py",
        ):
            with self.subTest(file=name):
                self.assertTrue((ROOT / name).exists(), f"{name} is missing")

    def test_the_mutation_script_exists(self) -> None:
        script = ROOT / "tools" / "mutation-check-v2160.sh"
        self.assertTrue(script.exists(), "the wave's mutation script is missing")
        self.assertIn("mutation-lib.sh", script.read_text(encoding="utf-8"))


class TestTheHandoffPromisesTheFullWorkspace(unittest.TestCase):
    """The contract the author of the bot reads must name the refusal."""

    def setUp(self) -> None:
        self.handoff = _prose("docs/HANDOFF-binary-transport.md")

    def test_the_version_is_named(self) -> None:
        self.assertIn(f"v{VERSION}", self.handoff)

    def test_the_full_workspace_is_named(self) -> None:
        self.assertIn("507", self.handoff)
        self.assertIn("the sandbox workspace is full", self.handoff)

    def test_the_changelog_says_what_the_release_does(self) -> None:
        section = " ".join(_section(_source("CHANGELOG.md"), VERSION).split())
        self.assertIn("AGENT_WORKSPACE_SIZE", section)
        self.assertIn("507", section)


if __name__ == "__main__":
    unittest.main()
