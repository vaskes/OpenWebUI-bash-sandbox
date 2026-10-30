"""The ceiling grows the workspace; it never wipes it.

This file pins three facts that a deployment depends on and that
nothing else would notice breaking.

**A bigger ceiling grows the workspace, and the files stay.** The
ceiling is the size of the image file, and the image file is the disk
of the filesystem inside it, so a bigger ceiling is the same disk with
more room: ``truncate`` the file, tell the loop device the file grew,
``resize2fs`` the filesystem. Making a new filesystem instead would
throw the agent's work away, and the work is the one thing in the
workspace that cannot be re-created. That is not a theoretical loss:
the workspace's own users lost files to it, and the loss looked like
"the sandbox wipes itself on restart" -- which was wrong about the
cause and right about the pain.

**A smaller ceiling is refused.** Shrinking a filesystem means
deciding what to throw away, and that decision belongs to a human.

**The mount is put back at boot.** The workspace is an ext4
filesystem in a file, mounted on the host. The mount is a fact about
the running kernel, not about the file: after a reboot the file is
intact and nothing is mounted, so the directory the volume binds is an
ordinary directory on the host's partition again -- and a directory
has no ceiling. The failure is quiet, which is the worst kind: the
container starts, the agent writes, and the host's disk fills up. An
entry in ``/etc/fstab`` puts the mount back before the containers come
up, and it carries ``nosuid`` and ``nodev`` because the entry that is
in force after a reboot is a boundary too.

What is pinned here:

* the plan -- one word, no root, no changes -- says what the setup
  script would do, and it says "grow" for a bigger ceiling and
  "refuse" for a smaller one;
* the script never deletes the image, and never formats over a file it
  did not make;
* a file that is not a filesystem is left byte-for-byte alone;
* the default ceiling is 4 GiB, and it is a parameter, not a constant;
* the boot entry carries the boundary, does not stop the boot, is
  written once, and is checked;
* the documents say the same thing.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parents[1]
SETUP = ROOT / "tools" / "setup-workspace.sh"
INSTALL_MOUNT = ROOT / "tools" / "install-workspace-mount.sh"


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _code_lines(text: str) -> list[str]:
    """The lines that are code, not commentary.

    A check that looks for a word finds it in the comment above the
    command as easily as in the command itself, and a check that a
    comment can satisfy is a check that a mutation cannot turn red.
    """
    return [raw for raw in text.splitlines() if raw.strip() and not raw.lstrip().startswith("#")]


def _plan(mountpoint: Path, size: str) -> str:
    """Ask the script what it would do. One word, no changes."""
    done = subprocess.run(
        ["bash", str(SETUP), "--plan", str(mountpoint), size],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )
    return done.stdout.strip()


def _install_mount(fstab: Path, mountpoint: str, image: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["FSTAB"] = str(fstab)
    return subprocess.run(
        ["bash", str(INSTALL_MOUNT), mountpoint, image],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
        env=env,
    )


class ThePlanSaysWhatWouldHappen(unittest.TestCase):
    """The decision is one function, and it is asked, not read."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.mountpoint = self.dir / "workspace"
        self.image = self.dir / "workspace.img"
        self.mountpoint.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_image(self, size_bytes: int) -> None:
        self.image.write_bytes(b"")
        os.truncate(self.image, size_bytes)

    def test_there_is_no_workspace_yet(self) -> None:
        self.assertEqual(_plan(self.mountpoint, "64m"), "create")

    def test_the_size_matches(self) -> None:
        self._make_image(64 * 1024 * 1024)
        self.assertEqual(_plan(self.mountpoint, "64m"), "same")

    def test_the_ceiling_is_bigger(self) -> None:
        """A bigger ceiling grows the workspace."""
        self._make_image(64 * 1024 * 1024)
        self.assertEqual(_plan(self.mountpoint, "128m"), "grow")

    def test_the_ceiling_is_smaller(self) -> None:
        """A smaller ceiling is refused -- not done quietly."""
        self._make_image(128 * 1024 * 1024)
        self.assertEqual(_plan(self.mountpoint, "64m"), "refuse")


class TheWorkspaceIsNotWiped(unittest.TestCase):
    """The code, not the comment about the code."""

    def setUp(self) -> None:
        self.code = "\n".join(_code_lines(_read("tools/setup-workspace.sh")))

    def test_the_image_is_never_deleted(self) -> None:
        for line in self.code.splitlines():
            self.assertNotRegex(
                line,
                r"\brm\b[^|;&]*\bIMAGE\b",
                "the setup script deletes the workspace image: the image is the "
                "only copy of the agent's work, and a ceiling that changes must "
                "not be the thing that throws it away",
            )

    def test_the_workspace_is_never_formatted_over(self) -> None:
        """The formatting is behind a guard: only when there is no file."""
        self.assertRegex(
            self.code,
            r'if\s+\[\s+!\s+-e\s+"\$IMAGE"\s*\]\s*;',
            "the filesystem is made without a guard on the file's existence: a "
            "file that is there would be formatted over, and that destroys "
            "whatever was in it",
        )

    def test_a_file_that_is_not_a_filesystem_is_not_touched(self) -> None:
        """The grow checks what the file is before it makes it bigger.

        The check is on the order, not on the presence of a word: the
        file is asked whether it is a filesystem BEFORE it is
        truncated. A file that is not one is somebody else's file, and
        growing it would change it without anybody deciding to.
        """
        body = self.code.split("grow_workspace()", 1)[-1]
        asked = body.find("dumpe2fs")
        grown = body.find("truncate -s")
        self.assertNotEqual(asked, -1, "the grow path never asks what the file is")
        self.assertNotEqual(grown, -1, "the grow path never makes the file bigger")
        self.assertLess(
            asked,
            grown,
            "the file is made bigger before it is asked what it is: a file "
            "that is not a filesystem would be changed before anybody could "
            "say so",
        )

    def test_the_default_ceiling_is_four_gibibytes(self) -> None:
        """The default is a parameter with a default, and the default is 4G.

        The size is not a constant in the code because the host's disk
        is the narrow one: a ceiling that is right on an empty disk is
        a bomb on a full one, and the operator has to be able to move
        it without editing a script.
        """
        self.assertRegex(
            self.code,
            r"AGENT_WORKSPACE_SIZE:-4g",
            "the default ceiling is not 4 GiB: AGENT_WORKSPACE_SIZE must "
            "default to 4g, and the size must stay a parameter",
        )

    def test_the_makefile_says_the_same_thing(self) -> None:
        """The Makefile is a second copy of the default.

        A rule that lives in two places rots in two places: the script
        says 4g and the Makefile says 1g, and `make workspace` builds a
        workspace that is not the one the documentation describes. So
        the second copy is pinned too.
        """
        makefile = _read("Makefile")
        self.assertRegex(
            makefile,
            r"(?m)^WORKSPACE_SIZE \?= 4g$",
            "the Makefile's default ceiling is not 4 GiB: `make workspace` "
            "would build a different workspace than the script's default",
        )


class TheWorkspaceIsMountedAtBoot(unittest.TestCase):
    """The mount survives the host, not just the deployment."""

    @staticmethod
    def _target(makefile: str, name: str) -> str:
        """The body of one Makefile target.

        A Makefile has more than one target, and a check that reads the
        whole file is satisfied by any of them: the `workspace` target
        installs the boot entry and the `workspace-check` target checks
        it, so a check for "the string is in the Makefile" says nothing
        about the one that has to install it. The body is read from the
        target's header to the next target.
        """
        body: list[str] = []
        inside = False
        for line in makefile.splitlines():
            if not inside:
                if line.startswith(f"{name}:"):
                    inside = True
                continue
            if line and not line[0].isspace():
                break
            body.append(line)
        return "\n".join(body)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fstab = Path(self._tmp.name) / "fstab"
        self.fstab.write_text("UUID=abc / ext4 defaults 0 1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _entry(self) -> str:
        for line in self.fstab.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) > 1 and fields[1] == "/opt/owbs-workspace":
                return line
        return ""

    def test_the_entry_carries_the_boundary(self) -> None:
        """nosuid and nodev: the entry in force after a reboot is a boundary.

        The workspace root is mode 1777 on the host's disk, and in the
        privilege mode the agent can become uid 0 inside the container
        -- which is uid 0 on the host. Without nosuid a file in the
        workspace with the setuid bit is a setuid-root file on the
        host; without nodev a device node in it is a device node.
        """
        _install_mount(self.fstab, "/opt/owbs-workspace", "/opt/owbs-workspace.img")
        entry = self._entry()
        self.assertTrue(entry, "no entry for the workspace was written")
        options = entry.split()[3]
        self.assertIn("nosuid", options.split(","), "the boot entry mounts without nosuid")
        self.assertIn("nodev", options.split(","), "the boot entry mounts without nodev")
        self.assertIn(
            "loop", options.split(","), "the boot entry does not mount the image as a filesystem"
        )

    def test_the_entry_does_not_stop_the_boot(self) -> None:
        """A missing image must not keep the host from booting."""
        _install_mount(self.fstab, "/opt/owbs-workspace", "/opt/owbs-workspace.img")
        options = self._entry().split()[3]
        self.assertIn(
            "nofail",
            options.split(","),
            "the boot entry is not nofail: a missing image would stop the host "
            "from booting, and a host that does not boot helps nobody",
        )

    def test_the_entry_is_written_once(self) -> None:
        """Installing twice does not install twice."""
        _install_mount(self.fstab, "/opt/owbs-workspace", "/opt/owbs-workspace.img")
        _install_mount(self.fstab, "/opt/owbs-workspace", "/opt/owbs-workspace.img")
        entries = [
            line
            for line in self.fstab.read_text(encoding="utf-8").splitlines()
            if len(line.split()) > 1 and line.split()[1] == "/opt/owbs-workspace"
        ]
        self.assertEqual(len(entries), 1, "the boot entry was written more than once")

    def test_the_entry_is_checked(self) -> None:
        """The check accepts the entry it would write."""
        _install_mount(self.fstab, "/opt/owbs-workspace", "/opt/owbs-workspace.img")
        env = dict(os.environ)
        env["FSTAB"] = str(self.fstab)
        done = subprocess.run(
            [
                "bash",
                str(INSTALL_MOUNT),
                "--check",
                "/opt/owbs-workspace",
                "/opt/owbs-workspace.img",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(ROOT),
            env=env,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_a_tampered_entry_is_refused(self) -> None:
        """An entry without the boundary is not the entry it should be."""
        _install_mount(self.fstab, "/opt/owbs-workspace", "/opt/owbs-workspace.img")
        text = self.fstab.read_text(encoding="utf-8").replace("nosuid,", "")
        self.fstab.write_text(text, encoding="utf-8")
        env = dict(os.environ)
        env["FSTAB"] = str(self.fstab)
        done = subprocess.run(
            [
                "bash",
                str(INSTALL_MOUNT),
                "--check",
                "/opt/owbs-workspace",
                "/opt/owbs-workspace.img",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(ROOT),
            env=env,
        )
        self.assertNotEqual(done.returncode, 0, "an entry without nosuid passed the check")

    def test_the_makefile_installs_the_boot_entry(self) -> None:
        """`make workspace` puts the mount back at boot too.

        The entry is the only thing that survives a reboot of the host,
        and the Makefile is how a host is set up. A `make workspace` that
        made the filesystem and stopped would leave a host whose ceiling
        quietly disappears on the next boot.
        """
        target = self._target(_read("Makefile"), "workspace")
        self.assertTrue(target, "the Makefile has no `workspace` target")
        self.assertIn(
            "tools/install-workspace-mount.sh",
            target,
            "the `workspace` target does not install the boot entry: after a "
            "reboot the workspace would be a plain directory, and a plain "
            "directory has no ceiling",
        )


class TheDocumentsSayTheSameThing(unittest.TestCase):
    """Documentation is a claim about the tree, and it is checked."""

    @staticmethod
    def _section(text: str, heading: str) -> str:
        """The part of the document under one heading.

        A claim that lives in a section has to be checked in that
        section: the same words somewhere else in the file are a
        different claim.
        """
        parts = text.split(f"### {heading}")
        if len(parts) < 2:
            return ""
        return parts[1].split("\n### ", 1)[0]

    def test_the_configuration_reference_names_the_default_ceiling(self) -> None:
        section = self._section(_read("docs/CONFIGURATION.md"), "`AGENT_WORKSPACE_SIZE`")
        self.assertTrue(section, "the configuration reference has no AGENT_WORKSPACE_SIZE section")
        self.assertRegex(
            section,
            r"Default: `4g`",
            "the configuration reference does not say that the default ceiling is 4g",
        )

    def test_the_configuration_reference_says_that_the_workspace_grows(self) -> None:
        section = self._section(_read("docs/CONFIGURATION.md"), "`AGENT_WORKSPACE_SIZE`")
        self.assertRegex(
            section,
            r"(?i)grow",
            "the configuration reference does not say that a bigger ceiling grows the workspace",
        )
        self.assertRegex(
            section,
            r"(?i)(refus|smaller)",
            "the configuration reference does not say that a smaller ceiling is refused",
        )

    def test_the_readme_says_that_the_workspace_survives(self) -> None:
        readme = _read("README.md")
        self.assertRegex(
            readme,
            r"(?i)survives a container recreate",
            "the README does not say that the workspace survives a container recreate",
        )

    def test_the_security_model_names_the_ceiling(self) -> None:
        security = _read("docs/SECURITY.md")
        self.assertIn("AGENT_WORKSPACE_SIZE", security)
