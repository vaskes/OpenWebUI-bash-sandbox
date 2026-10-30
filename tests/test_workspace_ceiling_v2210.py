"""The workspace's mount is local to the stack, and the agent refuses a
workspace that has lost its ceiling.

Two claims, and both of them are about the quiet failure:

* the mount is made when the stack starts, and it is **not** written
  into the host's `/etc/fstab` — the host's boot does not silently
  bring a filesystem with an agent's files into the picture;
* a container that comes up without the mount finds a plain directory,
  and a plain directory has no ceiling. The agent compares the size of
  the filesystem it is looking at with the ceiling the deployment
  declared, and refuses to serve when they disagree — so the failure
  is loud instead of quiet.

The second claim is checked by running the function, with a real
filesystem under it: a check that only reads the source would be
satisfied by a comment.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import agent_api

ROOT = Path(__file__).resolve().parent.parent


def _code_lines(text: str) -> list[str]:
    """The lines of a file that are code, not prose."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


class TheMountIsLocalToTheStack(unittest.TestCase):
    """Nothing in the repository puts the workspace into the host's
    boot: the mount belongs to the stack's lifecycle."""

    def test_there_is_no_tool_that_writes_into_the_boots_table(self) -> None:
        self.assertFalse(
            (ROOT / "tools" / "install-workspace-mount.sh").exists(),
            "the tool that wrote the workspace into /etc/fstab is back",
        )

    def test_no_shipped_file_mentions_the_boots_table(self) -> None:
        # The changelog is history: it says what v2.19.0 did, and it is
        # not a configuration. Everything else that ships is.
        offenders = []
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT)
            if rel.parts[0] in (".git", "tests") or rel.name == "CHANGELOG.md":
                continue
            if path.suffix not in (".sh", ".md", ".py", ".yml", ".yaml", ".toml", ".cfg"):
                continue
            if path.name in ("Makefile", "Dockerfile.sandbox", "Dockerfile.openapi-proxy"):
                pass
            else:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "/etc/fstab" in text:
                offenders.append(str(rel))
        self.assertEqual(offenders, [], "a shipped file still points at the host's boot table")

    def test_the_makefile_does_not_install_a_boot_entry(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertNotIn(
            "install-workspace-mount.sh",
            makefile,
            "the workspace target installs a boot entry again",
        )

    def test_the_ceiling_travels_into_the_container(self) -> None:
        for name in ("docker-compose.yml", "docker-compose.production.yml"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn(
                "AGENT_WORKSPACE_SIZE=",
                text,
                f"{name} does not tell the container what the ceiling is",
            )


class TheAgentRefusesAWorkspaceWithoutACeiling(unittest.TestCase):
    """The check runs against a real filesystem: the size of the
    filesystem under the agent has to be the size that was asked for."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="owbs-ceiling-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.workspace = Path(self.tmp) / "workspace"
        self.workspace.mkdir()

    def _state(self, declared: str) -> tuple[bool, str]:
        original = agent_api.WORKSPACE_SIZE
        original_root = agent_api.WORKSPACE
        agent_api.WORKSPACE_SIZE = declared
        agent_api.WORKSPACE = str(self.workspace)
        try:
            return agent_api.workspace_ceiling_state()
        finally:
            agent_api.WORKSPACE_SIZE = original
            agent_api.WORKSPACE = original_root

    def test_a_workspace_that_is_not_the_declared_size_is_refused(self) -> None:
        # The directory under the agent is a few kilobytes; a ceiling of
        # 4 GiB is not what it is.
        intact, why = self._state("4g")
        self.assertFalse(intact, "a plain directory passed as the ceiling")
        self.assertIn("4g", why, "the refusal does not name the ceiling")

    def test_a_workspace_that_matches_the_ceiling_is_accepted(self) -> None:
        # The ceiling is declared as the size of the filesystem that is
        # really there, so the check has to accept it.
        stat = os.statvfs(self.workspace)
        have = stat.f_blocks * stat.f_frsize
        intact, why = self._state(str(have))
        self.assertTrue(intact, f"the matching workspace was refused: {why}")

    def test_a_deployment_without_a_ceiling_is_left_alone(self) -> None:
        intact, why = self._state("")
        self.assertTrue(intact, "a deployment without a ceiling was refused")
        self.assertIn("no ceiling", why)

    def test_the_refusal_stops_the_agent(self) -> None:
        original = agent_api.WORKSPACE_SIZE
        original_root = agent_api.WORKSPACE
        agent_api.WORKSPACE_SIZE = "4g"
        agent_api.WORKSPACE = str(self.workspace)
        try:
            with self.assertRaises(SystemExit) as caught:
                agent_api.enforce_workspace_ceiling()
        finally:
            agent_api.WORKSPACE_SIZE = original
            agent_api.WORKSPACE = original_root
        self.assertEqual(caught.exception.code, 2)

    def test_the_sizes_are_read_as_sizes(self) -> None:
        self.assertEqual(agent_api._parse_size("4g"), 4 * 1024**3)
        self.assertEqual(agent_api._parse_size("512m"), 512 * 1024**2)
        self.assertEqual(agent_api._parse_size("1t"), 1024**4)
        self.assertIsNone(agent_api._parse_size(""))
        self.assertIsNone(agent_api._parse_size("four gigabytes"))
        self.assertIsNone(agent_api._parse_size("4 gigabytes"))


class TheShippedCodeSaysWhatItDoes(unittest.TestCase):
    """The claim in the documentation is a claim about the code."""

    def test_the_agent_really_checks_the_size(self) -> None:
        source = (ROOT / "agent_api.py").read_text(encoding="utf-8")
        code = "\n".join(_code_lines(source))
        self.assertIn("os.statvfs", code, "the agent does not read the size of the filesystem")
        self.assertIn(
            "AGENT_WORKSPACE_SIZE",
            code,
            "the agent does not know the ceiling it was promised",
        )

    def test_the_documentation_says_the_mount_is_local(self) -> None:
        for name in ("docs/CONFIGURATION.md", "docs/SECURITY.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn(
                "AGENT_WORKSPACE_SIZE",
                text,
                f"{name} does not mention the ceiling the agent checks",
            )


if __name__ == "__main__":
    unittest.main()
