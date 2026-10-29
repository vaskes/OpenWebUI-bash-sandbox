"""The sandbox's storage is capped, and the host's disk is out of reach.

The damage class this file exists for was measured, not guessed.

The production stack used to bind a host directory over the workspace::

    - /opt/telegrambot/sandbox-workspace:/workspace

That directory sits on the same ext4 partition the whole host lives on
(``nvme0n1p4``, 1.8 T, 94 % used, 106 G free when it was found), so a
``dd`` in the sandbox filled the operator's disk. Every other class of
damage the sandbox can do — a wiped ``/etc``, a deleted session, a
fork-bomb — dies with the container. A full disk does not: a reset
brings ``/etc`` back and does not bring a disk back.

So the rule this file pins is narrow and mechanical:

* the workspace is a **tmpfs inside the container**, with a **ceiling**;
* **no host path is mounted writable** into the agent;
* the agent has **no named volume** at all — a named volume is a
  directory on the host's disk, and a writable one is a hole in exactly
  the bound the sandbox claims to have;
* the tmpfs budget **fits the memory ceiling**, because a tmpfs is RAM
  and the sizes are charged against ``mem_limit``;
* the root filesystem is **read-only by default**, because the writable
  layer is a host directory with no quota on it.

The numbers are read out of the compose files rather than written here,
so a new stack gets the same checks without anybody remembering to add
them: a list of facts that lives in the tree is checked where it lives.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from agent_config import parse_size

ROOT = Path(__file__).resolve().parent.parent

#: Every stack this repository ships. The CI stack counts: it is a
#: deployment like any other, and a rule that only the two "real" stacks
#: follow is a rule with a hole in it.
COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.ci.yml",
    "docker-compose.production.yml",
)

#: The tmpfs mount points the agent image has to contain. A tmpfs mount
#: point that is not in the image has to be created by Docker inside the
#: container's root filesystem — which is read-only by default — and
#: that is a container that does not start.
REQUIRED_MOUNTPOINTS = ("/workspace", "/var/cache/apt", "/home/agent/.cache/pip")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _service_block(text: str, service: str) -> str:
    """The body of one service, without the neighbouring services.

    The service name sits at indent two and the service's own keys at
    indent four, so a line at indent two that ends in a colon is the
    next service — that is the boundary.
    """
    out: list[str] = []
    inside = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if len(raw) - len(raw.lstrip()) == 2 and stripped.endswith(":"):
            inside = stripped == f"{service}:"
            continue
        if inside:
            out.append(raw)
    return "\n".join(out)


def _list_entries(block: str, key: str) -> list[str]:
    """The entries of a list-valued key (``tmpfs:``, ``volumes:``).

    Comments and blank lines are skipped: a commented-out mount is not
    a mount, and reading it as one would make the rule pass for the
    wrong reason.
    """
    entries: list[str] = []
    inside = False
    for raw in block.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 4 and stripped.endswith(":"):
            inside = stripped == f"{key}:"
            continue
        if inside and indent == 6 and stripped.startswith("-"):
            entries.append(stripped[1:].strip())
    return entries


def _env_default(block: str, name: str) -> str | None:
    """The default of ``NAME=${NAME:-default}`` in a service's environment."""
    match = re.search(rf"{re.escape(name)}=\$\{{{re.escape(name)}:-([^}}]+)\}}", block)
    return match.group(1) if match else None


def _scalar(block: str, key: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(key)}:\s*(\S+)\s*$", block, re.M)
    return match.group(1) if match else None


def _split_outside_braces(text: str, separator: str = ":") -> list[str]:
    """Split on a separator, but not inside a ``${...}`` interpolation.

    A tmpfs option may carry a default: ``size=${AGENT_WORKSPACE_SIZE:-1g}``.
    Splitting that on every colon cuts the interpolation in half and the
    default with it.
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in text:
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        if char == separator and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def _tmpfs_entries(block: str) -> list[tuple[str, dict[str, str]]]:
    """(mount point, options) for every tmpfs mount of a service."""
    out: list[tuple[str, dict[str, str]]] = []
    for entry in _list_entries(block, "tmpfs"):
        parts = _split_outside_braces(entry)
        path = parts[0]
        options: dict[str, str] = {}
        for option in parts[1].split(","):
            if "=" in option:
                key, value = option.split("=", 1)
                options[key.strip()] = value.strip()
        out.append((path, options))
    return out


def _volume_entries(block: str) -> list[tuple[str, str, str]]:
    """(source, target, mode) for every volume mount of a service."""
    out: list[tuple[str, str, str]] = []
    for entry in _list_entries(block, "volumes"):
        parts = entry.split(":")
        if len(parts) < 2:
            continue
        source, target = parts[0], parts[1]
        mode = parts[2] if len(parts) > 2 else "rw"
        out.append((source, target, mode))
    return out


def _option_size(value: str) -> int:
    """Bytes for a size that may still carry a ``${VAR:-default}`` wrapper."""
    match = re.search(r":-([^}]+)\}", value)
    raw = match.group(1) if match else value
    return parse_size(raw, name="tmpfs size")


class TestTheWorkspaceIsCapped(unittest.TestCase):
    """The workspace is a tmpfs with a ceiling, and nothing else."""

    def test_the_workspace_is_a_tmpfs_and_not_a_host_path(self) -> None:
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                block = _service_block(_read(name), "sandbox-agent")
                targets = [target for _source, target, _mode in _volume_entries(block)]
                self.assertNotIn(
                    "/workspace",
                    targets,
                    "the workspace is mounted from a host path again — the "
                    "sandbox's scratchpad would be a directory on the "
                    "operator's disk, and an unbounded scratchpad is a "
                    "disk-filler with a shell in front of it",
                )
                tmpfs_paths = [path for path, _options in _tmpfs_entries(block)]
                self.assertIn(
                    "/workspace",
                    tmpfs_paths,
                    "the workspace is not a tmpfs — it has to live inside "
                    "the container, where its size can be capped",
                )

    def test_the_workspace_has_a_ceiling(self) -> None:
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                block = _service_block(_read(name), "sandbox-agent")
                sizes = {
                    path: options for path, options in _tmpfs_entries(block) if path == "/workspace"
                }
                self.assertIn("/workspace", sizes)
                options = sizes["/workspace"]
                self.assertIn(
                    "size",
                    options,
                    "the workspace tmpfs has no size — an unbounded tmpfs "
                    "is a memory hog instead of a disk-filler, which is a "
                    "different problem and not a better one",
                )
                self.assertGreater(
                    _option_size(options["size"]),
                    0,
                    "the workspace ceiling is zero bytes — nothing can be written at all",
                )

    def test_the_ceiling_can_hold_a_file_the_door_accepts(self) -> None:
        """The workspace has to be able to hold what the door accepts.

        The file door takes a file up to ``AGENT_BINARY_LIMIT`` and
        writes it into the workspace. A workspace smaller than the
        policy would refuse a file the door had already promised to
        accept — the same shape as a transport ceiling below the file
        policy, and it fails the same way: the client is told "no" for
        a file that was in range.
        """
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                block = _service_block(_read(name), "sandbox-agent")
                workspace = {path: options for path, options in _tmpfs_entries(block)}["/workspace"]
                binary = _env_default(block, "AGENT_BINARY_LIMIT")
                self.assertIsNotNone(
                    binary,
                    "AGENT_BINARY_LIMIT has no default in this stack — the "
                    "ceiling cannot be compared with the policy",
                )
                self.assertGreaterEqual(
                    _option_size(workspace["size"]),
                    parse_size(binary, name="AGENT_BINARY_LIMIT"),
                    "the workspace is smaller than the file policy: the "
                    "door would accept a file that cannot land",
                )

    def test_the_tmpfs_budget_fits_the_memory_ceiling(self) -> None:
        """A tmpfs is RAM: the sizes are charged against ``mem_limit``.

        If the mounts add up to more than the container's memory limit,
        the OOM killer stops the container before its own ceiling is
        reached — the limit is then a lie in the other direction.
        """
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                block = _service_block(_read(name), "sandbox-agent")
                total = sum(
                    _option_size(options["size"])
                    for _path, options in _tmpfs_entries(block)
                    if "size" in options
                )
                limit = _scalar(block, "mem_limit")
                self.assertIsNotNone(limit)
                self.assertLessEqual(
                    total,
                    parse_size(limit, name="mem_limit"),
                    f"the tmpfs mounts add up to {total} bytes, more than "
                    f"the {limit} memory limit of the container",
                )


class TestNothingHostSideIsWritable(unittest.TestCase):
    """No host path is mounted writable, and the agent has no volume."""

    def test_the_agent_has_no_named_volume(self) -> None:
        """A named volume is a directory on the host's disk.

        The apt and pip caches were volumes until v2.16.0. A writable one
        is a hole in the bound the sandbox claims to have, so the agent
        has none: every path it may write is a tmpfs with a ceiling.
        """
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                block = _service_block(_read(name), "sandbox-agent")
                named = [
                    source
                    for source, _target, _mode in _volume_entries(block)
                    if not source.startswith("/")
                ]
                self.assertEqual(
                    named,
                    [],
                    "the agent has a named volume — that is a directory on "
                    "the host's disk, and a writable one reaches past the "
                    "container",
                )

    def test_no_host_path_is_mounted_writable(self) -> None:
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                block = _service_block(_read(name), "sandbox-agent")
                writable = [
                    f"{source}:{target}"
                    for source, target, mode in _volume_entries(block)
                    if mode != "ro"
                ]
                self.assertEqual(
                    writable,
                    [],
                    "a host path is mounted writable into the agent — the "
                    "sandbox can write to the host's filesystem, which is "
                    "the one damage a container reset cannot undo",
                )

    def test_the_root_filesystem_is_read_only_by_default(self) -> None:
        """The writable layer is a host directory, and it has no quota.

        Compose has no per-container storage quota, so a writable root
        filesystem puts the host's disk back in reach. The default has
        to be read-only for the storage bound to mean anything.
        """
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                block = _service_block(_read(name), "sandbox-agent")
                match = re.search(r"read_only:\s*\"\$\{AGENT_READ_ONLY:-([^}]+)\}\"", block)
                self.assertIsNotNone(
                    match,
                    "the read_only setting has no default in this stack",
                )
                self.assertEqual(
                    match.group(1),
                    "true",
                    "the root filesystem is writable by default — the "
                    "container's writable layer is a directory on the "
                    "host's disk, with no quota on it",
                )


class TestTheImageHasTheMountPoints(unittest.TestCase):
    """A tmpfs mount point has to exist in the image.

    Docker creates a missing mount point inside the container's root
    filesystem — which is read-only by default. The container then does
    not start, and the failure looks like a broken image rather than a
    missing directory.
    """

    def test_the_image_creates_every_mount_point(self) -> None:
        dockerfile = _read("Dockerfile.sandbox")
        mkdir_lines = [line for line in dockerfile.splitlines() if "mkdir -p" in line]
        self.assertTrue(
            mkdir_lines,
            "the image creates no directories at all — the tmpfs mount "
            "points would have to be created inside a read-only root "
            "filesystem, and the container would not start",
        )
        created = "\n".join(mkdir_lines)
        for path in REQUIRED_MOUNTPOINTS:
            with self.subTest(path=path):
                self.assertIn(
                    path,
                    created,
                    f"the image does not create {path} — the tmpfs mount "
                    "point would have to be created inside a read-only "
                    "root filesystem",
                )


class TestTheDocumentsMatchTheTree(unittest.TestCase):
    """Documentation is a claim about the code, and it is pinned here."""

    def test_the_configuration_reference_describes_a_tmpfs(self) -> None:
        text = _read("docs/CONFIGURATION.md")
        row = [line for line in text.splitlines() if line.startswith("| `/workspace`")]
        self.assertEqual(
            len(row),
            1,
            "the filesystem layout table must name the workspace exactly "
            "once — two rows mean two facts, and one of them rots",
        )
        self.assertIn("tmpfs", row[0])
        self.assertNotIn(
            "bind-mount",
            row[0],
            "the layout table still says the workspace is a bind-mount",
        )

    def test_the_bind_mount_table_has_no_writable_workspace(self) -> None:
        text = _read("docs/CONFIGURATION.md")
        offenders = [
            line
            for line in text.splitlines()
            if line.startswith("| `/opt/agent-workspace`") and "/workspace" in line and "rw" in line
        ]
        self.assertEqual(
            offenders,
            [],
            "the bind-mount table still promises a writable host "
            "workspace — that is the hole this release closed",
        )

    def test_the_security_model_names_the_ceiling(self) -> None:
        text = _read("docs/SECURITY.md")
        self.assertIn(
            "AGENT_WORKSPACE_SIZE",
            text,
            "the security model does not name the ceiling — the disk-fill "
            "threat has to say what the bound is",
        )
        self.assertNotIn(
            "on a filesystem with a quota",
            text,
            "the security model still tells the operator to put the "
            "workspace on a separate filesystem — the workspace is a "
            "tmpfs now, and that advice describes a hole",
        )

    def test_the_readme_names_the_ceiling(self) -> None:
        text = _read("README.md")
        self.assertIn("AGENT_WORKSPACE_SIZE", text)
        self.assertNotIn(
            "bind-mount `/opt/agent-workspace`",
            text,
            "the README still tells the operator to bind a host directory over the workspace",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
