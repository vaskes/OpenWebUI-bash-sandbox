"""A root shell in a read-only container is a half-root.

The privilege mode (docs/SECURITY.md) hands the model a root shell.
Whether that root is *useful* is a second question, and it is decided
by a different switch: the container's root filesystem.

``apt-get`` needs three writable paths — ``/var/lib/apt/lists`` (the
index), ``/var/lib/dpkg`` (the package database) and ``/usr`` (where
the packages land). A read-only rootfs refuses all three, so the
model's ``apt-get install`` dies with *"Read-only file system"* and the
operator concludes the sandbox is broken, while the deployment is
merely a half-root.

These tests pin the detection (so the agent can announce the hollow
root at startup) and the knob (so a deployment can turn it off
deliberately).
"""

from __future__ import annotations

import logging
import unittest
from pathlib import Path

from agent_api import _warn_about_the_read_only_rootfs
from agent_core import rootfs_is_read_only

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.ci.yml",
    "docker-compose.production.yml",
)

#: What /proc/self/mountinfo looks like in a container with a writable
#: root: the root mount, then the tmpfs and the bind-mounts below it.
WRITABLE_MOUNTINFO = """\
25 0 8:1 / / rw,relatime - overlay overlay rw,lowerdir=/var/lib/docker/overlay2/l/AAA
26 0 8:2 / /workspace rw,relatime - ext4 /dev/sda1 rw
27 0 0:24 / /tmp rw,nosuid,nodev,relatime - tmpfs tmpfs rw,size=268435456
"""

#: The same container with `read_only: true`: the root mount is "ro".
READ_ONLY_MOUNTINFO = """\
25 0 8:1 / / ro,relatime - overlay overlay ro,lowerdir=/var/lib/docker/overlay2/l/AAA
26 0 8:2 / /workspace rw,relatime - ext4 /dev/sda1 rw
27 0 0:24 / /tmp rw,nosuid,nodev,relatime - tmpfs tmpfs rw,size=268435456
"""


class TestRootfsDetection(unittest.TestCase):
    """The detection decides what the operator is told at startup."""

    def test_a_writable_root_is_not_read_only(self) -> None:
        self.assertFalse(rootfs_is_read_only(WRITABLE_MOUNTINFO))

    def test_a_read_only_root_is_read_only(self) -> None:
        self.assertTrue(rootfs_is_read_only(READ_ONLY_MOUNTINFO))

    def test_the_root_mount_is_the_one_that_counts(self) -> None:
        """A read-only *workspace* does not make the root filesystem read-only.

        The workspace is a bind-mount in the stock deployment and it is
        writable; the root mount above it is what carries the "ro" flag.
        """
        blob = "26 0 8:2 / /workspace ro,relatime - ext4 /dev/sda1 ro\n" + WRITABLE_MOUNTINFO
        self.assertFalse(rootfs_is_read_only(blob))

    def test_an_unreadable_mount_table_counts_as_read_only(self) -> None:
        """A mount table that cannot be parsed must not be read as "it can write".

        The same rule the capability check follows: an unknown state is
        the safe one.
        """
        self.assertTrue(rootfs_is_read_only(""))
        self.assertTrue(rootfs_is_read_only("garbage\nmore garbage\n"))

    def test_the_superblock_options_are_consulted_too(self) -> None:
        blob = "25 0 8:1 / / rw,relatime - overlay overlay ro,lowerdir=/x\n"
        self.assertTrue(rootfs_is_read_only(blob))

    def test_a_dash_in_a_mount_point_is_not_the_separator(self) -> None:
        """A mount point may contain a dash; the separator is " - "."""
        blob = "28 0 8:3 / /mnt/my-disk rw,relatime - ext4 /dev/sdb1 rw\n" + WRITABLE_MOUNTINFO
        self.assertFalse(rootfs_is_read_only(blob))


class TestTheStartupWarning(unittest.TestCase):
    """A hollow root must announce itself, not wait for the model to find out."""

    def test_a_read_only_root_is_announced(self) -> None:
        with self.assertLogs(level=logging.WARNING) as captured:
            _warn_about_the_read_only_rootfs(READ_ONLY_MOUNTINFO)
        message = "\n".join(captured.output)
        self.assertIn("READ-ONLY", message)
        self.assertIn("AGENT_READ_ONLY=false", message)
        self.assertIn("apt-get", message)

    def test_a_writable_root_says_nothing(self) -> None:
        with self.assertNoLogs(level=logging.WARNING):
            _warn_about_the_read_only_rootfs(WRITABLE_MOUNTINFO)


class TestTheKnob(unittest.TestCase):
    """The fourth setting of the privilege mode: a knob with a safe default."""

    KNOB = 'read_only: "${AGENT_READ_ONLY:-true}"'

    def test_every_stack_carries_the_knob(self) -> None:
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn(self.KNOB, text)

    def test_the_stock_posture_stays_read_only(self) -> None:
        """A knob whose default is not the old value changes every deployment."""
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn(self.KNOB, text)
                self.assertNotIn("read_only: false", text)

    def test_only_the_agent_service_carries_the_knob(self) -> None:
        """The proxy never runs model commands; it keeps the strict posture."""
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                if "  sandbox-agent:" not in text:
                    continue
                agent = text.split("  sandbox-agent:", 1)[1].split("  openapi-proxy:", 1)[0]
                self.assertIn(self.KNOB, agent)


if __name__ == "__main__":
    unittest.main()
