"""Regression tripwires for the uvloop/first-exec deadlock.

Background
----------
uvicorn picks uvloop automatically whenever it is importable. Under uvloop
the first ``tmux new-session`` in a fresh container - the call that forks and
daemonises the tmux server - never completes, so the first ``/exec`` request
hangs forever while ``/healthz`` keeps returning 200. The container looks
healthy and cannot run a single command.

These tests pin the fix from both ends:

* the image must actually ask uvicorn for the plain asyncio loop, so a future
  edit that drops ``--loop asyncio`` fails here instead of in production;
* a real cold-start exec must complete under that loop, so the pin cannot
  outlive the bug it pins.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX_DOCKERFILE = REPO_ROOT / "Dockerfile.sandbox"


def _cmd_tokens() -> list[str]:
    """Return the exec-form CMD of Dockerfile.sandbox as a token list."""
    text = SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"^CMD\s+\[(.*?)\]", text, re.DOTALL | re.MULTILINE)
    assert match, "Dockerfile.sandbox must use exec-form CMD"
    # Join the line continuations, then split on the JSON string literals.
    joined = match.group(1).replace("\\\n", " ")
    return [token.strip().strip('"') for token in joined.split(",") if token.strip()]


class TestAgentEventLoopPinned(unittest.TestCase):
    """The shipped agent must not run on uvloop."""

    def test_cmd_pins_asyncio_loop(self) -> None:
        tokens = _cmd_tokens()
        self.assertIn(
            "--loop",
            tokens,
            "Dockerfile.sandbox CMD must pass --loop explicitly; without it "
            "uvicorn silently auto-selects uvloop and the first exec deadlocks.",
        )
        idx = tokens.index("--loop")
        self.assertEqual(
            tokens[idx + 1],
            "asyncio",
            "The agent must run on plain asyncio. uvloop deadlocks the first "
            "tmux new-session in a cold container (it forks and daemonises "
            "the tmux server, and asyncio's subprocess pipes never resolve).",
        )

    def test_cmd_mentions_why(self) -> None:
        text = SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn(
            "uvloop",
            text,
            "Dockerfile.sandbox must document the uvloop deadlock next to the "
            "--loop flag, otherwise the next person will 'optimise' it away.",
        )


class TestColdStartExecUnderPinnedLoop(unittest.TestCase):
    """A cold-start exec must complete on the loop the image actually uses."""

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_first_exec_on_a_cold_agent_completes(self) -> None:
        import agent_api

        agent = agent_api.__file__
        if agent.endswith(".pyc"):  # pragma: no cover - defensive
            self.skipTest("agent_api resolved to a bytecode file")

        # No /workspace volume here, so the sessions file and the tmux socket
        # both start empty - the same cold state as a fresh container.
        loop = asyncio.new_event_loop()
        try:
            with cold_workspace() as workspace:
                with mock.patch.object(agent_api, "WORKSPACE", workspace):
                    result = loop.run_until_complete(
                        asyncio.wait_for(
                            agent_api.exec_cmd(
                                "coldstart",
                                agent_api.ExecRequest(
                                    session_id="coldstart",
                                    command="echo cold-start-ok",
                                    timeout=10,
                                ),
                            ),
                            timeout=25,
                        )
                    )
        finally:
            loop.close()

        self.assertEqual(result.get("exit_code"), 0, result)
        self.assertIn("cold-start-ok", result.get("stdout", ""))

        # The tmux server must exist afterwards - if the daemonising spawn
        # had deadlocked, nothing above would have returned at all.
        sessions = subprocess.run(
            ["tmux", "ls"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout
        self.assertIn("coldstart", sessions)


@contextlib.contextmanager
def cold_workspace() -> Iterator[str]:
    """Throwaway workspace so the test does not need a real /workspace mount.

    A fresh directory is what makes this a *cold* start: the sessions file is
    absent and no tmux server is running, exactly like a new container.
    """
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp
