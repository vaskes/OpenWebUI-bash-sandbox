"""The release claims of v2.18.0, pinned by tests.

A release note is a claim about the code. A claim that is not pinned by
a test is a claim that rots: it stays true until somebody edits the
code and forgets the sentence. These tests are the pin.

What v2.18.0 claims:

* the two services answer with this version;
* the changelog has the section, and the changelog is newest-first;
* the sandbox image has a package manager -- pip and venv -- so the
  model does not spend a command's timeout installing one;
* the packages the model keeps asking for are preinstalled, and the
  wheels they came from ship with the image, so installing one of them
  is local and instant;
* the model's shell sees the preinstalled tree, and the service does
  not -- the tree carries its own dependencies, and the service's pins
  must not be shadowed by them;
* the model is told all of this: the tool description names every
  package the image ships;
* the tripwires for the python environment exist, and so does the
  mutation script that breaks their properties on purpose.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VERSION = "2.18.0"


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


def _newest_section_version() -> str:
    """The version of the newest ``## [X.Y.Z]`` section of the changelog.

    The version a release claims is the version the changelog's **newest**
    section describes -- not a literal written into this file. A literal
    here is a number that rots: it goes stale on the next release, and
    then it is either edited (so the test says nothing) or left (so the
    suite is red for a reason that has nothing to do with the code).
    """
    match = re.search(r"^## \[([0-9]+\.[0-9]+\.[0-9]+)\]", _source("CHANGELOG.md"), re.M)
    assert match is not None, "the changelog has no version section"
    return match.group(1)


class TestTheVersionIsTheRelease(unittest.TestCase):
    def test_the_services_answer_with_the_same_version(self) -> None:
        import agent_core  # noqa: PLC0415
        import proxy_core  # noqa: PLC0415

        self.assertEqual(agent_core.__version__, proxy_core.__version__)

    def test_the_services_answer_with_the_released_version(self) -> None:
        import agent_core  # noqa: PLC0415

        self.assertEqual(agent_core.__version__, _newest_section_version())

    def test_the_changelog_has_the_section(self) -> None:
        self.assertIn(f"## [{VERSION}]", _source("CHANGELOG.md"))

    def test_the_changelog_is_newest_first(self) -> None:
        headings = re.findall(r"^## \[(\d+)\.(\d+)\.(\d+)\]", _source("CHANGELOG.md"), re.MULTILINE)
        self.assertTrue(headings, "the changelog has no version sections")
        versions = [(int(a), int(b), int(c)) for a, b, c in headings]
        self.assertEqual(versions, sorted(versions, reverse=True))


class TestThePythonEnvironmentIsInTheRelease(unittest.TestCase):
    """The claim is about the code, not about the release note.

    The structural checks live in
    ``tests/test_python_env_v2180.py`` and are not repeated here: a fact
    that lives in two places rots in the one nobody looks at. What this
    file pins is the *release*: the version, the documentation, and the
    existence of the tripwires and the mutation script.
    """

    def test_the_documents_say_the_sandbox_can_install_packages(self) -> None:
        for name in ("README.md", "docs/CONFIGURATION.md", "docs/SECURITY.md"):
            with self.subTest(file=name):
                self.assertIn(
                    "pip",
                    _prose(name),
                    f"{name} does not mention pip -- the sandbox can install "
                    "packages, and the document has to say so",
                )

    def test_the_tripwires_exist(self) -> None:
        for name in ("tests/test_python_env_v2180.py",):
            with self.subTest(file=name):
                self.assertTrue((ROOT / name).exists(), f"{name} is missing")

    def test_the_mutation_script_exists(self) -> None:
        script = ROOT / "tools" / "mutation-check-v2180.sh"
        self.assertTrue(script.exists(), "the wave's mutation script is missing")
        self.assertIn("mutation-lib.sh", script.read_text(encoding="utf-8"))

    def test_the_changelog_says_what_the_release_does(self) -> None:
        section = " ".join(_section(_source("CHANGELOG.md"), VERSION).split())
        self.assertIn("pip", section)
        self.assertIn("/opt/pylibs", section)
        self.assertIn("/opt/wheelhouse", section)


class TestTheHandoffNamesTheRelease(unittest.TestCase):
    """The contract the author of the bot reads must name the release."""

    def test_the_version_is_named(self) -> None:
        self.assertIn(f"v{VERSION}", _prose("docs/HANDOFF-binary-transport.md"))


if __name__ == "__main__":
    unittest.main()
