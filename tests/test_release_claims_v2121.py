"""The release claims of v2.12.1, pinned by tests.

The fixes are pinned in ``test_review_v2120.py`` and
``test_hardening_v2121.py``. This file pins the *other* half: the
claims the release makes about itself. A changelog that says one thing
and a README that says another are not documentation — they are
advertisements.

Every claim below is checked against the thing it describes, so a
document that drifts from the code fails the suite rather than the
reader.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class TestTheDocumentationSaysWhatTheCodeDoes(unittest.TestCase):
    """A document that promises more than the code delivers is a bug.

    These are claims a caller reads before writing code against this
    sandbox, so each one is checked against the thing it describes.
    """

    def test_the_server_says_the_session_is_shared(self) -> None:
        # The model reads the instructions, not the README. A shared
        # session that the instructions do not mention is a session
        # two conversations silently share.
        source = _source("mcp_server.py")
        self.assertIn("a session is shared", source)
        self.assertIn("session_id", source)

    def test_the_handoff_says_a_timeout_does_not_stop_the_command(self) -> None:
        handoff = _source("docs/HANDOFF-mcp.md")
        self.assertIn("A timeout does not stop the command", handoff)

    def test_the_handoff_says_the_read_holds_the_file_in_memory(self) -> None:
        handoff = _source("docs/HANDOFF-mcp.md")
        self.assertIn("holds the file in memory", handoff)

    def test_the_handoff_carries_the_read_ceiling(self) -> None:
        handoff = _source("docs/HANDOFF-mcp.md")
        self.assertIn("64 MiB", handoff)

    def test_the_readme_carries_the_same_three_claims(self) -> None:
        readme = _source("README.md")
        self.assertIn("persistent and shared", readme)
        self.assertIn("does not stop the command", readme)
        self.assertIn("64 MiB", readme)

    def test_the_changelog_has_the_section(self) -> None:
        changelog = _source("CHANGELOG.md")
        self.assertIn("## [2.12.1]", changelog)

    def test_the_versions_agree(self) -> None:
        import agent_core  # noqa: PLC0415
        import proxy_core  # noqa: PLC0415

        self.assertEqual(agent_core.__version__, "2.12.1")
        self.assertEqual(proxy_core.__version__, "2.12.1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
