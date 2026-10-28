"""The release claims of v2.14.6, pinned by tests.

A release note is a claim about the code. A claim that is not pinned by
a test is a claim that rots: it stays true until somebody edits the
code and forgets the sentence. These tests are the pin.

What v2.14.6 claims:

* the two services answer with this version;
* the changelog has the section, and the changelog is newest-first;
* the CI job that runs the suite without tmux *replaces* the PATH, so
  the suite really runs without tmux;
* the protocol's marker does not reach the caller's stdout;
* the file door checks the limit before it opens the file, and closes
  the descriptor on every path that does not hand it to the response;
* the review checklist is a file, and it names the check behind every
  finding;
* the correction to the v2.14.5 notes is in the changelog, because a
  false claim that is only fixed in the notes of the next release is a
  false claim that stays in the release that made it.
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
        self.assertEqual(agent_core.__version__, "2.14.6")

    def test_the_changelog_has_the_section(self) -> None:
        self.assertIn("## [2.14.6]", _source("CHANGELOG.md"))

    def test_the_changelog_is_newest_first(self) -> None:
        headings = re.findall(r"^## \[(\d+)\.(\d+)\.(\d+)\]", _source("CHANGELOG.md"), re.MULTILINE)
        self.assertTrue(headings, "the changelog has no version sections")
        versions = [(int(a), int(b), int(c)) for a, b, c in headings]
        self.assertEqual(versions, sorted(versions, reverse=True))

    def test_the_correction_to_the_previous_notes_is_in_the_changelog(self) -> None:
        """A false claim is corrected where the claim is, not only in the notes."""
        text = _prose("CHANGELOG.md")
        self.assertIn("Correction to the v2.14.5 notes", text)
        self.assertIn("98", text)
        self.assertIn("101", text)


class TestTheJobWithoutTmuxIsTheClaim(unittest.TestCase):
    def test_the_path_is_replaced_not_prepended(self) -> None:
        workflow = _source(".github/workflows/ci.yml")
        block = workflow.split("suite-without-tmux:", 1)[-1]
        # The *command* of the step, not the block: the block also holds
        # the comment that explains what the old form did, and the
        # comment is not what runs.
        commands = [line.strip() for line in block.split("\n") if line.strip().startswith("run:")]
        self.assertTrue(commands, "the job has no run step")
        self.assertTrue(
            any(command.startswith("run: PATH=/tmp/no-tmux pytest") for command in commands),
            f"the suite is not run with the farm: {commands}",
        )
        self.assertFalse(
            any("PATH=/tmp/no-tmux:$PATH" in command for command in commands),
            "the PATH is prepended — the original one stays, and it has tmux",
        )

    def test_the_farm_is_built_from_the_whole_path(self) -> None:
        workflow = _source(".github/workflows/ci.yml")
        block = workflow.split("suite-without-tmux:", 1)[-1]
        self.assertIn("tr ':' ' '", block, "the farm is built from one directory, not the PATH")

    def test_the_check_runs_the_job_instead_of_reading_it(self) -> None:
        """The tripwire must be able to go red; reading strings cannot."""
        source = _source("tests/test_ci_without_tmux_v2146.py")
        self.assertIn("test_tmux_is_not_findable_in_the_path_the_job_runs_with", source)
        self.assertIn("shutil.which", source)


class TestTheMarkerIsTheClaim(unittest.TestCase):
    def test_the_redrawn_lines_are_cut(self) -> None:
        source = _source("agent_api.py")
        # The marker is cut; the redraw of the command line stays, because
        # that is the caller's own text and the stdout contract has it.
        self.assertIn("if line.startswith(CONTINUATION_PROMPT):", source)
        self.assertIn("line = rest", source)

    def test_the_marker_is_still_installed(self) -> None:
        source = _source("agent_core.py")
        self.assertIn('CONTINUATION_PROMPT = "__AGENT_CONT__"', source)
        self.assertTrue(
            any("PS2=" in line and "CONTINUATION_PROMPT" in line for line in source.split("\n")),
            "the protocol installs no continuation prompt of its own",
        )


class TestTheFileDoorIsTheClaim(unittest.TestCase):
    def test_the_limit_is_checked_before_the_file_is_opened(self) -> None:
        source = _source("agent_api.py")
        block = source.split("async def download_file(", 1)[1].split("\n@app", 1)[0]
        self.assertLess(
            block.index("_path_size(target)"),
            block.index("_open_for_download(target)"),
            "the file is opened before the limit is checked",
        )

    def test_the_descriptor_is_closed_on_the_path_that_does_not_use_it(self) -> None:
        source = _source("agent_api.py")
        block = source.split("async def download_file(", 1)[1].split("\n@app", 1)[0]
        self.assertIn("handle.close()", block.split("_parse_byte_range", 1)[-1])


class TestTheChecklistIsTheClaim(unittest.TestCase):
    def test_the_checklist_is_a_file(self) -> None:
        self.assertTrue((ROOT / "docs" / "REVIEW-CHECKLIST.md").is_file())

    def test_the_checklist_names_the_waves(self) -> None:
        text = _source("docs/REVIEW-CHECKLIST.md")
        for wave in ("v2.14.3", "v2.14.4", "v2.14.5"):
            self.assertIn(wave, text, f"the checklist does not mention {wave}")

    def test_every_finding_of_the_three_waves_is_in_the_checklist(self) -> None:
        text = _source("docs/REVIEW-CHECKLIST.md")
        for finding in ("R1", "R2", "R3", "R4", "R5", "R6", "S1", "S2", "S3", "S4", "S5"):
            self.assertRegex(text, rf"\|\s*{finding}\s*\|", f"{finding} is not in the checklist")
        for finding in ("T1", "T2", "T3"):
            self.assertRegex(text, rf"\|\s*{finding}\s*\|", f"{finding} is not in the checklist")

    def test_the_checklist_says_what_a_closed_finding_is(self) -> None:
        text = _prose("docs/REVIEW-CHECKLIST.md")
        self.assertIn("can go red", text)


if __name__ == "__main__":
    unittest.main()
