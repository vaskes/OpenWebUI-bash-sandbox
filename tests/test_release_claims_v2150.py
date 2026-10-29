"""The release claims of v2.15.0, pinned by tests.

A release note is a claim about the code. A claim that is not pinned by
a test is a claim that rots: it stays true until somebody edits the
code and forgets the sentence. These tests are the pin.

What v2.15.0 claims:

* the two services answer with this version;
* the changelog has the section, and the changelog is newest-first;
* the door takes files in: the route is in the public API, the body is
  the raw bytes, the limit is enforced while the bytes arrive, the
  write is atomic, and nothing is ever overwritten;
* the handoff contract -- the document the author of the bot reads --
  names the new route and the new version;
* the tripwire for the door exists, and the mutation script that
  breaks its properties on purpose is there too.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VERSION = "2.15.0"


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


class TestTheDoorTakesFilesIn(unittest.TestCase):
    """The claim is about the code, not about the release note."""

    def test_the_route_is_in_the_public_api(self) -> None:
        source = _source("agent_api.py")
        self.assertIn('@app.post("/files"', source)
        self.assertIn("POST /files?path=P", source)

    def test_the_body_is_the_raw_bytes(self) -> None:
        source = _source("agent_api.py")
        self.assertIn("async for chunk in request.stream():", source)

    def test_the_limit_is_checked_while_the_bytes_arrive(self) -> None:
        source = _source("agent_api.py")
        self.assertIn("if BINARY_LIMIT_BYTES and written > BINARY_LIMIT_BYTES:", source)

    def test_the_write_is_atomic(self) -> None:
        source = _source("agent_api.py")
        self.assertIn("os.replace(tmp_path, target)", source)

    def test_nothing_is_overwritten(self) -> None:
        source = _source("agent_api.py")
        self.assertIn('status_code=409, detail=f"file already exists: {name}"', source)

    def test_the_door_test_exists(self) -> None:
        self.assertTrue((ROOT / "tests" / "test_file_door_upload_v2150.py").exists())

    def test_the_mutation_script_exists(self) -> None:
        script = ROOT / "tools" / "mutation-check-v2150.sh"
        self.assertTrue(script.exists(), "the wave's mutation script is missing")
        self.assertIn("mutation-lib.sh", script.read_text(encoding="utf-8"))


class TestTheHandoffNamesTheNewDoor(unittest.TestCase):
    """The contract the author of the bot reads must name the route."""

    def setUp(self) -> None:
        self.handoff = _prose("docs/HANDOFF-binary-transport.md")

    def test_the_route_is_named(self) -> None:
        self.assertIn("`POST`", self.handoff)
        self.assertIn("`/files?path=P`", self.handoff)

    def test_the_version_is_named(self) -> None:
        self.assertIn("v2.15.0", self.handoff)

    def test_the_changelog_says_what_the_door_does(self) -> None:
        section = " ".join(_section(_source("CHANGELOG.md"), VERSION).split())
        self.assertIn("POST /files", section)
        self.assertIn("409", section)
        self.assertIn("BINARY_LIMIT_BYTES", section)


if __name__ == "__main__":
    unittest.main()
