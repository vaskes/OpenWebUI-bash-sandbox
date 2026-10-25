"""The privilege mode: ``sudo su`` inside the sandbox.

The feature is a **configuration switch**, not a code path: the image
either ships ``sudo`` or it does not, and the container's posture
decides whether the setuid bit can do its job. That makes the tests
here mostly about the *artifacts* — the Dockerfile, the three compose
files and the documentation — because that is where the switch lives.

The failure mode this file exists to prevent: a deployment that is
half-configured. The image has ``sudo`` but the container still runs
with no-new-privileges on, or with an empty capability set. The model
then gets an error it cannot interpret, and the operator goes looking
for a broken sandbox instead of a missing setting.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX_DOCKERFILE = ROOT / "Dockerfile.sandbox"
SECURITY_DOC = ROOT / "docs" / "SECURITY.md"
CONFIGURATION_DOC = ROOT / "docs" / "CONFIGURATION.md"
ENV_EXAMPLE = ROOT / ".env.example"

COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.ci.yml",
    "docker-compose.production.yml",
)

#: The stock posture. These two are what a deployment gets when nobody
#: touches the environment: an empty capability set, and the
#: no-new-privileges flag on.
STOCK_CAP_DROP = "${AGENT_CAP_DROP:-ALL}"
STOCK_SECURITY_OPT = "no-new-privileges:${AGENT_NO_NEW_PRIVILEGES:-true}"


class TestTheImageCarriesSudoOnlyWhenAsked(unittest.TestCase):
    """The image must not ship a setuid-root path by default."""

    @staticmethod
    def _dockerfile() -> str:
        return SANDBOX_DOCKERFILE.read_text(encoding="utf-8")

    def test_the_build_argument_defaults_to_off(self) -> None:
        self.assertIn("ARG AGENT_ALLOW_SUDO=0", self._dockerfile())

    def test_sudo_is_installed_only_under_the_flag(self) -> None:
        """`sudo` in the image means a root shell for whoever runs it.

        The install has to be conditional on the build argument — a
        stock image with ``sudo`` and a NOPASSWD rule would be a
        privilege escalation waiting for a compose file to relax the
        posture.
        """
        text = self._dockerfile()
        self.assertIn('AGENT_ALLOW_SUDO" = "1"', text)
        self.assertIn("echo sudo", text)
        # The package list is guarded: sudo is appended by a shell
        # conditional, not listed unconditionally.
        for line in text.splitlines():
            if "sudo" in line and "echo sudo" in line:
                continue
            if line.strip() in ("sudo", "sudo \\"):
                self.fail(f"sudo is installed unconditionally: {line.strip()!r}")

    def test_the_sudoers_rule_is_written_and_validated(self) -> None:
        """A broken sudoers file breaks sudo for everybody, root included.

        ``visudo -c`` is the check that catches a typo at build time
        instead of at the first command the model runs.
        """
        text = self._dockerfile()
        self.assertIn("/etc/sudoers.d/agent-sudo", text)
        self.assertIn("NOPASSWD:ALL", text)
        self.assertIn("visudo -c", text)

    def test_the_rule_is_bound_to_the_agent_user(self) -> None:
        """The rule names the user, resolved from the uid — not "ALL"."""
        text = self._dockerfile()
        self.assertIn("getent passwd 1000", text)

    def test_sudo_does_not_allocate_its_own_pty(self) -> None:
        """Without ``!use_pty`` the feature is broken, and silently.

        Sudo runs the command in a pseudo-terminal of its own by
        default. When that pty is torn down the terminal is reset, and
        the reset wipes the completion marker the exec protocol depends
        on: the command succeeds, its output is right there in the
        pane, and the exec still times out and reports nothing.

        That failure mode is invisible in a manual test — the shell
        works fine — and it only shows up as "the sandbox hangs on
        sudo". The line is load-bearing.
        """
        text = self._dockerfile()
        self.assertIn("Defaults !use_pty", text)


class TestTheComposeFilesCarryTheSwitch(unittest.TestCase):
    """All three stacks must expose the same knobs, with the same defaults."""

    def _compose(self, name: str) -> str:
        return (ROOT / name).read_text(encoding="utf-8")

    def test_every_stack_passes_the_build_argument(self) -> None:
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                text = self._compose(name)
                self.assertIn('AGENT_ALLOW_SUDO: "${AGENT_ALLOW_SUDO:-0}"', text)

    def test_the_default_posture_does_not_change(self) -> None:
        """The stock deployment must keep the stock posture.

        A knob whose default is not the old value is a silent change of
        the security posture of every deployment that did not opt in.
        """
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                text = self._compose(name)
                self.assertIn(STOCK_CAP_DROP, text)
                self.assertIn(STOCK_SECURITY_OPT, text)

    def test_the_agent_service_is_the_one_that_carries_the_knobs(self) -> None:
        """The proxy never runs model commands, so it keeps the strict posture."""
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                text = self._compose(name)
                agent = text.split("  sandbox-agent:", 1)[1].split("  openapi-proxy:", 1)[0]
                self.assertIn(STOCK_CAP_DROP, agent)
                self.assertIn(STOCK_SECURITY_OPT, agent)


class TestTheDocumentationSaysTheWarning(unittest.TestCase):
    """A dangerous switch that is not labelled is a trap.

    The docs have to carry the caveat, and they have to carry it in the
    places an operator actually reads: the security model, the
    configuration reference and the environment template.
    """

    #: The phrase that makes the caveat. Not a paraphrase: the point is
    #: that the reader sees "private deployments in secured
    #: environments" before they turn anything on.
    CAVEAT = "private deployments in secured environments"

    def _mentions_the_caveat(self, text: str) -> bool:
        return self.CAVEAT in text.lower()

    def test_the_security_model_carries_the_caveat(self) -> None:
        text = SECURITY_DOC.read_text(encoding="utf-8")
        self.assertIn("The privilege mode", text)
        self.assertTrue(
            self._mentions_the_caveat(text),
            "docs/SECURITY.md must say that the privilege mode is for "
            "private deployments in secured environments only",
        )

    def test_the_security_model_explains_every_setting(self) -> None:
        text = SECURITY_DOC.read_text(encoding="utf-8")
        for knob in (
            "AGENT_ALLOW_SUDO",
            "AGENT_NO_NEW_PRIVILEGES",
            "AGENT_CAP_DROP",
            "AGENT_READ_ONLY",
        ):
            with self.subTest(knob=knob):
                self.assertIn(knob, text)

    def test_the_configuration_reference_carries_the_caveat(self) -> None:
        text = CONFIGURATION_DOC.read_text(encoding="utf-8")
        self.assertTrue(
            self._mentions_the_caveat(text),
            "docs/CONFIGURATION.md must carry the same caveat",
        )

    def test_the_environment_template_carries_the_caveat(self) -> None:
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("AGENT_ALLOW_SUDO", text)
        self.assertTrue(
            self._mentions_the_caveat(text),
            ".env.example must carry the same caveat — it is the file an operator edits",
        )

    def test_the_environment_template_carries_the_fourth_setting(self) -> None:
        """The template is where an operator looks for the switches.

        A knob that exists in the compose files but not in .env.example
        is a knob nobody sets.
        """
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("AGENT_READ_ONLY", text)


if __name__ == "__main__":
    unittest.main()
