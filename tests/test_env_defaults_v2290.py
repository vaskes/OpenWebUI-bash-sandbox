"""Tripwires for the template's defaults: the knobs are on, and they are placeholders.

Context. Measured on 2026-11-04, on both live stands: the token
separation was ON there (``AGENT_INBOUND_TOKEN`` set, the operator's
public token nowhere in the agent's container). But the **template**
shipped with both knobs commented out:

* ``AGENT_INBOUND_TOKEN=`` — so a deployment that copied
  ``.env.example`` and filled in the rest ran with **one** token for the
  whole stack: the model's shell inherited the agent's environment, and
  the operator's public door token was in it;
* ``AGENT_WORKSPACE_SIZE=1g`` — so the ceiling the workspace actually
  has was **not declared**, and the agent's check that refuses a
  workspace without a ceiling never ran.

A template is what most deployments start from. The knobs the operator
measured on the stands have to be on in the template too, or the
measurement only holds for the stands.

Two rules, and the second one is why the first one is not enough:

* the knob must be **active** — a commented-out knob is a default-off
  knob;
* the value must be a **placeholder** — a published token is a token
  every deployment that copies the template shares, which is worse than
  the knob being off. ``make check-env`` refuses a ``.env`` that still
  holds a placeholder, and the services refuse a placeholder token
  themselves, so an unfilled template does not start at all.

The third property here is smaller: a number in a comment goes stale,
and a stale number in a comment about the sweep is how a reader ends up
believing the wrong thing about what the CI runs.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
CONFIGURATION = ROOT / "docs" / "CONFIGURATION.md"
CI = ROOT / ".github" / "workflows" / "ci.yml"
TOOLS = ROOT / "tools"


def _flat(text: str) -> str:
    """Collapse the wrapping of a prose document."""
    return " ".join(text.split())


class TestTheTemplateTurnsTheKnobsOn(unittest.TestCase):
    def setUp(self) -> None:
        self.text = ENV_EXAMPLE.read_text(encoding="utf-8")

    def test_the_inbound_token_is_not_commented_out(self) -> None:
        """A commented-out knob is a default-off knob."""
        self.assertIn(
            "\nAGENT_INBOUND_TOKEN=",
            "\n" + self.text,
            "the inbound token has to be ACTIVE in the template: a "
            "commented-out knob leaves the operator's public token in the "
            "agent's container",
        )

    def test_the_inbound_token_is_a_placeholder_and_not_a_value(self) -> None:
        """A published token is a token every deployment shares."""
        match = re.search(r"(?m)^AGENT_INBOUND_TOKEN=(\S*)", self.text)
        self.assertIsNotNone(match, "the template has no inbound token")
        value = match.group(1)
        self.assertTrue(
            value.startswith("CHANGE_ME"),
            "the template must carry a placeholder, not a value: a "
            f"published token is a shared secret (found {value!r})",
        )

    def test_the_ceiling_is_not_commented_out(self) -> None:
        """A ceiling nobody declared is a ceiling nobody checks."""
        self.assertIn(
            "\nAGENT_WORKSPACE_SIZE=",
            "\n" + self.text,
            "the workspace's ceiling has to be ACTIVE in the template: "
            "otherwise the agent's check never runs",
        )

    def test_the_template_says_how_to_fill_the_placeholder(self) -> None:
        """The recipe is the part an operator actually needs."""
        flat = _flat(self.text)
        self.assertIn(
            "openssl rand -hex 32",
            flat,
            ".env.example must say how to generate the inbound token",
        )


class TestTheDocumentNamesWhatTheAbsenceCosts(unittest.TestCase):
    def setUp(self) -> None:
        self.flat = _flat(CONFIGURATION.read_text(encoding="utf-8"))

    def test_the_cost_of_no_separation_is_written_down(self) -> None:
        self.assertIn(
            "the operator's public token is not in that container at all",
            self.flat,
            "docs/CONFIGURATION.md must say what the separation buys",
        )

    def test_the_forged_code_case_is_written_down(self) -> None:
        self.assertIn(
            "forged agent code would run with",
            self.flat,
            "docs/CONFIGURATION.md must say what a forged agent code would "
            "run with — that is the case the separation decides",
        )

    def test_the_env_file_is_not_a_shell_script_is_written_down(self) -> None:
        self.assertIn(
            "is not a shell script",
            self.flat,
            "docs/CONFIGURATION.md must say that `$(...)` is not expanded "
            "in .env — the recipe is the thing people get wrong",
        )


class TestTheSweepCountInTheCommentIsTrue(unittest.TestCase):
    """A number in a comment goes stale; this one is pinned."""

    def test_the_comment_names_the_number_of_scripts_on_disk(self) -> None:
        scripts = sorted(TOOLS.glob("mutation-check-*.sh"))
        self.assertTrue(scripts, "no mutation scripts in tools/")
        text = CI.read_text(encoding="utf-8")
        match = re.search(r"# The sweep is (\d+) scripts", text)
        self.assertIsNotNone(match, "the CI comment no longer names a count")
        self.assertEqual(
            int(match.group(1)),
            len(scripts),
            "the CI comment says the sweep is "
            f"{match.group(1)} scripts; there are {len(scripts)} on disk",
        )


if __name__ == "__main__":
    unittest.main()
