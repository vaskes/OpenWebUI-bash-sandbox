"""The shipped configuration names no host of its own.

The stack used to publish its ports on one specific machine's LAN address,
hardcoded into ``docker-compose.yml`` and copied into the configuration
reference. Two things were wrong with that, and only one of them was the
bug report:

* the Quick start did not work on anybody else's host — the address did
  not exist there, so the container refused to start;
* the address was *somebody's* address. A public repository that carries
  one operator's private LAN address is a small leak of somebody's
  network, and it survives long after that address stops being theirs.

The rule this file pins: **the deployable configuration takes the address
from the operator, and never names a host by its IPv4 address.**

What counts as "the deployable configuration": the compose files, the
environment template, the systemd unit and the documentation a person
reads before deploying. What does *not* count: the changelog (its job is
to record what was removed, by name), the tests (a test has to be able
to say what it forbids) and the mutation scripts (a mutation has to be
able to put it back for a moment).

A CIDR is not a host address: ``10.0.0.0/8`` in an SSH allowlist is a
*pattern* the operator matches against, not a machine this stack talks
to. That distinction is the difference between a rule that can be
obeyed and a rule that has to be broken.
"""

from __future__ import annotations

import ipaddress
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The private networks. A host address from one of them names a machine.
#: The loopback is deliberately *not* here: ``127.0.0.1`` is the default
#: the stack publishes on, and it names nothing.
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)

#: The files that make up the deployable configuration. Globs, so a new
#: compose file is covered by the rule from the moment it is written.
SCAN_GLOBS = (
    "docker-compose*.yml",
    ".env.example",
    "agent-stack.service*",
    "README.md",
    "docs/*.md",
)

#: Files in the scan that are allowed to name a host: the changelog
#: records the removal of such an address, by name.
SKIP_FILES = frozenset({"CHANGELOG.md"})

#: An IPv4 literal. The address itself is checked against the private
#: networks above; this only recognises the shape.
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")


def _scanned_files() -> list[Path]:
    """Every file of the deployable configuration."""
    found: list[Path] = []
    for pattern in SCAN_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            if not path.is_file() or path.name in SKIP_FILES:
                continue
            found.append(path)
    return found


def _is_private_host(address: str) -> bool:
    """Is this address in a private network? (The loopback is not.)"""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:  # pragma: no cover - the regex only matches digits
        return False
    return any(ip in network for network in PRIVATE_NETWORKS)


class TestTheConfigurationNamesNoHost(unittest.TestCase):
    """No private address of a machine in the shipped configuration."""

    def test_the_scan_covers_something(self) -> None:
        """A check that scans an empty collection proves nothing."""
        files = _scanned_files()
        self.assertGreaterEqual(
            len(files),
            5,
            f"the scan found {len(files)} files — the rule is not looking at anything: {files}",
        )
        names = {p.name for p in files}
        for expected in ("docker-compose.yml", "README.md", ".env.example"):
            self.assertIn(expected, names, f"{expected} is not part of the scan any more")

    def test_no_file_names_a_host_by_its_address(self) -> None:
        """The rule itself, file by file."""
        offences: list[str] = []
        for path in _scanned_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), start=1):
                for match in _IPV4.finditer(line):
                    address = match.group(0)
                    # A CIDR is a pattern, not a machine.
                    if line[match.end() : match.end() + 1] == "/":
                        continue
                    if not _is_private_host(address):
                        continue
                    offences.append(
                        f"{path.relative_to(ROOT)}:{number}: {address} — "
                        "the address of a machine does not belong in the shipped configuration"
                    )
        self.assertEqual(
            offences,
            [],
            "the shipped configuration names a host by its address:\n  " + "\n  ".join(offences),
        )

    def test_the_published_ports_take_the_address_from_the_operator(self) -> None:
        """The positive claim: the bind address is a parameter, not a constant."""
        body = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        mappings = re.findall(r'^\s*-\s*"([^"]*:\d+:\d+)"\s*$', body, re.M)
        self.assertTrue(mappings, "no published port mapping was found in docker-compose.yml")
        for mapping in mappings:
            self.assertIn(
                "${SANDBOX_BIND_HOST:-",
                mapping,
                f"the port mapping {mapping!r} hardcodes the address the operator should choose",
            )

    def test_the_default_bind_address_is_the_loopback(self) -> None:
        """The default publishes nothing to the network."""
        body = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        defaults = re.findall(r"\$\{SANDBOX_BIND_HOST:-([^}]*)\}", body)
        self.assertTrue(defaults, "the bind address has no default")
        for value in defaults:
            self.assertEqual(value, "127.0.0.1", "the default bind address must be the loopback")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
