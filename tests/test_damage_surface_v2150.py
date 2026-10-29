"""Tests for the sandbox's *damage surface* — what an agent can destroy.

An operator gave a privileged agent the task of "shooting itself in the
foot as hard as it can", and the answer it produced is worth writing
down before it is forgotten:

* ``rm -rf /`` and ``dd of=/dev/nvme0n1p4`` were refused by the
  caller's command policy — and the container has no device nodes
  anyway, so the host's disk was never reachable;
* ``sudo rm -rf /etc`` was **not** refused, and that is the real
  maximum damage: it takes the passwd database, the CA bundle and the
  ``/etc/alternatives`` layer with it, and the container cannot be
  repaired from inside afterwards.

The consequence for this repository is not a new guardrail — the
caller's policy is the caller's business — but the set of defaults that
make the *default* deployment harmless, and the fact that they are
defaults and not accidents:

* **no sudo in the image** unless the privilege mode asks for it;
* **a read-only root filesystem**, so even a root command cannot write
  to ``/etc``;
* a **non-root user**;
* **no capabilities**, no privileged container, no docker socket.

Those are measured facts about a running deployment, and a fact that is
not pinned rots: somebody will one day set ``AGENT_READ_ONLY=false``
"to make the build work" and the sandbox stops being a scratchpad.
So they are pinned here, against the compose files and the Dockerfile
that ship them.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _agent_service(compose: str) -> str:
    """The ``sandbox-agent`` service block of a compose file."""
    text = _source(compose)
    match = re.search(r"^  sandbox-agent:\n(.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)", text, re.M | re.S)
    if match is None:
        raise AssertionError(f"{compose} has no sandbox-agent service")
    return match.group(1)


class TestTheDefaultDeploymentHasNoDamageSurface(unittest.TestCase):
    """The defaults are what makes the difference; they are pinned here."""

    COMPOSES = ("docker-compose.yml", "docker-compose.production.yml")

    def test_the_root_filesystem_is_read_only_by_default(self) -> None:
        for compose in self.COMPOSES:
            with self.subTest(compose=compose):
                service = _agent_service(compose)
                self.assertIn(
                    'read_only: "${AGENT_READ_ONLY:-true}"',
                    service,
                    f"{compose}: the agent's root filesystem is not read-only by default — "
                    "a command that runs as root can then rewrite /etc",
                )

    def test_sudo_is_off_by_default(self) -> None:
        for compose in self.COMPOSES:
            with self.subTest(compose=compose):
                service = _agent_service(compose)
                self.assertIn(
                    'AGENT_ALLOW_SUDO: "${AGENT_ALLOW_SUDO:-0}"',
                    service,
                    f"{compose}: the privilege mode is on by default — an agent can become "
                    "root without the operator asking for it",
                )

    def test_the_agent_runs_as_a_non_root_user(self) -> None:
        for compose in self.COMPOSES:
            with self.subTest(compose=compose):
                service = _agent_service(compose)
                self.assertIn(
                    'user: "1000:1000"',
                    service,
                    f"{compose}: the agent does not run as a non-root user",
                )

    def test_no_privileges_and_no_docker_socket(self) -> None:
        for compose in self.COMPOSES:
            with self.subTest(compose=compose):
                service = _agent_service(compose)
                self.assertIn(
                    "cap_drop:",
                    service,
                    f"{compose}: the agent service drops no capabilities",
                )
                self.assertNotIn(
                    "privileged: true",
                    service,
                    f"{compose}: the agent service is a privileged container — that is the "
                    "host, with the host's disks",
                )
                self.assertNotIn(
                    "/var/run/docker.sock",
                    service,
                    f"{compose}: the agent has the docker socket — that is the host's root, "
                    "and no policy in the world can defend it",
                )

    def test_the_image_ships_sudo_only_when_the_privilege_mode_asks(self) -> None:
        """``sudo`` is in the package list only under the ARG.

        The default image has no ``sudo`` binary and no setuid binary
        that could help: a container that cannot become root cannot be
        talked into wiping ``/etc``.
        """
        dockerfile = _source("Dockerfile.sandbox")
        self.assertIn(
            "ARG AGENT_ALLOW_SUDO=0",
            dockerfile,
            "the image's privilege switch does not default to off",
        )
        self.assertIn(
            '$( [ "$AGENT_ALLOW_SUDO" = "1" ] && echo sudo )',
            dockerfile,
            "sudo is in the image's package list unconditionally — the default image ships "
            "a setuid binary the agent does not need",
        )
        self.assertIn(
            'RUN if [ "$AGENT_ALLOW_SUDO" = "1" ]; then',
            dockerfile,
            "the sudoers rule is written unconditionally",
        )


if __name__ == "__main__":
    unittest.main()
