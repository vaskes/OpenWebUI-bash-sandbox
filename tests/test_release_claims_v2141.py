"""The release claims of v2.14.1, pinned by tests.

A release note is a claim about the code. A claim that is not pinned by
a test is a claim that rots: it stays true until somebody edits the
code and forgets the sentence. These tests are the pin.

What v2.13.2 claims:

* the two services answer with this version;
* the changelog has the section, and the changelog is newest-first;
* the code carries the fixes — the file door opens the vetted path
  relative to the workspace, the size comes from the descriptor, the
  timeout is refused legibly, and the test harnesses clean up;
* the caller is told about it: the handoff says how the mime type is
  guessed and what "stateless" means, and the security reference
  describes how the file is opened.
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
        """The two services agree — the number is the current release's.

        The exact number belongs to the release that is being made, and
        it moves with every release. What has to hold is that the two
        services do not drift apart: a client that reads the version
        from one of them and compares it with the other must find them
        equal.
        """
        import agent_core  # noqa: PLC0415
        import proxy_core  # noqa: PLC0415

        self.assertEqual(agent_core.__version__, proxy_core.__version__)

    def test_the_changelog_has_the_section(self) -> None:
        self.assertIn("## [2.14.1]", _source("CHANGELOG.md"))

    def test_the_changelog_is_newest_first(self) -> None:
        headings = re.findall(r"^## \[(\d+)\.(\d+)\.(\d+)\]", _source("CHANGELOG.md"), re.MULTILINE)
        self.assertTrue(headings, "the changelog has no version sections")
        versions = [(int(a), int(b), int(c)) for a, b, c in headings]
        self.assertEqual(versions, sorted(versions, reverse=True))


class TestTheFixesAreInTheCode(unittest.TestCase):
    def test_the_file_is_opened_relative_to_the_workspace(self) -> None:
        source = _source("agent_api.py")
        self.assertIn("_open_inside_workspace", source)
        self.assertIn("dir_fd=dir_fd", source)

    def test_every_component_is_opened_without_following(self) -> None:
        source = _source("agent_api.py")
        block = source.split("def _open_inside_workspace", 1)[1].split("\ndef ", 1)[0]
        opens = [line for line in block.splitlines() if "os.open(" in line]
        self.assertTrue(opens, "the helper opens nothing")
        for line in opens:
            self.assertIn("os.O_NOFOLLOW", line)

    def test_the_size_comes_from_the_descriptor(self) -> None:
        self.assertIn("os.fstat(handle.fileno()).st_size", _source("mcp_server.py"))

    def test_the_timeout_is_refused_with_the_range(self) -> None:
        source = _source("mcp_server.py")
        self.assertIn("the timeout must be between 1 and {MAX_TIMEOUT} seconds", source)

    def test_the_test_harnesses_clean_up(self) -> None:
        for name in ("tests/test_agent_api_endpoints.py", "tests/test_mcp_server.py"):
            with self.subTest(file=name):
                self.assertIn("atexit.register", _source(name))


class TestTheCallerIsTold(unittest.TestCase):
    def test_the_handoff_says_the_mime_is_guessed(self) -> None:
        handoff = _prose("docs/HANDOFF-mcp.md")
        self.assertIn("The mime type is guessed from the name", handoff)

    def test_the_handoff_says_what_stateless_means(self) -> None:
        # The transport is stateless; the sandbox is not. A reader who
        # believes the opposite restarts a shell and loses its state.
        handoff = _prose("docs/HANDOFF-mcp.md")
        self.assertIn("the **transport** is stateless", handoff)
        self.assertIn("The **sandbox** is not", handoff)

    def test_the_security_reference_describes_the_open(self) -> None:
        security = _prose("docs/SECURITY.md")
        self.assertIn("How the file is opened", security)
        self.assertIn("relative to a directory descriptor", security)

    def test_the_architecture_lists_the_refusal(self) -> None:
        architecture = _prose("docs/ARCHITECTURE.md")
        self.assertIn("the path was replaced while it was being opened", architecture)

    def test_the_changelog_names_the_four_fixes(self) -> None:
        changelog = _prose("CHANGELOG.md")
        for claim in (
            "relative to a descriptor of the workspace",
            "The size in the MCP answer describes the bytes that were read.",
            "A timeout outside the range is refused in words the model can use.",
            "The test workspaces do not outlive the test process.",
        ):
            with self.subTest(claim=claim):
                self.assertIn(claim, changelog)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
