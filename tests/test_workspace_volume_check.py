"""The workspace check has to refuse a path that is not a filesystem.

The workspace is an ext4 filesystem in a file, mounted on the host, and
the container's volume binds its mount point. A volume that binds the
path is therefore not yet a workspace with a ceiling: the path is only
the workspace while a *filesystem* is mounted on it. When the mount is
gone, the path is a plain directory on the host's disk and the container
starts on the host's partition -- 1.8 TB instead of the 4 GB that were
promised.

The agent notices and refuses to start, which is the ceiling check
doing its job. But by then the deployment has already taken the stack
down to find out, and the deployment that did it is the one that
recreated the container in the first place: a container that is removed
takes the host mount with it. That is what happened on the production
stand on 2026-11-03: the deploy was ordered "mount the filesystem, then
start the stack", the container was recreated, the filesystem was
unmounted on the way, and the new container bound the plain directory.

So the check that runs *before* the stack starts has to say no to a path
that is not a filesystem. This is that check, tested on the only case
a test can make: a plain directory.

A check that cannot be made red is a decoration:
``tools/mutation-check-v2250.sh`` breaks the refusal on purpose and
expects the named test to go red.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "tools" / "check-workspace-volume.sh"


class TestTheWorkspacePathMustBeAFilesystem(unittest.TestCase):
    """A plain directory is not a workspace with a ceiling."""

    def _run(self, path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(CHECK), str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=ROOT,
        )

    def test_a_plain_directory_is_refused(self) -> None:
        """The check refuses a path that carries no filesystem."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(Path(tmp))
        self.assertEqual(
            result.returncode,
            1,
            "the workspace check does not refuse a plain directory with its "
            "own exit code: the ceiling would be a promise, and the agent "
            f"would start on the host's partition (said: {result.stderr!r})",
        )

    def test_the_refusal_says_what_is_missing(self) -> None:
        """The refusal names the path and what is wrong with it."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(Path(tmp))
        message = result.stderr + result.stdout
        self.assertIn(
            "not an ext4 filesystem",
            message,
            "the refusal does not say what is wrong with the workspace",
        )
        self.assertIn(
            "no ceiling",
            message,
            "the refusal does not say what it means: a volume that binds a "
            "plain directory has no ceiling",
        )

    def test_the_filesystem_check_comes_before_the_early_exit(self) -> None:
        """The check must not be skipped when there is no volume yet.

        The script has an early exit for a deployment that has no
        workspace volume yet -- compose makes it when the stack starts.
        That exit must not come before the filesystem check: a first
        deployment with an unmounted workspace is exactly the case that
        starts a stack on the host's partition.

        The order cannot be observed from the outside -- the refusal
        happens first either way -- so it is pinned in the file itself:
        the check has to be written above the exit it must not reach.
        """
        source = CHECK.read_text(encoding="utf-8")
        check_at = source.index("is not an ext4 filesystem")
        exit_at = source.index("there is no workspace volume yet")
        self.assertLess(
            check_at,
            exit_at,
            "the filesystem check is written below the early exit: a "
            "deployment with no volume yet would wave through before the "
            "workspace was ever looked at",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
