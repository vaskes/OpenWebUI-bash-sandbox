"""Wave 3 regression tests — fixes for the 3 CRITICAL blockers that
were left open in Wave 2:

#C1: Open WebUI SQLite was inside /workspace — LLM could ``rm -rf``
     the database. Now lives in a named volume.
#C2: SSH ``-i <path>`` was accepted for any path matching the
     char whitelist — including ``/home/agent/.ssh/id_rsa``. First
     fixed by scoping the identity to the workspace; since v2.11.0 the
     flag is refused outright, because a path check in the proxy cannot
     be made atomic with the call that opens the file in the sandbox.
#C3: SSH ``-o`` was whitelisted by *name* (blocklist style) — a new
     option like ``LocalCommand`` would slip through. Now whitelisted
     by option *name* from a fixed safe set.
#H1: WebSocket happy path through ``Sec-WebSocket-Protocol`` was not
     tested — only the rejection case. Added.
#H2: Lifespan did not reap dead sessions on startup. Added.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

# Make sure the proxy module picks up a token before any import-time
# validation runs.
os.environ.setdefault("OPENAPI_PROXY_TOKEN", "test-token")
os.environ.setdefault("AGENT_TOKEN", "test-token")


# ---------------------------------------------------------------------------
# #C1 — Open WebUI data volume
# ---------------------------------------------------------------------------
class TestOpenWebuiDataVolumeNotInWorkspace(unittest.TestCase):
    """#C1: The Open WebUI SQLite volume must not be bind-mounted into
    the agent's /workspace, otherwise the LLM can ``rm -rf`` the database."""

    def test_open_webui_volume_is_named_not_bind(self) -> None:
        with open(os.path.join(os.path.dirname(__file__), "..", "docker-compose.yml")) as f:
            compose = f.read()
        # The bind mount is the dangerous pattern.
        self.assertNotIn(
            "/opt/agent-workspace/.open-webui-data:/app/backend/data",
            compose,
            "open-webui data MUST NOT be bind-mounted into /workspace; "
            "use the open-webui-data named volume",
        )
        # The named volume is the safe pattern.
        self.assertIn(
            "open-webui-data:/app/backend/data",
            compose,
            "open-webui data must be a named volume, not a bind mount",
        )
        # And the volume is declared at the bottom.
        # Multi-line search: top-level "volumes:" + indented "open-webui-data:"
        in_volumes_block = False
        for line in compose.splitlines():
            if line == "volumes:":
                in_volumes_block = True
                continue
            if in_volumes_block and line and not line.startswith(" "):
                in_volumes_block = False
            if in_volumes_block and line.strip() == "open-webui-data:":
                break
        else:
            self.fail("open-webui-data must be declared in the top-level volumes: section")


# ---------------------------------------------------------------------------
# #C2 — SSH -i <path> is refused outright
# ---------------------------------------------------------------------------
class TestSshIdentityFlagIsRefused(unittest.TestCase):
    """#C2, revised in v2.11.0.

    The wave-3 rule was "``-i`` must point inside the workspace", checked
    by the proxy with ``realpath`` + ``commonpath``. That check is a
    *path* check performed in the proxy, while the file is opened by
    ``ssh`` later, in the sandbox — two processes, two filesystems, a
    round-trip in between. A model with a second session could swap the
    validated file for a symlink inside that window, and the old
    implementation said so in its own docstring ("the TOCTOU window ...
    is exploitable").

    A path rule cannot be made race-free here: the only check that
    cannot be raced is the permission check the shell itself performs
    when it opens the file. So the flag is refused outright, and the
    operator's key is protected by the permissions of its read-only
    mount (docs/SECURITY.md, section 3).
    """

    def setUp(self) -> None:
        # Import here so env vars are in place.
        from openapi_proxy import _validate_ssh_args

        self._validate = _validate_ssh_args

    def test_identity_in_persistent_ssh_dir_rejected(self) -> None:
        """The exact Wave 3 attack: -i /home/agent/.ssh/id_rsa."""
        host, err = self._validate("ssh -i /home/agent/.ssh/id_rsa lab.local")
        self.assertIsNone(host)
        self.assertIsNotNone(err)
        self.assertIn("identity", err.lower())
        self.assertIn("not allowed", err.lower())

    def test_identity_in_etc_rejected(self) -> None:
        host, err = self._validate("ssh -i /etc/passwd lab.local")
        self.assertIsNone(host)
        self.assertIn("identity", err.lower())

    def test_identity_in_tmp_rejected(self) -> None:
        """Even /tmp is outside the workspace by default."""
        host, err = self._validate("ssh -i /tmp/key lab.local")
        self.assertIsNone(host)
        self.assertIn("identity", err.lower())

    def test_identity_inside_workspace_rejected_too(self) -> None:
        """A key the LLM just wrote into /workspace is refused as well.

        The old rule accepted it. Accepting it is what made the check
        racy: a path the model may rewrite at any moment cannot be
        pinned by a check that happened earlier.
        """
        with tempfile.TemporaryDirectory() as tmp:
            key = os.path.join(tmp, "id_ed25519")
            # Create a real file so a path check would have accepted it.
            with open(key, "w") as f:
                f.write("fake key")
            with patch.dict(os.environ, {"AGENT_WORKSPACE": tmp}):
                # ssh is a bare hostname so it has to be in the allowlist;
                # we don't care about the allowlist outcome here, only
                # the -i check.
                host, err = self._validate(f"ssh -i {key} lab.local")
        self.assertIsNone(host)
        self.assertIn("identity", err.lower())

    def test_nonexistent_identity_rejected(self) -> None:
        """There is no path the shortcut accepts an identity from."""
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "does-not-exist")
            with patch.dict(os.environ, {"AGENT_WORKSPACE": tmp}):
                host, err = self._validate(f"ssh -i {fake} lab.local")
        self.assertIsNone(host)
        self.assertIn("identity", err.lower())

    def test_the_flag_is_not_in_the_allowlist(self) -> None:
        """The flag list and the behaviour must agree."""
        from openapi_proxy import SSH_ALLOWED_FLAGS

        self.assertNotIn("-i", SSH_ALLOWED_FLAGS)

    def test_the_toctou_helper_is_gone(self) -> None:
        """The racy path check must not stay in the tree as dead code."""
        import openapi_proxy

        self.assertFalse(
            hasattr(openapi_proxy, "_ssh_identity_within_workspace"),
            "the TOCTOU-racy identity check must be removed, not just unused",
        )


# ---------------------------------------------------------------------------
# #C3 — SSH -o option whitelist
# ---------------------------------------------------------------------------
class TestSshOptionWhitelist(unittest.TestCase):
    """#C3: ``-o ProxyCommand``, ``-o LocalCommand``, ``-o LocalForward``,
    ``-o RemoteForward``, ``-o DynamicForward``, ``-o PermitLocalCommand``
    must all be rejected by *name*."""

    def setUp(self) -> None:
        from openapi_proxy import _validate_ssh_args

        self._validate = _validate_ssh_args

    def test_proxycommand_rejected(self) -> None:
        # Two ways the proxy blocks ProxyCommand:
        # (1) shell metachars in the value get caught by the unsafe-token regex
        # (2) the ProxyCommand name itself is not in the safe-options whitelist
        host, err = self._validate("ssh -o ProxyCommand=curl-evil lab.local")
        self.assertIsNone(host)
        # Either rejection path is acceptable. We don't pin the wording.
        self.assertIsNotNone(err)
        # Whichever path triggered, the host is not returned.
        self.assertEqual(host, None)

    def test_localcommand_rejected(self) -> None:
        host, err = self._validate('ssh -o LocalCommand="id" lab.local')
        self.assertIsNone(host)
        self.assertIn("not allowed", err.lower())

    def test_permitlocalcommand_rejected(self) -> None:
        host, err = self._validate("ssh -o PermitLocalCommand=yes lab.local")
        self.assertIsNone(host)
        self.assertIn("not allowed", err.lower())

    def test_localforward_rejected(self) -> None:
        host, err = self._validate("ssh -o LocalForward=2222:evil:22 lab.local")
        self.assertIsNone(host)
        self.assertIn("not allowed", err.lower())

    def test_remoteforward_rejected(self) -> None:
        host, err = self._validate("ssh -o RemoteForward=2222:127.0.0.1:22 lab.local")
        self.assertIsNone(host)
        self.assertIn("not allowed", err.lower())

    def test_dynamicforward_rejected(self) -> None:
        host, err = self._validate("ssh -o DynamicForward=1080 lab.local")
        self.assertIsNone(host)
        self.assertIn("not allowed", err.lower())

    def test_stricthostkeychecking_override_rejected(self) -> None:
        """Even though StrictHostKeyChecking=accept-new is the default,
        we don't let the LLM override it (defence in depth)."""
        host, err = self._validate("ssh -o StrictHostKeyChecking=no lab.local")
        self.assertIsNone(host)
        self.assertIn("not allowed", err.lower())

    def test_safe_option_accepted(self) -> None:
        """ConnectTimeout is in the safe set — no rejection from the
        option check. The host check may still kick in (lab.local is
        not in the allowlist by default), so we test the option gate
        in isolation."""
        # Use case-insensitive matching to mirror ssh semantics.
        from openapi_proxy import _validate_ssh_args

        with patch("openapi_proxy._ssh_host_allowed", return_value=True):
            host, safe_argv = _validate_ssh_args("ssh -o ConnectTimeout=5 lab.local")
        self.assertIsNotNone(host, f"err was: {safe_argv}")
        self.assertIsNotNone(safe_argv, "must produce a safe argv string")
        self.assertIn("ConnectTimeout=5", safe_argv)

    def test_case_insensitive_option_accepted(self) -> None:
        """ssh option keys are case-insensitive — match the real tool."""
        from openapi_proxy import _validate_ssh_args

        with patch("openapi_proxy._ssh_host_allowed", return_value=True):
            host, safe_argv = _validate_ssh_args("ssh -o connecttimeout=5 lab.local")
        self.assertIsNotNone(host, f"err was: {safe_argv}")
        self.assertIn("connecttimeout=5", safe_argv.lower())

    def test_o_without_equals_rejected(self) -> None:
        """A bare ``-o LocalCommand`` (without =value) is invalid ssh syntax."""
        host, err = self._validate("ssh -o LocalCommand lab.local")
        self.assertIsNone(host)
        self.assertIn("invalid", err.lower())


# ---------------------------------------------------------------------------
# #H2 — Startup reaper for dead tmux sessions
# ---------------------------------------------------------------------------
class TestStartupReaper(unittest.IsolatedAsyncioTestCase):
    """#H2: The lifespan handler must drop registry entries whose tmux
    backend no longer exists."""

    async def test_reap_drops_dead_sessions(self) -> None:
        from agent_api import SESSIONS, _reap_dead_sessions, _save_sessions

        # Snapshot and restore around the test so we don't pollute the
        # module-level registry.
        original = dict(SESSIONS)
        SESSIONS.clear()
        SESSIONS.update(
            {
                "alive-session": {"tmux_name": "tmux-alive", "cwd": "/workspace"},
                "dead-session": {"tmux_name": "tmux-dead", "cwd": "/workspace"},
            }
        )
        try:
            with (
                patch(
                    "agent_api._tmux",
                    new=AsyncMock(return_value=(0, "tmux-alive\n", "")),
                ),
                patch("agent_api._save_sessions", new=AsyncMock()) as save,
            ):
                reaped = await _reap_dead_sessions()
            self.assertEqual(reaped, ["dead-session"])
            self.assertIn("alive-session", SESSIONS)
            self.assertNotIn("dead-session", SESSIONS)
            save.assert_awaited_once()
        finally:
            SESSIONS.clear()
            SESSIONS.update(original)

    async def test_reap_no_dead_sessions(self) -> None:
        from agent_api import SESSIONS, _reap_dead_sessions

        original = dict(SESSIONS)
        SESSIONS.clear()
        SESSIONS["only-alive"] = {"tmux_name": "tmux-only", "cwd": "/workspace"}
        try:
            with (
                patch(
                    "agent_api._tmux",
                    new=AsyncMock(return_value=(0, "tmux-only\n", "")),
                ),
                patch("agent_api._save_sessions", new=AsyncMock()) as save,
            ):
                reaped = await _reap_dead_sessions()
            self.assertEqual(reaped, [])
            self.assertIn("only-alive", SESSIONS)
            save.assert_not_awaited()
        finally:
            SESSIONS.clear()
            SESSIONS.update(original)

    async def test_reap_handles_tmux_not_running(self) -> None:
        """If tmux has no sessions at all, every registry entry is dead."""
        from agent_api import SESSIONS, _reap_dead_sessions

        original = dict(SESSIONS)
        SESSIONS.clear()
        SESSIONS["s1"] = {"tmux_name": "tmux-s1", "cwd": "/workspace"}
        SESSIONS["s2"] = {"tmux_name": "tmux-s2", "cwd": "/workspace"}
        try:
            with (
                patch(
                    "agent_api._tmux",
                    new=AsyncMock(return_value=(1, "", "no sessions")),
                ),
                patch("agent_api._save_sessions", new=AsyncMock()) as save,
            ):
                reaped = await _reap_dead_sessions()
            self.assertEqual(set(reaped), {"s1", "s2"})
            self.assertEqual(SESSIONS, {})
            save.assert_awaited_once()
        finally:
            SESSIONS.clear()
            SESSIONS.update(original)


if __name__ == "__main__":
    unittest.main()
