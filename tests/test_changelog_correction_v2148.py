"""A correction has to live where the claim lives.

**What was wrong.** The v2.14.5 notes claimed *"The mutation count on
v2.14.4 was 101, not 98."* — and that claim was false. The count on
v2.14.4 is 98. The correction was written down, but it was written
down a hundred lines further up, in the section of the *next* release:
a reader of the v2.14.5 section read the false claim and nothing else.

That is the whole defect. A correction that lives in another section is
not a correction of *this* sentence; it is a correction somewhere in
the file. The claim's own section has to point at it.

**What this test does.** It reads the section that says the false thing
and requires it to say, in the same section, that the number was
corrected and where the correction lives. The text of the old claim is
not rewritten — history is not edited — but it may not stand alone.
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


def _section(text: str, version: str) -> str:
    """The text of the changelog section of *version*."""
    match = re.search(rf"^## \[{re.escape(version)}\].*?$(.*?)(?=^## \[|\Z)", text, re.M | re.S)
    assert match is not None, f"the changelog has no section for {version}"
    return match.group(1)


class TestTheCorrectionIsWhereTheClaimIs(unittest.TestCase):
    def test_the_claim_about_the_v2_14_4_count_is_still_there(self) -> None:
        """The old text is history; it is not rewritten."""
        section = _section(_source("CHANGELOG.md"), "2.14.5")
        self.assertIn("101", section, "the v2.14.5 section no longer mentions the count")

    def test_the_v2_14_5_section_points_at_the_correction(self) -> None:
        """A correction in another section is not a correction of this one."""
        section = _section(_source("CHANGELOG.md"), "2.14.5")
        self.assertIn(
            "[2.14.6]",
            section,
            "the v2.14.5 section states the false count without pointing at the correction",
        )
        self.assertIn(
            "orrected",
            section,
            "the v2.14.5 section does not say that the count was corrected",
        )

    def test_the_correction_still_says_what_the_truth_is(self) -> None:
        """The correction itself must carry the number that is right."""
        section = _section(_source("CHANGELOG.md"), "2.14.6")
        self.assertIn("98", section, "the correction no longer carries the right count")
        self.assertIn(
            "101",
            section,
            "the correction no longer names the number it corrects",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
