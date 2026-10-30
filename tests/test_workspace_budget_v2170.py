"""The workspace is on disk with a ceiling, and the root can really write.

Two decisions shape this file, and both of them are the operator's.

**The workspace goes back to disk, with a hard ceiling.** In v2.16.0
the workspace became a tmpfs inside the container: RAM, capped, gone
when the container is replaced. That was the wrong trade for a
deployment whose whole point is a working shell — the workspace was
empty on every restart, and the session registry with it. The workspace
is now a **named volume** (``owbs-workspace``) whose driver binds a
host path, and that host path is the mount point of an ext4 filesystem
living in a file of ``AGENT_WORKSPACE_SIZE``. The file is sparse (it
costs what the workspace holds), the ceiling is exact (the filesystem
runs out of space, the write fails with ``ENOSPC``, the door answers
507), and the workspace survives a container recreate.

The ceiling cannot come from Docker. There is no per-volume quota:
compose has no such option, and ``--storage-opt size`` only ever
worked with the devicemapper storage driver, which Docker no longer
ships (this host runs overlayfs). A named volume is a directory on the
host's disk by construction, so the ceiling has to come from the
filesystem — which is what ``tools/setup-workspace.sh`` makes.

**A root that cannot write is a half-root.** The privilege mode grants
sudo; a root whose filesystem is read-only cannot install anything,
and the model sees "Read-only file system" and calls the sandbox
broken. So the rule is now explicit: *where sudo is on, the root
filesystem has to be writable*. The default deployment still has no
sudo and a read-only root — that is the scratchpad. The rule is
enforced by ``tools/check-posture.sh``, which refuses a half-root.

What is pinned here:

* the workspace is a volume, not a tmpfs and not a raw host path;
* the volume binds a host path, and the ceiling comes from a
  filesystem that a script in the tree actually makes;
* the ceiling has to be able to hold what the file door accepts;
* the tmpfs budget still fits the memory ceiling (the workspace left
  the RAM budget, so the budget shrank);
* the stock deployment has no sudo and a read-only root;
* a deployment with sudo may not keep the root read-only — and that
  check is *run*, not read;
* the documents say what the tree does.

The numbers are read out of the compose files rather than written here,
so a new stack gets the same checks without anybody remembering to add
them: a list of facts that lives in the tree is checked where it lives.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_config import parse_size

ROOT = Path(__file__).resolve().parent.parent

#: A fake `docker` for the volume check.
#:
#: The check asks docker two things: which volumes carry a name label
#: (and, since the host can carry more than one deployment, which
#: project they belong to), and what each of them binds. The fake
#: answers both, and it answers them the way docker does -- the
#: filters are applied, not ignored -- so the scoping of the check is
#: part of what the tripwire pins.
FAKE_DOCKER = """#!/usr/bin/env python3
import os
import sys

#: One fake volume per line: name, project, device (empty for a plain
#: local volume, which is a directory in Docker's own directory).
volumes = []
for line in os.environ.get("OWBS_FAKE_VOLUMES", "").splitlines():
    parts = line.split()
    if len(parts) >= 2:
        volumes.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))

args = sys.argv[1:]


def labels() -> dict:
    out = {}
    for arg in args:
        if arg.startswith("label="):
            key, _, value = arg[len("label="):].partition("=")
            out[key] = value
    return out


if args[:2] == ["volume", "ls"]:
    wanted = labels()
    for name, project, _device in volumes:
        if wanted.get("com.docker.compose.volume") not in (None, "owbs-workspace"):
            continue
        if "com.docker.compose.project" in wanted:
            if project != wanted["com.docker.compose.project"]:
                continue
        print(name)
    sys.exit(0)

if args[:2] == ["volume", "inspect"]:
    name = args[2] if len(args) > 2 else ""
    for volume_name, _project, device in volumes:
        if volume_name == name:
            print(device)
            sys.exit(0)
    sys.exit(1)

sys.exit(0)
"""

#: Every stack this repository ships. The CI stack counts: it is a
#: deployment like any other, and a rule that only the two "real" stacks
#: follow is a rule with a hole in it.
COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.ci.yml",
    "docker-compose.production.yml",
)

#: The name of the workspace volume. One name, one place: the three
#: stacks and the setup script all talk about the same workspace.
WORKSPACE_VOLUME = "owbs-workspace"

#: The tmpfs mount points the agent image has to contain. A tmpfs mount
#: point that is not in the image has to be created by Docker inside the
#: container's root filesystem — which is read-only by default — and
#: that is a container that does not start.
REQUIRED_MOUNTPOINTS = ("/workspace", "/var/cache/apt", "/home/agent/.cache/pip")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _code_lines(text: str) -> list[str]:
    """The lines that are code, not commentary.

    A check that looks for a word finds it in the comment above the
    command as easily as in the command itself, and a check that a
    comment can satisfy is a check that cannot go red. The comment
    stays a comment; the fact is looked for where it lives.
    """
    return [raw for raw in text.splitlines() if raw.strip() and not raw.lstrip().startswith("#")]


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


def _top_level_block(text: str, key: str) -> str:
    """The body of a top-level mapping (``volumes:``).

    A top-level key sits at indent zero, so the block ends at the next
    line that starts at indent zero.
    """
    out: list[str] = []
    inside = False
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if len(raw) - len(raw.lstrip()) == 0:
            inside = raw.strip() == f"{key}:"
            continue
        if inside:
            out.append(raw)
    return "\n".join(out)


def _named_block(text: str, name: str) -> str:
    """The body of a named entry of a mapping (``owbs-workspace:``)."""
    out: list[str] = []
    inside = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if len(raw) - len(raw.lstrip()) == 2 and stripped.endswith(":"):
            inside = stripped == f"{name}:"
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
    """The default of ``NAME=${NAME:-default}`` in a service's environment.

    The name may be followed by ``=`` (a list entry) or by ``: "`` (a
    mapping key), and both spellings are in use in these files.
    """
    pattern = rf"{re.escape(name)}[:=]\s*\"?\$\{{{re.escape(name)}:-([^}}]+)\}}"
    match = re.search(pattern, block)
    return match.group(1) if match else None


def _scalar(block: str, key: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(key)}:\s*(\S+)\s*$", block, re.M)
    return match.group(1) if match else None


def _split_outside_braces(text: str, separator: str = ":") -> list[str]:
    """Split on a separator, but not inside a ``${...}`` interpolation.

    A mount option may carry a default: ``size=${AGENT_WORKSPACE_SIZE:-1g}``.
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


def _default_of(value: str) -> str:
    """The default of a ``${VAR:-default}`` wrapper, or the value itself."""
    match = re.search(r":-([^}]+)\}", value)
    return match.group(1) if match else value


class TestTheWorkspaceIsOnDiskWithACeiling(unittest.TestCase):
    """The workspace is a volume over a filesystem that has a size."""

    def test_the_workspace_is_a_volume_and_not_a_tmpfs(self) -> None:
        """A tmpfs is RAM, and it is gone when the container is replaced.

        The workspace has to survive a recreate: the files the model
        wrote, and the session registry with them.
        """
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                block = _service_block(_read(name), "sandbox-agent")
                targets = [target for _source, target, _mode in _volume_entries(block)]
                self.assertIn(
                    "/workspace",
                    targets,
                    "the workspace is not mounted at all -- the agent's "
                    "writable surface has to exist",
                )
                sources = {
                    source
                    for source, target, _mode in _volume_entries(block)
                    if target == "/workspace"
                }
                self.assertEqual(
                    sources,
                    {WORKSPACE_VOLUME},
                    "the workspace is not the named volume -- it has to be "
                    "one, because a raw host path is an unbounded "
                    "directory on the operator's disk, and a tmpfs is RAM "
                    "that vanishes with the container",
                )
                tmpfs_paths = [path for path, _options in _tmpfs_entries(block)]
                self.assertNotIn(
                    "/workspace",
                    tmpfs_paths,
                    "the workspace is a tmpfs again -- it is RAM, and it "
                    "does not survive a container recreate",
                )

    def test_the_volume_binds_a_host_path(self) -> None:
        """The volume is named, but it lives on the host.

        A named volume whose driver binds a host path is how a mount
        point reaches the container. The path has to be absolute: Docker
        resolves a relative ``device`` against the daemon's working
        directory, not against the compose file.
        """
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                text = _read(name)
                definition = _named_block(_top_level_block(text, "volumes"), WORKSPACE_VOLUME)
                self.assertTrue(
                    definition,
                    f"{name}: there is no top-level volume named {WORKSPACE_VOLUME} -- the "
                    "workspace has to be declared, or compose mounts it from "
                    "/var/lib/docker/volumes, which is a directory on the "
                    "host's disk with no ceiling on it",
                )
                self.assertEqual(
                    _scalar(definition, "driver"),
                    "local",
                    f"{name}: the workspace volume is not a local volume",
                )
                device = _scalar(definition, "device")
                self.assertIsNotNone(device, f"{name}: the volume binds no device")
                default = _default_of(device)
                self.assertTrue(
                    default.startswith("/"),
                    f"{name}: the workspace device '{default}' is not absolute -- Docker "
                    "resolves a relative path against the daemon's working "
                    "directory, not against the compose file",
                )
                self.assertIn(
                    "AGENT_WORKSPACE_PATH",
                    device,
                    f"{name}: the workspace path is not the knob -- it has to be one, so "
                    "the ceiling and the place the workspace lives can be "
                    "decided by the operator",
                )

    def test_the_ceiling_comes_from_a_filesystem_somebody_makes(self) -> None:
        """Docker cannot cap a volume, so a script in the tree has to.

        The ceiling is the size of an ext4 filesystem in a file, and
        the script that makes it is what turns the promise into a
        fact. This checks the script's shape; the CI runs it, which is
        what proves the shape works.
        """
        script = ROOT / "tools" / "setup-workspace.sh"
        self.assertTrue(script.exists(), "tools/setup-workspace.sh is missing")
        # The commands, not the words: the script mentions truncate and
        # mkfs.ext4 in its comments and in the list of tools it checks
        # for, and a check that is satisfied by either of those is a
        # check that a mutation cannot turn red.
        code = "\n".join(_code_lines(script.read_text(encoding="utf-8")))
        self.assertRegex(
            code,
            r"(?m)^\s*truncate\s+-s\s+",
            "the setup script does not make a file of the ceiling -- without "
            "the file there is no filesystem and no ceiling",
        )
        self.assertRegex(
            code,
            r"(?m)^\s*mkfs\.ext4\s+",
            "the setup script does not make a filesystem -- a file on the "
            "host's disk is a directory with a size, not a filesystem that "
            "runs out of space",
        )
        # The fresh mount, and not any mount: the script also remounts
        # an older workspace with the options, and a check that is
        # satisfied by the remount says nothing about the mount that
        # makes a new workspace.
        self.assertRegex(
            code,
            r'(?m)^\s*mount\s+-o\s+"\$\{?WORKSPACE_MOUNT_OPTS\}?"\s+"\$\{?IMAGE',
            "the setup script does not mount the filesystem with the "
            "options it names -- the volume binds a mount point, and a "
            "path that is not a mount point is just a directory",
        )

    def test_the_workspace_is_mounted_without_the_setuid_bit(self) -> None:
        """The workspace is a directory any host user can reach.

        In the privilege mode the agent can become uid 0 inside the
        container, and without a user namespace that is uid 0 on the
        host. So a binary dropped in the workspace with the setuid bit
        set is a setuid-root file that lives on the host's disk, and
        whoever runs it gets the host's root. The tmpfs the workspace
        used to be carried ``nosuid`` and ``nodev``; the filesystem on
        disk has to carry them too.
        """
        script = ROOT / "tools" / "setup-workspace.sh"
        code = "\n".join(_code_lines(script.read_text(encoding="utf-8")))
        match = re.search(r'WORKSPACE_MOUNT_OPTS="([^"]+)"', code)
        self.assertIsNotNone(
            match,
            "the setup script does not name the options the workspace is "
            "mounted with -- the options are the boundary, and a boundary "
            "that is not written down is not one",
        )
        options = [option.strip() for option in match.group(1).split(",")]
        for flag in ("nosuid", "nodev"):
            self.assertIn(
                flag,
                options,
                f"the workspace is mounted without {flag}: the setuid bit "
                "and the device files in a directory any host user can "
                "reach are the host's problem, not the sandbox's",
            )

    def test_the_check_accepts_a_mount_with_the_flags_and_refuses_one_without(self) -> None:
        """The comparison has to work on the options as the mount gives them.

        The shape of the check is not the check. A version of this
        function that compared "rwnosuidnodev" against "nosuid" has the
        right shape and refuses every mount there is -- which is how it
        was found: the deployment stopped with "still mounted without
        nosuid" on a mount that carried both. So the function is run,
        with a mount table of its own.
        """
        script = ROOT / "tools" / "setup-workspace.sh"
        for options, expected in (
            ("rw,nosuid,nodev,relatime", 0),
            ("rw,relatime", 1),
            ("rw,nosuid,relatime", 1),
            ("rw,nodev,relatime", 1),
            # The separators are the token boundaries: an option that
            # merely starts with the same letters is not that option.
            ("rw,nosuidfoo,nodev,relatime", 1),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                mountpoint = Path(tmp) / "workspace"
                mountpoint.mkdir()
                # The mode is part of the workspace's contract, and the
                # check refuses a workspace that does not carry it --
                # so the mount point in the test has to carry it too.
                mountpoint.chmod(0o1777)
                image = Path(tmp) / "workspace.img"
                image.write_bytes(b"x" * 1024)
                bindir = Path(tmp) / "bin"
                bindir.mkdir()
                self._fake_mount_tools(bindir, options, image, mountpoint)
                result = subprocess.run(
                    [
                        "bash",
                        str(script),
                        "--check",
                        str(mountpoint),
                        "1k",
                    ],
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
                        "MOUNTPOINT_FAKE": str(mountpoint),
                        "OPTIONS_FAKE": options,
                        "IMAGE_FAKE": str(image),
                    },
                )
                self.assertEqual(
                    result.returncode,
                    expected,
                    f"the check on the options {options!r} answered "
                    f"{result.returncode}, expected {expected}: "
                    f"{result.stdout}{result.stderr}",
                )

    @staticmethod
    def _fake_mount_tools(bindir: Path, options: str, image: Path, mountpoint: Path) -> None:
        """A mount table of our own, so the script can be run without root."""
        findmnt = bindir / "findmnt"
        findmnt.write_text(
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            '    *"-no TARGET"*) echo "$MOUNTPOINT_FAKE" ;;\n'
            '    *"-no SOURCE"*) echo "/dev/loop999" ;;\n'
            '    *"-no OPTIONS"*) echo "$OPTIONS_FAKE" ;;\n'
            '    *) echo "" ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        findmnt.chmod(0o755)
        losetup = bindir / "losetup"
        losetup.write_text(
            '#!/usr/bin/env bash\necho "/dev/loop999 $IMAGE_FAKE"\n',
            encoding="utf-8",
        )
        losetup.chmod(0o755)
        # The answers travel in the environment the script is run with;
        # nothing is left behind in the process that runs the tests.

    def test_the_check_reads_the_live_mount_and_not_the_script(self) -> None:
        """A rule about the filesystem is only true of the filesystem.

        The script may have been changed since the mount was made --
        that is exactly what happened when the workspace moved from a
        tmpfs to a filesystem on the disk. So the check reads the mount
        that is there, and compares the options as tokens: "nosuidfoo"
        is not "nosuid".
        """
        script = ROOT / "tools" / "setup-workspace.sh"
        code = "\n".join(_code_lines(script.read_text(encoding="utf-8")))
        self.assertRegex(
            code,
            r"findmnt\s+-no\s+OPTIONS",
            "the script does not read the options the workspace is mounted "
            "with -- the script's own text says what it would mount, not "
            "what is mounted",
        )
        self.assertRegex(
            code,
            r"WORKSPACE_REQUIRED_OPTS",
            "the script does not name the options the mount has to carry",
        )

    def test_the_workspace_is_writable_by_the_root_the_container_hands_out(self) -> None:
        """The scratchpad has to be writable by the root as well.

        The workspace's root directory was mode 755, owned by the
        agent's user. That is fine for the agent -- and it is not fine
        for the root the privilege mode hands out: a container that
        drops every capability has no CAP_DAC_OVERRIDE, so uid 0 cannot
        write into a directory it does not own. The CI found it: the
        deploy job's file-door step asked the container's root to create
        a file, and the file was not there afterwards.

        The mode is 1777, like the tmpfs this workspace replaced.
        """
        code = "\n".join(
            _code_lines((ROOT / "tools" / "setup-workspace.sh").read_text(encoding="utf-8"))
        )
        self.assertRegex(
            code,
            r"(?m)^\s*WORKSPACE_MODE=1777\s*$",
            "the workspace's mode is not 1777 -- the agent's user writes there, "
            "and so does the root the privilege mode hands out, and a root "
            "without capabilities cannot write into a directory owned by "
            "somebody else",
        )
        self.assertRegex(
            code,
            r'(?m)^\s*chmod\s+"\$WORKSPACE_MODE"\s+"\$MOUNTPOINT"',
            "the setup script never sets the mode of the workspace -- the "
            "filesystem's root directory keeps the mode mkfs gave it, and "
            "that mode is not the one the scratchpad needs",
        )

    def test_a_plain_directory_is_not_a_mounted_workspace(self) -> None:
        """The workspace's directory lives on the host's partition.

        findmnt answers with the nearest mount that CONTAINS the path,
        so a plain directory inside a mounted filesystem looks mounted
        too -- and the setup script refused the production host's
        workspace for exactly that reason ("already mounted from
        /dev/nvme0n1p4"), when it was a directory like any other.

        The check needs no root and no loop device, so it can be run
        here: a directory that is not a mount point is a directory that
        is not mounted.
        """
        script = ROOT / "tools" / "setup-workspace.sh"
        with tempfile.TemporaryDirectory() as tmp:
            plain = pathlib.Path(tmp) / "not-mounted"
            plain.mkdir()
            result = subprocess.run(
                ["bash", str(script), "--check", str(plain), "1g"],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(
            result.returncode,
            0,
            "the check accepted a directory that is not mounted",
        )
        self.assertIn(
            "not mounted",
            result.stderr,
            "a plain directory was taken for a mounted workspace -- the "
            "setup script would refuse to make the workspace on the host "
            "that has one",
        )

    def test_the_workspace_volume_has_to_be_the_workspace(self) -> None:
        """A volume that is not the workspace has no ceiling.

        Docker does not change a volume's options once the volume
        exists: a volume left over from an earlier stack keeps the
        driver it was made with, and a plain local volume is a
        directory on the host's partition with nothing to stop it
        growing. That is exactly the hole the filesystem closes, so the
        volume has to be the workspace -- and the check that says so is
        *run* here, against a fake docker that answers like one.
        """
        script = ROOT / "tools" / "check-workspace-volume.sh"
        self.assertTrue(script.exists(), "tools/check-workspace-volume.sh is missing")
        self.assertTrue(
            os.access(script, os.X_OK),
            "tools/check-workspace-volume.sh is not executable -- a check that "
            "cannot be run is a check that does not happen",
        )

        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = pathlib.Path(tmp)
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(FAKE_DOCKER, encoding="utf-8")
            fake_docker.chmod(0o755)

            def check(volumes: str, *args: str) -> subprocess.CompletedProcess:
                env = {
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "OWBS_FAKE_VOLUMES": volumes,
                }
                return subprocess.run(
                    ["bash", str(script), *args],
                    capture_output=True,
                    text=True,
                    check=False,
                    env=env,
                )

            # The volumes of OUR project: one is the workspace, one is a
            # plain directory in Docker's own directory. Both carry the
            # same logical name.
            ours = "ours-good ours /opt/owbs-workspace\nours-plain ours \n"
            # A volume of another deployment on the same host: not ours.
            theirs = "theirs-plain theirs \n"

            good = check("ours-good ours /opt/owbs-workspace\n", "/opt/owbs-workspace", "ours")
            self.assertEqual(
                good.returncode,
                0,
                f"the check refused the workspace volume: {good.stdout}{good.stderr}",
            )

            plain = check(ours, "/opt/owbs-workspace", "ours")
            self.assertNotEqual(
                plain.returncode,
                0,
                "a volume that is not the workspace passed the check -- that "
                "volume is a plain directory on the host's partition, with no "
                "ceiling on it",
            )
            self.assertIn(
                "ours-plain",
                plain.stderr,
                "the check does not name the volume that is not the workspace",
            )

            # Another deployment's volume is not ours, and the check must
            # not reach for it: the host can carry more than one.
            foreign = check(theirs, "/opt/owbs-workspace", "ours")
            self.assertEqual(
                foreign.returncode,
                0,
                f"the check refused another deployment's volume: {foreign.stdout}{foreign.stderr}",
            )

    def test_the_deployment_runs_the_setup_script(self) -> None:
        """A script nobody runs is a promise nobody checks.

        The CI has to make the workspace before it starts the stack:
        without the script, the volume binds an ordinary directory and
        the ceiling is a word.
        """
        workflow = _read(".github/workflows/ci.yml")
        # A run step, not a mention: the workflow explains in a comment
        # which script it runs, and a comment is not a run.
        self.assertRegex(
            workflow,
            r"(?m)^\s*run:.*tools/setup-workspace\.sh",
            "the CI never makes the workspace -- the stack it starts has "
            "no ceiling, and the CI would be testing something the "
            "operator's deployment is not",
        )

    def test_the_ceiling_has_to_hold_what_the_door_accepts(self) -> None:
        """The file door's policy is the ceiling's lower bound.

        The door accepts a file up to ``AGENT_BINARY_LIMIT`` and writes
        it into the workspace. A ceiling below the policy refuses a
        file the door already promised to accept — and the refusal
        comes from the wrong side, as "no space" for a file that was in
        range. The script compares the two, where the ceiling is
        applied.
        """
        # The assignment, not the mention: the script names the policy in
        # its error message as well, and a message is not a reading.
        code = "\n".join(
            _code_lines((ROOT / "tools" / "setup-workspace.sh").read_text(encoding="utf-8"))
        )
        self.assertRegex(
            code,
            r"(?m)^\s*POLICY=.*AGENT_BINARY_LIMIT",
            "the setup script does not know the file policy -- a ceiling "
            "smaller than the policy is a workspace that cannot hold a "
            "file the door accepts",
        )

    def test_the_tmpfs_budget_fits_the_memory_ceiling(self) -> None:
        """A tmpfs is RAM: the sizes are charged against ``mem_limit``.

        The workspace left the RAM budget, so what is left has to fit:
        if the mounts add up to more than the container's memory limit,
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


class TestTheRootCanWriteWhereItIsRoot(unittest.TestCase):
    """Where sudo is on, the root has to be able to write."""

    def test_the_stock_deployment_has_no_sudo_and_a_read_only_root(self) -> None:
        """The default deployment is a scratchpad, and that stays true."""
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
                self.assertEqual(
                    _env_default(block, "AGENT_ALLOW_SUDO"),
                    "0",
                    "the privilege mode is on by default — an agent can "
                    "become root without the operator asking for it",
                )

    def test_a_deployment_with_sudo_may_not_keep_the_root_read_only(self) -> None:
        """The rule, and the check that enforces it.

        The check is *run* here, not read: a rule that only exists as a
        sentence in a compose comment is a rule that rots the first
        time somebody edits the comment.
        """
        script = ROOT / "tools" / "check-posture.sh"
        self.assertTrue(script.exists(), "tools/check-posture.sh is missing")

        def verdict(allow_sudo: str, read_only: str) -> int:
            result = subprocess.run(
                ["bash", str(script)],
                capture_output=True,
                text=True,
                check=False,
                env={
                    "PATH": "/usr/bin:/bin:/usr/local/bin",
                    "AGENT_ALLOW_SUDO": allow_sudo,
                    "AGENT_READ_ONLY": read_only,
                },
            )
            return result.returncode

        self.assertNotEqual(
            verdict("1", "true"),
            0,
            "a deployment that grants sudo and keeps the root filesystem "
            "read-only passed the posture check — that is a half-root",
        )
        self.assertNotEqual(
            verdict("1", ""),
            0,
            "a deployment that grants sudo and leaves the root filesystem "
            "undecided passed the posture check — the default is "
            "read-only, so it is a half-root",
        )
        self.assertEqual(
            verdict("1", "false"),
            0,
            "the posture check refused a real root — sudo on and a "
            "writable root is the privilege mode, and it has to pass",
        )
        self.assertEqual(
            verdict("0", "true"),
            0,
            "the posture check refused the stock deployment — no sudo and "
            "a read-only root is the default and it has to pass",
        )


class TestTheImageHasTheMountPoints(unittest.TestCase):
    """A mount point has to exist in the image.

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
            "the image creates no directories at all — the mount points "
            "would have to be created inside a read-only root filesystem, "
            "and the container would not start",
        )
        created = "\n".join(mkdir_lines)
        for path in REQUIRED_MOUNTPOINTS:
            with self.subTest(path=path):
                self.assertIn(
                    path,
                    created,
                    f"the image does not create {path} — the mount point "
                    "would have to be created inside a read-only root "
                    "filesystem",
                )


class TestTheDocumentsMatchTheTree(unittest.TestCase):
    """Documentation is a claim about the code, and it is pinned here."""

    def test_the_configuration_reference_describes_the_workspace(self) -> None:
        text = _read("docs/CONFIGURATION.md")
        row = [line for line in text.splitlines() if line.startswith("| `/workspace`")]
        self.assertEqual(
            len(row),
            1,
            "the filesystem layout table must name the workspace exactly "
            "once — two rows mean two facts, and one of them rots",
        )
        self.assertIn("volume", row[0].lower())
        self.assertNotIn(
            "tmpfs",
            row[0],
            "the layout table still says the workspace is a tmpfs — it is "
            "a volume over a filesystem with a ceiling now",
        )

    def test_the_configuration_reference_names_the_workspace_path(self) -> None:
        text = _read("docs/CONFIGURATION.md")
        self.assertIn(
            "AGENT_WORKSPACE_PATH",
            text,
            "the configuration reference does not name the knob that says "
            "where the workspace lives — the ceiling is not enough, the "
            "place matters too",
        )

    def test_the_security_model_names_the_ceiling_and_its_price(self) -> None:
        text = _read("docs/SECURITY.md")
        self.assertIn(
            "AGENT_WORKSPACE_SIZE",
            text,
            "the security model does not name the ceiling — the disk-fill "
            "threat has to say what the bound is",
        )
        self.assertIn(
            "AGENT_WORKSPACE_PATH",
            text,
            "the security model does not name where the workspace lives",
        )

    def test_the_readme_names_the_ceiling(self) -> None:
        text = _read("README.md")
        self.assertIn("AGENT_WORKSPACE_SIZE", text)
        self.assertNotIn(
            "bind-mount `/opt/agent-workspace`",
            text,
            "the README still tells the operator to bind a host directory over the workspace",
        )

    def test_the_env_example_names_the_workspace_path(self) -> None:
        text = _read(".env.example")
        self.assertIn("AGENT_WORKSPACE_PATH", text)
        self.assertIn("AGENT_WORKSPACE_SIZE", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
