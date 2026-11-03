"""Tripwires for the deploy order: the stack must not take the ceiling with it.

Context. The workspace is a filesystem with a ceiling: an ext4 filesystem
in a file, mounted on the host, handed to the container as a volume. When
Docker removes the container, the container's mount namespace goes with
it — and so does the host bind that was inside it. The workspace stops
being a filesystem with a ceiling and becomes a plain directory on the
host's own partition.

``docker compose up --force-recreate`` removes the old container and
starts the new one in one command, and the mount cannot be re-made in
between. Measured on the production stand on 2026-11-03: the deploy was
ordered "mount the filesystem, then start the stack", the agent
container was recreated, the filesystem went away with it, and the new
container came up on the host's disk and refused to start — with the
service down.

``docs/SECURITY.md`` section 4 has stated the rule since v2.21.0 —
*stack down, filesystem mounted, stack up* — and the repository's own
paths did not follow it. ``make restart`` ran
``up -d --build --force-recreate``, which is exactly the order the rule
forbids. These tests are the claim and the implementation meeting.

The order is pinned positionally: three commands in a script, and only
one order of them is correct.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
RECREATE = ROOT / "tools" / "recreate.sh"
CI = ROOT / ".github" / "workflows" / "ci.yml"
CI_COMPOSE = ROOT / "docker-compose.ci.yml"
SECURITY = ROOT / "docs" / "SECURITY.md"


def _flat(text: str) -> str:
    """Collapse the wrapping of a prose document."""
    return " ".join(text.split())


def _makefile_target(text: str, name: str) -> str:
    """The recipe of one Makefile target, comments dropped."""
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith(f"{name}:"):
            inside = True
            continue
        if inside:
            if line.startswith("\t"):
                out.append(line.strip())
            elif line.strip() and not line.startswith("#"):
                break
    return "\n".join(out)


class TestTheOrderIsRemoveMountStart(unittest.TestCase):
    """The script's three commands have to be in that order."""

    def setUp(self) -> None:
        self.body = RECREATE.read_text(encoding="utf-8")

    def test_the_script_removes_the_container_first(self) -> None:
        self.assertIn('docker compose -f "$COMPOSE_FILE" rm -sf', self.body)

    def test_the_script_makes_the_mount_after_the_removal(self) -> None:
        self.assertIn("tools/setup-workspace.sh", self.body)

    def test_the_script_starts_the_stack_last(self) -> None:
        self.assertIn('docker compose -f "$COMPOSE_FILE" up -d --build', self.body)

    def test_the_order_is_remove_mount_start(self) -> None:
        """Only one order is correct, and the order is what is pinned.

        The three commands have to appear in the file in this order: the
        container is removed, then the mount is made, then the stack is
        started. A script that starts the stack before it makes the
        mount has the old defect — the bind is resolved against a plain
        directory.
        """
        remove = self.body.index("rm -sf")
        mount = self.body.index("setup-workspace.sh")
        start = self.body.index("up -d --build")
        self.assertLess(
            remove,
            mount,
            "the container has to be removed before the mount is made — "
            "the removal is what takes the mount away",
        )
        self.assertLess(
            mount,
            start,
            "the mount has to be made before the stack starts — the bind "
            "is resolved when the container is created",
        )

    def test_the_script_does_not_guess_the_ceiling(self) -> None:
        """A default here is one more place for the guess to be wrong.

        The path and the size belong to the deployment, so the script
        refuses to run without them instead of picking one.
        """
        self.assertIn("AGENT_WORKSPACE_PATH:?AGENT_WORKSPACE_PATH must be set", self.body)
        self.assertIn("AGENT_WORKSPACE_SIZE:?AGENT_WORKSPACE_SIZE must be set", self.body)
        self.assertNotIn(
            "AGENT_WORKSPACE_PATH:-",
            self.body,
            "the script must not fall back to a default workspace path",
        )
        self.assertNotIn(
            "AGENT_WORKSPACE_SIZE:-",
            self.body,
            "the script must not fall back to a default ceiling",
        )


class TestTheMakefileFollowsTheRule(unittest.TestCase):
    """``make restart`` is the path that used to break the ceiling."""

    def setUp(self) -> None:
        self.text = MAKEFILE.read_text(encoding="utf-8")

    def test_restart_goes_through_the_order(self) -> None:
        recipe = _makefile_target(self.text, "restart")
        self.assertIn("tools/recreate.sh", recipe)
        self.assertNotIn(
            "--force-recreate",
            recipe,
            "a recreate in one command is the order the rule forbids: the "
            "mount cannot be made between the removal and the start",
        )

    def test_restart_declares_the_ceiling(self) -> None:
        recipe = _makefile_target(self.text, "restart")
        self.assertIn("AGENT_WORKSPACE_SIZE=", recipe)

    def test_up_declares_the_ceiling(self) -> None:
        """A ceiling that exists and is not declared is not checked.

        The workspace on this path *is* a filesystem with that ceiling,
        so the agent has to be told what it was promised — otherwise the
        check that refuses a plain directory never runs.
        """
        recipe = _makefile_target(self.text, "up")
        self.assertIn("AGENT_WORKSPACE_SIZE=", recipe)


class TestTheCiFollowsTheRuleToo(unittest.TestCase):
    """The CI is where the order is measured, so it has to follow it."""

    def setUp(self) -> None:
        self.text = CI.read_text(encoding="utf-8")

    def test_no_recreate_in_one_command_in_the_workflow(self) -> None:
        """``up -d --force-recreate`` removes and starts in one command."""
        offenders = [
            line.strip() for line in self.text.splitlines() if "up -d --force-recreate" in line
        ]
        self.assertEqual(
            offenders,
            [],
            "the workflow recreates the agent in one command, which takes "
            f"the mount with it: {offenders}",
        )

    def test_the_ci_recreates_through_the_order(self) -> None:
        self.assertIn("tools/recreate.sh docker-compose.ci.yml sandbox-agent", self.text)

    def test_the_ci_declares_the_ceiling_it_mounts(self) -> None:
        """The CI mounts a filesystem of a size, and says so to the agent."""
        compose = CI_COMPOSE.read_text(encoding="utf-8")
        self.assertIn("AGENT_WORKSPACE_SIZE=${AGENT_WORKSPACE_SIZE:-1g}", compose)


class TestTheDocumentNamesTheOrder(unittest.TestCase):
    """The rule is in the document; the paths have to follow it."""

    def test_the_rule_is_stated(self) -> None:
        flat = _flat(SECURITY.read_text(encoding="utf-8"))
        self.assertIn(
            "the order is: stack down, filesystem mounted, stack up",
            flat,
            "docs/SECURITY.md must state the order the deploy follows",
        )

    def test_the_repos_own_paths_are_named(self) -> None:
        """A rule with no implementation is a wish."""
        flat = _flat(SECURITY.read_text(encoding="utf-8"))
        self.assertIn(
            "tools/recreate.sh",
            flat,
            "docs/SECURITY.md must name the script that carries the order",
        )


if __name__ == "__main__":
    unittest.main()
