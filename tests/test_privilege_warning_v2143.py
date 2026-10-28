"""The privilege mode must announce itself — and the announcement must be a fact.

``docs/SECURITY.md`` §9 and ``docs/CONFIGURATION.md`` promise the operator
one thing at start-up: *the agent logs a warning when ``sudo`` is present*
— either that the mode is on, or which of the two runtime settings is
still in the way, or that the root filesystem is read-only.

A promise like that is only worth something if it is pinned. There was
no test for the privilege warning at all: the sibling function (the
read-only root) was tested directly, so the *path* that reaches it from
the start-up could rot unnoticed — and it did.

The rot was small and it was honest to describe: the function was
decorated with ``@asynccontextmanager`` and called as a plain function.
The decorator turns a call into "hand back a context manager", and the
body only ran because the decorator's constructor called the function on
the way there. The warning *did* print — but a reader could not tell that
from the code, and ``async with`` on it raises ``TypeError``.

So the decorator is gone, and these tests pin the behaviour that the
documentation promises:

* the mode is announced, loudly, when ``sudo`` is present;
* the setting that blocks the escalation is named;
* the hollow root (a read-only filesystem) is announced too;
* nothing is said when there is no ``sudo`` to escalate to;
* the start-up really calls the warning — a function that is never
  called prints nothing, however loud it is.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: /proc/self/status of a container where the setuid bit still works.
STATUS_ESCALATION_WORKS = "NoNewPrivs:\t0\nCapBnd:\t0000003fffffffff\n"
#: ... and one where the kernel ignores the setuid bit.
STATUS_NO_NEW_PRIVILEGES = "NoNewPrivs:\t1\nCapBnd:\t0000003fffffffff\n"
#: ... and one where the capability bounding set is empty.
STATUS_EMPTY_CAP_BND = "NoNewPrivs:\t0\nCapBnd:\t0000000000000000\n"

#: A read-only root filesystem — the stock posture of the stock stack.
READ_ONLY_MOUNTINFO = """\
25 0 8:1 / / ro,relatime - overlay overlay ro,lowerdir=/var/lib/docker/overlay2/l/AAA
26 0 8:2 / /workspace rw,relatime - ext4 /dev/sda1 rw
"""
#: A writable one.
WRITABLE_MOUNTINFO = """\
25 0 8:1 / / rw,relatime - overlay overlay rw,lowerdir=/var/lib/docker/overlay2/l/AAA
26 0 8:2 / /workspace rw,relatime - ext4 /dev/sda1 rw
"""


class _Shutil:
    """A ``shutil`` whose ``which`` answers for ``sudo`` and nothing else.

    The stub replaces the *module's* global, not ``shutil.which`` itself:
    patching the standard library would reach into the test harness (the
    session bookkeeping asks ``shutil.which("tmux")``).
    """

    def __init__(self, sudo: str | None) -> None:
        self.sudo = sudo

    def which(self, name: str) -> str | None:
        return self.sudo if name == "sudo" else None


def _fresh_agent():
    """Reload ``agent_api`` against a temp workspace and return the module.

    The reload matters: the harness drops the module from
    ``sys.modules`` between tests, so the module a test patches has to be
    the one the application runs.
    """
    tmpdir = tempfile.mkdtemp(prefix="agent-privilege-test-")
    atexit.register(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
    os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmpdir, "sessions.json")
    os.environ["AGENT_WORKSPACE"] = tmpdir
    os.environ["AGENT_TOKEN"] = "test-agent-token-xyz"
    for name in ("agent_api", "mcp_server", "agent_config"):
        sys.modules.pop(name, None)
    import agent_api  # noqa: PLC0415

    return agent_api


class TestThePrivilegeWarning(unittest.TestCase):
    """The warning the documentation promises."""

    def setUp(self) -> None:
        self.agent_api = _fresh_agent()

    def _warn(
        self,
        *,
        sudo: str | None = "/usr/bin/sudo",
        status: str = STATUS_ESCALATION_WORKS,
        mountinfo: str = READ_ONLY_MOUNTINFO,
    ) -> None:
        """Call the warning the way the start-up calls it."""
        with mock.patch.dict(self.agent_api.__dict__, {"shutil": _Shutil(sudo)}):
            self.agent_api._warn_about_the_privilege_mode(status=status, mountinfo=mountinfo)

    def test_the_mode_is_announced(self) -> None:
        with self.assertLogs(level=logging.WARNING) as captured:
            self._warn()
        message = "\n".join(captured.output)
        self.assertIn("the privilege mode is ON", message)
        self.assertIn("PRIVATE, SECURED DEPLOYMENTS ONLY", message)

    def test_the_setting_that_blocks_the_escalation_is_named(self) -> None:
        with self.assertLogs(level=logging.WARNING) as captured:
            self._warn(status=STATUS_NO_NEW_PRIVILEGES)
        message = "\n".join(captured.output)
        self.assertIn("the escalation would NOT work", message)
        self.assertIn("no-new-privileges", message)
        self.assertIn("AGENT_NO_NEW_PRIVILEGES=false", message)

    def test_an_empty_bounding_set_is_named(self) -> None:
        with self.assertLogs(level=logging.WARNING) as captured:
            self._warn(status=STATUS_EMPTY_CAP_BND)
        message = "\n".join(captured.output)
        self.assertIn("the escalation would NOT work", message)
        self.assertIn("capability bounding set is empty", message)

    def test_the_hollow_root_is_announced_too(self) -> None:
        """A root shell in a read-only container is a half-root — say so."""
        with self.assertLogs(level=logging.WARNING) as captured:
            self._warn(mountinfo=READ_ONLY_MOUNTINFO)
        message = "\n".join(captured.output)
        self.assertIn("READ-ONLY", message)
        self.assertIn("AGENT_READ_ONLY=false", message)
        self.assertIn("apt-get", message)

    def test_a_writable_root_says_nothing_about_itself(self) -> None:
        with self.assertLogs(level=logging.WARNING) as captured:
            self._warn(mountinfo=WRITABLE_MOUNTINFO)
        message = "\n".join(captured.output)
        self.assertNotIn("READ-ONLY", message)

    def test_nothing_is_said_without_sudo(self) -> None:
        """No sudo, no escalation, nothing to announce."""
        with self.assertNoLogs(level=logging.WARNING):
            self._warn(sudo=None)


class TestTheFunctionIsAPlainFunction(unittest.TestCase):
    """A function dressed as a context manager does its work by accident.

    ``@asynccontextmanager`` on an ordinary function makes the *call*
    hand back a context manager; the body runs only because the
    decorator's constructor called the function on the way there, and
    ``async with`` on the result raises ``TypeError: 'NoneType' object
    is not an async iterator``.

    The call must return ``None`` — the function's own return value —
    because the start-up calls it as a statement.
    """

    def setUp(self) -> None:
        self.agent_api = _fresh_agent()

    def test_the_call_returns_nothing(self) -> None:
        with mock.patch.dict(self.agent_api.__dict__, {"shutil": _Shutil(None)}):
            returned = self.agent_api._warn_about_the_privilege_mode(
                status=STATUS_ESCALATION_WORKS,
                mountinfo=WRITABLE_MOUNTINFO,
            )
        self.assertIsNone(returned)


class TestTheStartupAnnouncesIt(unittest.TestCase):
    """A warning that nobody calls prints nothing, however loud it is."""

    def setUp(self) -> None:
        self.agent_api = _fresh_agent()

    def test_the_startup_calls_the_warning(self) -> None:
        calls: list[tuple[object, object]] = []

        def recorder(status=None, mountinfo=None):  # noqa: ANN001, ANN202
            calls.append((status, mountinfo))

        app = self.agent_api.app
        with mock.patch.dict(
            self.agent_api.__dict__,
            {"_warn_about_the_privilege_mode": recorder},
        ):

            async def scenario() -> None:
                async with app.router.lifespan_context(app):
                    pass

            asyncio.run(scenario())

        self.assertTrue(
            calls,
            "the start-up did not announce the privilege mode — the warning "
            "is unreachable from the only place that could print it",
        )
