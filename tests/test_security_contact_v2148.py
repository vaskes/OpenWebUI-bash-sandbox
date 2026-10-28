"""A security report needs an address, and the address is checked.

**What was missing.**  ``CONTRIBUTING.md`` sent a security report to
"the maintainer directly" and named no address: the one place a
reporter is pointed at told them to go nowhere in particular.  The
repository had a private vulnerability reporting channel and the
documents did not say so — a reader had to know that GitHub has such a
thing, and guess that it had been turned on.

**What this file does.**  It reads the two documents a reporter is sent
to and checks that each of them names a **concrete** channel — an email
address, or this repository's own private reporting link — that the
link is not a placeholder, and that the two documents name the same
channel (one address, not two that drift apart).

A note on what this file cannot check: that the repository's private
vulnerability reporting is actually *enabled* is a property of the
repository's settings, not of the tree.  It was measured from the API
(``GET /repos/{owner}/{repo}/private-vulnerability-reporting`` → enabled),
and the link the documents carry only works while it stays that way.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

CONTRIBUTING = ROOT / "CONTRIBUTING.md"
SECURITY = ROOT / "docs" / "SECURITY.md"

#: GitHub's private vulnerability reporting lives at
#: ``github.com/<owner>/<repo>/security/advisories/new``.
PRIVATE_REPORTING_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/security/advisories/new"
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PLACEHOLDER_RE = re.compile(
    r"OWNER|REPO|<owner>|<repo>|your[-_ ]?email|example\.com", re.IGNORECASE
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The text of the section that starts with *heading*."""
    start = text.find(heading)
    if start == -1:
        return ""
    rest = text[start + len(heading) :]
    nxt = rest.find("\n## ")
    return rest if nxt == -1 else rest[:nxt]


class TestTheSecurityChannelIsNamed(unittest.TestCase):
    def test_contributing_names_a_concrete_channel(self) -> None:
        """A report needs an address, not an instruction to guess one."""
        section = _section(_text(CONTRIBUTING), "## Security issues")
        self.assertTrue(section, "CONTRIBUTING.md has no '## Security issues' section")
        self.assertTrue(
            PRIVATE_REPORTING_RE.search(section) or EMAIL_RE.search(section),
            "the security section names no channel: neither a private "
            "reporting link nor an email address",
        )
        self.assertNotIn(
            "contact the maintainer directly",
            section,
            "the section still sends the reporter to nowhere in particular",
        )

    def test_the_security_document_names_a_channel(self) -> None:
        section = _section(_text(SECURITY), "## Reporting a vulnerability")
        self.assertTrue(
            section,
            "docs/SECURITY.md has no '## Reporting a vulnerability' section — "
            "a reporter who reads the threat model first must find the channel there",
        )
        self.assertTrue(
            PRIVATE_REPORTING_RE.search(section) or EMAIL_RE.search(section),
            "the reporting section names no channel",
        )

    def test_both_documents_name_the_same_channel(self) -> None:
        """One address, not two that drift apart."""
        contributing = PRIVATE_REPORTING_RE.findall(_text(CONTRIBUTING))
        security = PRIVATE_REPORTING_RE.findall(_text(SECURITY))
        if not contributing and not security:
            self.skipTest("neither document names a private reporting link")
        if bool(contributing) != bool(security):
            self.fail(
                "one document names a private reporting link and the other "
                "does not — the reporter is sent to two different places"
            )
        self.assertEqual(
            {tuple(pair) for pair in contributing},
            {tuple(pair) for pair in security},
            "CONTRIBUTING.md and docs/SECURITY.md point at different repositories",
        )

    def test_the_channel_is_not_a_placeholder(self) -> None:
        """A link with OWNER in it is a template, not an address."""
        for path in (CONTRIBUTING, SECURITY):
            for match in PRIVATE_REPORTING_RE.finditer(_text(path)):
                owner, repo = match.group("owner"), match.group("repo")
                self.assertFalse(
                    PLACEHOLDER_RE.search(f"{owner}/{repo}"),
                    f"{path.name} carries a placeholder channel: {match.group(0)}",
                )


if __name__ == "__main__":
    unittest.main()
