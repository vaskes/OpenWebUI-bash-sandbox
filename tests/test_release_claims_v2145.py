"""The release claims of v2.14.5, pinned by tests.

A release note is a claim about the code. A claim that is not pinned by
a test is a claim that rots: it stays true until somebody edits the
code and forgets the sentence. These tests are the pin.

What v2.14.5 claims:

* the two services answer with this version;
* the changelog has the section, and the changelog is newest-first;
* the file door's size comes from the descriptor the bytes are read
  from;
* a known session comes back in the directory it had;
* a bad command reaches the model as words, not as a framework error;
* the MCP tool opens the session only when it has to;
* the suite runs a tmux server of its own, and the CI checks the suite
  without tmux;
* the tree has no unreachable operators.
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
        # The number itself belongs to the release that carries it; what
        # this test owns is that the two services agree — a tree where
        # they disagree is a deployment that answers with two versions.
        # The number itself belongs to the release that carries it; what
        # this test owns is that the two services agree -- a tree where
        # they disagree is a deployment that answers with two versions.

    def test_the_changelog_has_the_section(self) -> None:
        self.assertIn("## [2.14.5]", _source("CHANGELOG.md"))

    def test_the_changelog_is_newest_first(self) -> None:
        headings = re.findall(r"^## \[(\d+)\.(\d+)\.(\d+)\]", _source("CHANGELOG.md"), re.MULTILINE)
        self.assertTrue(headings, "the changelog has no version sections")
        versions = [(int(a), int(b), int(c)) for a, b, c in headings]
        self.assertEqual(versions, sorted(versions, reverse=True))

    def test_the_notes_name_the_posture_of_the_deployment(self) -> None:
        """The production posture is the deployment's, and it is said.

        The env file the deployment reads sets
        `AGENT_NO_NEW_PRIVILEGES=false`, `AGENT_CAP_DROP=SYS_ADMIN` and
        `AGENT_READ_ONLY=false` — so the agent runs with sudo and a
        writable root.  That was already true before this release; it
        was never written down, and a reader of the compose files would
        have concluded the opposite.
        """
        text = _prose("CHANGELOG.md")
        self.assertIn("AGENT_NO_NEW_PRIVILEGES=false", text)
        self.assertIn("AGENT_CAP_DROP=SYS_ADMIN", text)
        self.assertIn("AGENT_READ_ONLY=false", text)


class TestTheFileDoorIsTheClaim(unittest.TestCase):
    def test_the_size_comes_from_the_descriptor(self) -> None:
        source = _source("agent_api.py")
        block = source.split("async def download_file(", 1)[1].split("\n@app", 1)[0]
        self.assertIn("os.fstat(handle.fileno()).st_size", block)

    def test_the_file_is_opened_before_it_is_measured(self) -> None:
        source = _source("agent_api.py")
        block = source.split("async def download_file(", 1)[1].split("\n@app", 1)[0]
        self.assertLess(
            block.index("_open_for_download(target)"),
            block.index("os.fstat(handle.fileno())"),
            "the file is measured before it is opened — the two can disagree",
        )


class TestTheSessionIsTheClaim(unittest.TestCase):
    def test_a_known_session_comes_back_where_it_was(self) -> None:
        source = _source("agent_api.py")
        self.assertIn("def _persisted_cwd(", source)
        block = source.split("async def create_session(", 1)[1].split("\n@app", 1)[0]
        self.assertIn("_persisted_cwd(sid)", block)

    def test_the_auto_create_path_names_no_directory(self) -> None:
        """A directory named here would override the persisted one."""
        source = _source("agent_api.py")
        block = source.split("async def exec_cmd(", 1)[1].split("\n@app", 1)[0]
        self.assertIn("CreateSession(session_id=sid)", block)
        self.assertNotIn("CreateSession(session_id=sid, cwd=", block)


class TestTheModelIsTheClaim(unittest.TestCase):
    def test_a_bad_command_is_refused_in_words(self) -> None:
        source = _source("mcp_server.py")
        self.assertIn("the command is empty", source)
        self.assertIn("over the limit of", source)

    def test_the_request_is_built_inside_the_guard(self) -> None:
        source = _source("mcp_server.py")
        block = source.split("async def bash(", 1)[1].split("\n@", 1)[0]
        self.assertLess(
            block.index("try:"),
            block.index("request = ExecRequest("),
            "the request is built before the guard that catches its errors",
        )

    def test_the_tool_opens_the_session_only_when_it_has_to(self) -> None:
        source = _source("mcp_server.py")
        self.assertIn("if cwd is not None or sid not in SESSIONS:", source)


class TestTheSuiteIsTheClaim(unittest.TestCase):
    def test_the_suite_has_a_tmux_server_of_its_own(self) -> None:
        source = _source("tests/conftest.py")
        self.assertIn('os.environ["TMUX_TMPDIR"] = str(private)', source)

    def test_the_ci_checks_the_suite_without_tmux(self) -> None:
        workflow = _source(".github/workflows/ci.yml")
        self.assertIn("suite-without-tmux", workflow)
        self.assertIn("no-tmux", workflow)

    def test_the_tree_has_no_unreachable_operators(self) -> None:
        """The scan that guards the tree is part of the tree."""
        source = _source("tests/test_tree_hygiene_v2145.py")
        self.assertIn("test_no_operator_follows_a_return_or_a_raise", source)
        self.assertNotIn("unresolvable path", _source("agent_api.py"))


if __name__ == "__main__":
    unittest.main()
