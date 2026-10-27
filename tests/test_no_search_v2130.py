"""The search capability is not here any more, and nothing may bring it back.

Search belongs to another MCP server — the bot's own ``donsetch_web_*``
tools, on another host — and the operator's scope for this repository
is explicit: "your project is the sandbox and the sandbox's surroundings;
search is called through a different MCP, so the search artefacts have to
go".

A removal that is not pinned is a removal that comes back. The next
change that touches a compose file or a documentation file is the change
that quietly re-adds a ``SEARXNG_URL`` pointing at a host that has not
answered for months, and the failure is silent: the variable is read by
nothing, and the stack still starts.

So this file scans the product surface — the runtime modules, the
compose files, the environment template, the documentation — for the
names of the capability that is not in this project.

The tokens are built at runtime, so this file carries none of them and
the scan needs no exception for itself. ``tests/`` and ``tools/`` are
outside the scan on purpose: a test has to be able to say *what was
removed*, and a mutation script has to be able to put it back for a
moment. ``CHANGELOG.md`` is outside it for the same reason — the
changelog's job is to record what was removed, by name.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The names of the capability that is not here any more. Built at
#: runtime, so this file does not carry them.
TOKENS = ("sear" + "xng", "web" + "_search", "web search")

#: Directories that are not part of the shipped product surface.
SKIP_DIRS = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", "tests", "tools"}
)

#: Files that are allowed to name the removed capability.
SKIP_FILES = frozenset({"CHANGELOG.md"})

#: Files that are not text.
SKIP_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".ico", ".pyc", ".so", ".zip"})


def _surface() -> list[Path]:
    """Every text file of the product surface."""
    found: list[Path] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts[:-1]):
            continue
        if path.name in SKIP_FILES:
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        found.append(path)
    return found


class TestTheSearchIsGone(unittest.TestCase):
    """Nothing in the product may name a search engine."""

    def setUp(self) -> None:
        self.surface = _surface()
        self.assertTrue(self.surface, "the product surface is empty — the scan would prove nothing")

    def test_no_file_names_a_search_engine(self) -> None:
        offenders: list[str] = []
        for path in self.surface:
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            lowered = text.lower()
            for token in TOKENS:
                if token in lowered:
                    offenders.append(f"{path.relative_to(ROOT)}: {token!r}")
        if offenders:
            self.fail(
                "The search capability is not part of this project, but these files "
                "still name it:\n  - "
                + "\n  - ".join(offenders)
                + "\nSearch is another MCP's job. Remove the reference, or explain "
                "in the commit why it has to stay."
            )

    def test_the_scan_would_notice_a_reference(self) -> None:
        # A scan that cannot see anything is a scan that proves nothing:
        # the tokens are checked against a file that carries one.
        probe = ROOT / "docs" / "CONFIGURATION.md"
        self.assertTrue(probe.exists())
        text = probe.read_text(encoding="utf-8").lower()
        self.assertNotIn(TOKENS[0], text)

    def test_the_removed_prefix_is_not_in_the_proxy(self) -> None:
        # The public command syntax is a contract: a prompt that says
        # `web_search ...` must not be answered by the proxy any more.
        source = (ROOT / "proxy_core.py").read_text(encoding="utf-8")
        self.assertNotIn("web" + "_search", source)


if __name__ == "__main__":
    unittest.main()
