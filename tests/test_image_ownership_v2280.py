"""Tripwires for the agent's own code: it belongs to root, not to the model.

Context. Measured on a polygon stack on 2026-11-04: ``/home/agent`` and
``agent_api.py`` were owned by **uid 1000** — the uid the *model's own
shell* runs as. A command in the sandbox could therefore rewrite the
agent's code in place: no escalation, no ``sudo``. The write landed in
the container's writable layer, that layer survives ``docker restart``,
and the agent came up on the next start running the code the model had
written, with the agent's token in its environment. Measured: an agent
whose ``agent_api.py`` the sandbox had overwritten went into a restart
loop and stopped serving.

So the boundary moved in one place: not out of the container, but *into*
the code that enforces it. The fix is in the image — the agent's modules
are owned by root, and the directory is not writable by the model's uid.

These tests are the cheap half: they parse the artifacts we ship. The
expensive half is a step in the CI's deploy job that tries the write
against the *shipped image* and requires it to be refused.

Two things this must not break, and both are pinned here: the agent
still has to be able to *read* its modules (they are only ever read), and
the model still has to own the places it legitimately writes — its pip
cache and its workspace.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX_DOCKERFILE = ROOT / "Dockerfile.sandbox"
SECURITY = ROOT / "docs" / "SECURITY.md"

#: The modules the agent ships and imports. They are read, never written,
#: so root ownership costs the service nothing.
AGENT_MODULES = (
    "agent_api.py",
    "agent_browser.py",
    "agent_core.py",
    "agent_config.py",
    "mcp_server.py",
)

SHARED_MODULES = ("body_limit.py", "json_logging.py", "metrics.py", "token_rules.py")


def _strip_comments(text: str) -> str:
    """Drop whole-line and trailing ``#`` comments.

    The Dockerfile quotes the *old* broken commands in its comments, so a
    naive substring scan would match the comment and fail forever.
    """
    out = []
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        out.append(line.split(" #", 1)[0] if " #" in line else line)
    return "\n".join(out)


def _flat(text: str) -> str:
    """Collapse the wrapping of a prose document.

    A check that pins the document's line breaks fails the first time
    somebody reflows a paragraph; the rule is what has to be pinned.
    """
    return " ".join(text.split())


class TestTheAgentsCodeIsNotTheModelsToWrite(unittest.TestCase):
    """The image must not hand the model's uid the agent's own code."""

    def setUp(self) -> None:
        self.body = _strip_comments(SANDBOX_DOCKERFILE.read_text(encoding="utf-8"))

    def test_no_module_is_copied_as_the_models_uid(self) -> None:
        """``COPY --chown=1000:1000`` hands the model the enforcing code.

        uid 1000 is the uid the model's shell runs as. A module owned by
        it can be rewritten by any command in the sandbox, and the write
        survives a container restart.
        """
        offenders = re.findall(r"^COPY\s+--chown=1000:1000\s+(\S+)", self.body, re.M)
        self.assertEqual(
            offenders,
            [],
            f"these files are copied into the image owned by the model's uid: {offenders}",
        )

    def test_the_agents_directory_is_owned_by_root(self) -> None:
        """``/home/agent`` must not be writable by the model's uid."""
        self.assertIn(
            "chown root:root /home/agent",
            self.body,
            "the agent's directory must be owned by root, not by the model's uid",
        )

    def test_the_agents_directory_is_not_writable_by_others(self) -> None:
        """The mode must not give the model's uid a way in.

        Ownership alone is not enough: a group- or world-writable
        directory is writable by uid 1000 whatever it is owned by.
        """
        match = re.search(r"chmod (\d+) /home/agent", self.body)
        self.assertIsNotNone(match, "the mode of /home/agent is not pinned")
        # The mode in a Dockerfile is octal: ``chmod 755`` is 0o755, and
        # read as a decimal number the mask below would be nonsense.
        mode = int(match.group(1), 8)
        self.assertEqual(
            mode & 0o022,
            0,
            f"/home/agent is mode {mode:o}: it must not be writable by group or by others",
        )

    def test_the_image_does_not_write_bytecode_into_the_agents_directory(self) -> None:
        """Python would try to write ``.pyc`` files next to the modules.

        They are only read, so the bytecode is worth nothing — but the
        *attempt* would be a write into a directory the service may not
        write, and the resulting noise hides a real permission problem.
        """
        self.assertIn(
            "PYTHONDONTWRITEBYTECODE=1",
            self.body,
            "the image must not try to write .pyc files into /home/agent",
        )


class TestTheFixDidNotTakeAnythingAway(unittest.TestCase):
    """Root ownership is only free if nothing needed to write there."""

    def setUp(self) -> None:
        self.body = _strip_comments(SANDBOX_DOCKERFILE.read_text(encoding="utf-8"))

    def test_every_agent_module_is_still_copied_into_the_image(self) -> None:
        """A module left out of the image fails the container, not the build."""
        for module in AGENT_MODULES + SHARED_MODULES:
            self.assertIn(
                f"/home/agent/{module}",
                self.body,
                f"{module} is no longer copied into /home/agent",
            )

    def test_the_models_own_writable_places_survive(self) -> None:
        """The pip cache and the workspace are the model's to write.

        Root ownership of the code must not take the model's own scratch
        space with it: ``pip install`` writes into the cache, and the
        model writes into the workspace.
        """
        self.assertIn(
            "chown 1000:1000 /workspace /var/cache/apt /home/agent/.cache/pip",
            self.body,
            "the model's workspace and pip cache must stay owned by its own uid",
        )


class TestTheDocsSayWhatTheImageDoes(unittest.TestCase):
    """The document has to name the rule, not just the change."""

    def test_the_rule_is_written_down(self) -> None:
        flat = _flat(SECURITY.read_text(encoding="utf-8"))
        self.assertIn(
            "the agent's modules owned by root, the directory left unwritable by uid 1000",
            flat,
            "docs/SECURITY.md must state the ownership rule that the image implements",
        )

    def test_the_measured_fact_is_written_down(self) -> None:
        """The rule is only credible with the measurement behind it."""
        flat = _flat(SECURITY.read_text(encoding="utf-8"))
        self.assertIn(
            "The agent's own code is writable without `sudo` — and a restart runs it",
            flat,
            "docs/SECURITY.md must name the measured defect, not only the fix",
        )


if __name__ == "__main__":
    unittest.main()
