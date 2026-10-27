"""The release claims of v2.14.0, pinned by tests.

The claim of this release: **a command may run for an hour**, and every
number that says so says the same one.

The numbers involved live in three places — the proxy's accepted range,
the agent's hard cap, and the relay's patience — and each of them used
to be a separate copy of the same fact. The operator measured the limit
from the inside (a command was cut off at a minute, and the output it
had already produced went with it) and ruled that the ceiling should be
an hour. A ceiling that is written down three times is a ceiling that
drifts, so:

* the proxy's range and the agent's cap are equal, so the proxy can
  never ask for a timeout the agent would refuse;
* the relay waits longer than the agent can work, so a command that
  runs to the cap still gets its answer home;
* the documentation carries the same numbers, and the knob's default is
  still 60 — the range is what a deployment raises, not the default.
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


def _int_constant(path: Path, name: str) -> int:
    """Read an integer constant out of a module, by name.

    The point is that the number under test comes from the code, not
    from a constant in this file: a test that quotes the number it is
    checking cannot notice the number changing.
    """
    match = re.search(rf"^{name}\s*(?::\s*\w+\s*)?=\s*(\d+)", _source(path.name), re.MULTILINE)
    assert match is not None, f"{path.name} has no {name}"
    return int(match.group(1))


class TestTheVersionIsTheRelease(unittest.TestCase):
    def test_the_services_answer_with_this_version(self) -> None:
        # The concrete number belongs to the release that is being
        # prepared -- it is pinned in that release's own file. Here the
        # claim is the weaker one that never rots: the two services are
        # the same release of the same project.
        import agent_core  # noqa: PLC0415
        import proxy_core  # noqa: PLC0415

        self.assertEqual(agent_core.__version__, proxy_core.__version__)

    def test_the_changelog_has_the_section(self) -> None:
        self.assertIn("## [2.14.0]", _source("CHANGELOG.md"))

    def test_the_changelog_is_newest_first(self) -> None:
        headings = re.findall(r"^## \[(\d+)\.(\d+)\.(\d+)\]", _source("CHANGELOG.md"), re.MULTILINE)
        self.assertTrue(headings, "the changelog has no version sections")
        versions = [(int(a), int(b), int(c)) for a, b, c in headings]
        self.assertEqual(versions, sorted(versions, reverse=True))


class TestTheCeilingIsOneNumber(unittest.TestCase):
    def test_the_proxy_can_ask_for_what_the_agent_accepts(self) -> None:
        # The proxy's range and the agent's cap are two copies of one
        # ceiling. If the agent's cap were lower, the proxy could ask
        # for a timeout the agent refuses -- and the caller would get a
        # validation error instead of a command.
        ceiling = _int_constant(ROOT / "proxy_core.py", "EXEC_TIMEOUT_MAX_SECONDS")
        cap = _int_constant(ROOT / "agent_api.py", "MAX_TIMEOUT")
        self.assertEqual(ceiling, cap)

    def test_the_ceiling_is_an_hour(self) -> None:
        self.assertEqual(_int_constant(ROOT / "agent_api.py", "MAX_TIMEOUT"), 3600)

    def test_the_relay_waits_longer_than_the_agent_can_work(self) -> None:
        relay = re.search(
            r"^MCP_RELAY_TIMEOUT_SECONDS:\s*float\s*=\s*([\d.]+)",
            _source("openapi_proxy.py"),
            re.MULTILINE,
        )
        self.assertIsNotNone(relay, "the relay timeout constant is gone")
        cap = _int_constant(ROOT / "agent_api.py", "MAX_TIMEOUT")
        self.assertGreater(
            float(relay.group(1)), cap, "the MCP relay would give up before the agent stops working"
        )


class TestTheDefaultDidNotMove(unittest.TestCase):
    """The range widened; the default did not.

    A deployment raises the knob. The default is what a deployment that
    never touched it gets -- and that number is not this release's
    business.
    """

    def test_the_default_is_still_sixty(self) -> None:
        self.assertEqual(_int_constant(ROOT / "proxy_core.py", "EXEC_TIMEOUT_SECONDS"), 60)

    def test_the_environment_template_still_offers_sixty(self) -> None:
        self.assertIn("# AGENT_EXEC_TIMEOUT=60", _source(".env.example"))


class TestTheDocumentsCarryTheSameNumbers(unittest.TestCase):
    def test_the_architecture_table_has_the_cap(self) -> None:
        arch = _source("docs/ARCHITECTURE.md")
        self.assertIn("3600 s", arch)
        self.assertIn("5..3600", arch)

    def test_the_configuration_reference_has_the_range(self) -> None:
        self.assertIn("5..3600", _source("docs/CONFIGURATION.md"))

    def test_the_old_ceiling_is_gone_from_the_documents(self) -> None:
        # A stale range in a document is a limit nobody can trust: the
        # operator reads 5..600 and believes the deployment cannot do
        # more.
        for name in (
            "docs/ARCHITECTURE.md",
            "docs/CONFIGURATION.md",
            "docs/HANDOFF-mcp.md",
            ".env.example",
        ):
            with self.subTest(document=name):
                text = _source(name)
                self.assertNotIn("5..600", text)
                self.assertNotIn("1..600", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
