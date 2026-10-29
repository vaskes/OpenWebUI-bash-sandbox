#!/usr/bin/env python3
"""The model's shell must not inherit the agent's credentials.

**What was wrong.** The agent needs its own token to authenticate the
calls that arrive at its doors. The model's shell inherits the agent's
environment through the tmux server: tmux starts its server from the
first client, and the server's environment is what every session's shell
gets.

Measured on 2026-10-29, on the production deployment: a session's
``env`` printed ``AGENT_TOKEN`` in plaintext, ``tmux show-environment
-g`` printed it too, and the sandbox has unrestricted outbound network
access — so a token in the model's hands is a token that can leave the
host. The token was the same value as the operator's stack token, which
made the leak the operator's own credential.

Two things are pinned here:

* the tmux client — and therefore the tmux server — gets an environment
  without the credentials, so a session's shell never sees them;
* the deployment can give the agent its **own** inbound token, so the
  operator's stack token does not have to live inside the container the
  model runs in. The default is unchanged: one token for the whole
  stack.

The first is checked by running tmux for real, not by reading the
source: a server is started with the agent's own environment builder, a
session is created in it, and the pane's ``env`` output is inspected.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOKEN = "test-agent-token-xyz"


def _has_tmux() -> bool:
    """Is tmux on this machine?  The check that drives a real tmux
    session is skipped when it is not — a missing tool is an
    environment, not a defect."""
    return shutil.which("tmux") is not None


requires_tmux = unittest.skipUnless(_has_tmux(), "tmux is not installed")


def _agent_with_a_token() -> object:
    """Import the agent with a token in its environment, and return it."""
    os.environ["AGENT_TOKEN"] = TOKEN
    sys.modules.pop("agent_api", None)
    import agent_api  # noqa: PLC0415

    return agent_api


class TestTheTmuxEnvironmentHasNoCredentials(unittest.TestCase):
    """The environment tmux hands to the model must be clean."""

    def test_the_environment_builder_drops_the_credentials(self) -> None:
        agent_api = _agent_with_a_token()
        env = agent_api._tmux_environment()
        self.assertNotIn(
            "AGENT_TOKEN",
            env,
            "the tmux client gets the token — the tmux server inherits it, "
            "and so does every session's shell",
        )

    def test_the_environment_builder_keeps_the_ordinary_variables(self) -> None:
        agent_api = _agent_with_a_token()
        env = agent_api._tmux_environment()
        for name in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"):
            with self.subTest(name=name):
                if name in os.environ:
                    self.assertIn(name, env, f"tmux lost {name} — the shell would break")

    def test_every_credential_shaped_name_is_dropped(self) -> None:
        agent_api = _agent_with_a_token()
        os.environ["A_FAKE_TOKEN"] = "x"
        os.environ["A_FAKE_SECRET"] = "x"
        os.environ["A_FAKE_PASSWORD"] = "x"
        try:
            env = agent_api._tmux_environment()
            for name in ("A_FAKE_TOKEN", "A_FAKE_SECRET", "A_FAKE_PASSWORD"):
                with self.subTest(name=name):
                    self.assertNotIn(name, env)
        finally:
            for name in ("A_FAKE_TOKEN", "A_FAKE_SECRET", "A_FAKE_PASSWORD"):
                os.environ.pop(name, None)


@requires_tmux
class TestASessionsShellDoesNotSeeTheToken(unittest.TestCase):
    """The real thing: a tmux server, a real session, a real `env`."""

    def setUp(self) -> None:
        self.agent_api = _agent_with_a_token()
        self.tmpdir = tempfile.mkdtemp(prefix="owbs-token-test-")
        self.name = f"agent_token_probe_{uuid.uuid4().hex[:8]}"
        # A tmux server of its own, so the probe cannot touch another
        # server's sessions.
        self.tmux_tmpdir = tempfile.mkdtemp(prefix="owbs-tmux-")
        self._previous_tmpdir = os.environ.get("TMUX_TMPDIR")
        os.environ["TMUX_TMPDIR"] = self.tmux_tmpdir

    def tearDown(self) -> None:
        subprocess.run(
            ["tmux", "kill-server"],
            capture_output=True,
            text=True,
            check=False,
            env=self.agent_api._tmux_environment(),
        )
        if self._previous_tmpdir is None:
            os.environ.pop("TMUX_TMPDIR", None)
        else:
            os.environ["TMUX_TMPDIR"] = self._previous_tmpdir

    def _tmux(self, *args: str) -> str:
        """Run tmux **the way the agent runs it** — its own function.

        A hand-rolled subprocess would measure the test's own idea of the
        environment, not the agent's: the agent's `_tmux` is where the
        environment is chosen, so that is what has to be exercised.
        """
        import asyncio  # noqa: PLC0415

        rc, out, err = asyncio.run(self.agent_api._tmux(*args))
        return out if rc == 0 else f"{out}{err}"

    def _pane(self, name: str) -> str:
        return self._tmux("capture-pane", "-t", name, "-p")

    def _wait_for(self, name: str, needle: str, tries: int = 40) -> str:
        import time  # noqa: PLC0415

        pane = ""
        for _ in range(tries):
            pane = self._pane(name)
            if needle in pane:
                break
            time.sleep(0.1)
        return pane

    def test_the_server_environment_has_no_token(self) -> None:
        self._tmux("new-session", "-d", "-s", self.name)
        shown = self._tmux("show-environment", "-g")
        self.assertNotIn(
            "AGENT_TOKEN",
            shown,
            "the tmux server's own environment carries the token — every "
            "session's shell inherits it",
        )

    def test_a_sessions_shell_does_not_see_the_token(self) -> None:
        self._tmux("new-session", "-d", "-s", self.name)
        self._tmux("send-keys", "-t", self.name, "-l", "env")
        self._tmux("send-keys", "-t", self.name, "Enter")
        pane = self._wait_for(self.name, "PATH=")
        self.assertIn("PATH=", pane, f"the probe never ran: {pane!r}")
        self.assertNotIn(
            TOKEN,
            pane,
            "the model's shell sees the agent's token — anything the model "
            "prints, writes or sends carries it",
        )

    def test_a_server_started_with_the_token_is_cleaned_up(self) -> None:
        """The server may have been started by something else.

        tmux keeps the environment of the client that started it. A server
        that was started before this release — or by anything else in the
        container — still carries the token, and every session created
        after that inherits it. The agent unsets it globally before the
        first pane is created.
        """
        dirty = dict(os.environ)
        dirty["AGENT_TOKEN"] = TOKEN
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", self.name],
            capture_output=True,
            text=True,
            check=False,
            env=dirty,
        )
        shown = self._tmux("show-environment", "-g")
        self.assertIn(
            "AGENT_TOKEN",
            shown,
            "the probe did not reproduce a server started with the token — "
            "the test would be vacuous",
        )
        # Now what the agent does before creating a pane.
        import asyncio  # noqa: PLC0415

        asyncio.run(self.agent_api._unscrub_tmux_server())
        shown = self._tmux("show-environment", "-g")
        self.assertNotIn(
            "AGENT_TOKEN",
            shown,
            "the agent does not clean a server that was started with the token",
        )


class TestTheDeploymentCanSeparateTheTokens(unittest.TestCase):
    """The operator's stack token must not have to live in the container.

    With one token for the whole stack, the agent's token *is* the
    operator's credential, so the leak above is the operator's leak. The
    compose files let the agent have its own inbound token instead.
    """

    def _compose(self, name: str) -> str:
        return (ROOT / name).read_text(encoding="utf-8")

    def test_the_agent_token_can_be_overridden(self) -> None:
        for name in ("docker-compose.yml", "docker-compose.production.yml"):
            with self.subTest(file=name):
                self.assertIn(
                    "AGENT_TOKEN=${AGENT_INBOUND_TOKEN:-",
                    self._compose(name),
                    "the agent's inbound token cannot be separated from the operator's stack token",
                )

    def test_the_proxy_forwards_the_agents_own_token(self) -> None:
        for name in ("docker-compose.yml", "docker-compose.production.yml"):
            with self.subTest(file=name):
                self.assertIn(
                    "OPENAPI_PROXY_UPSTREAM_TOKEN=${OPENAPI_PROXY_UPSTREAM_TOKEN:-${AGENT_INBOUND_TOKEN:-",
                    self._compose(name),
                    "the proxy does not forward the agent's own inbound token — "
                    "a separated deployment would not authenticate upstream",
                )

    def test_the_default_is_unchanged(self) -> None:
        """One token for the whole stack stays the default shape."""
        for name, fallback in (
            ("docker-compose.yml", "OPENAPI_PROXY_TOKEN must be set"),
            ("docker-compose.production.yml", "AGENT_STACK_TOKEN must be set"),
        ):
            with self.subTest(file=name):
                text = self._compose(name)
                self.assertIn("AGENT_TOKEN=${AGENT_INBOUND_TOKEN:-${", text)
                self.assertIn(
                    fallback,
                    text,
                    "the agent's token no longer falls back to the stack token — "
                    "a deployment without AGENT_INBOUND_TOKEN would not start",
                )


class TestTheSecurityDocumentSaysSo(unittest.TestCase):
    def test_the_document_states_the_boundary(self) -> None:
        text = " ".join((ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8").split())
        self.assertIn(
            "The sandbox is not a boundary",
            text,
            "the security document does not say what the sandbox is",
        )

    def test_the_document_names_the_separated_token(self) -> None:
        text = " ".join((ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8").split())
        self.assertIn(
            "AGENT_INBOUND_TOKEN",
            text,
            "the security document does not mention the separated inbound token",
        )


if __name__ == "__main__":
    unittest.main()
