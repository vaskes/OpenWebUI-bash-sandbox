"""Tests for the session registry lock and the exec answer contract.

**What the review said.** Two requests for the same ``session_id`` could
run the create path at the same time. The tmux server refuses a
duplicate session name, so the loser of that race did not get a second
pane — it got something worse: it went on as if it had created the
session, replaced the winner's window, and wrote **its own** directory
into the registry. The registry then named a directory the pane was not
running in, and the next command for that session answered from
somewhere else.

The fix is structural: one lock per session id owns the whole create
path, and one lock owns the registry itself. The registry is a plain
dict that a listing iterates and the persist path dumps **in a worker
thread**; a dict that changes size while it is iterated raises
``RuntimeError: dictionary changed size during iteration``, so the lock
is what keeps the listing and the persistence from crashing when two
requests arrive together.

**What the contract is.** Every exec answer now says whether the
command *started*. Before this, a command that never ran (the session
was busy) and a command that ran and timed out looked the same to a
caller: empty stdout, no exit code, ``timed_out: true``. The prose in
stderr said it — but a machine caller had to read English to tell
"queued" from "killed". ``command_started`` is that fact, and it is a
field, not a sentence.

**What these tests do NOT do.** A tripwire that cannot be made red is
decoration. The first draft of this file watched a dict subclass for
writes that happened while it was iterated — and a plain ``dict`` is
never iterated through ``__iter__`` when it is read as
``dict.items()``, so that watch could never fire and the test it
guarded was green for a reason that had nothing to do with the
property. Every test below is written so that breaking the property
makes **that** test red.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from test_agent_api_endpoints import _fresh_app  # noqa: E402
from test_busy_session_v2131 import BUSY_PANE, _patched_tmux, _TmuxDouble  # noqa: E402


class _CreateDouble:
    """A tmux double for the create path.

    It remembers whether a session exists: ``has-session`` answers "no"
    until a ``new-session`` has been answered "yes". That is the state
    the race needs — the second request must find the session the first
    one made.
    """

    def __init__(self, pane_cwd: str) -> None:
        self.pane_cwd = pane_cwd
        self.calls: list[tuple[str, ...]] = []
        self.created = False
        self.new_session_calls = 0

    async def __call__(self, *args: str) -> tuple[int, str, str]:
        # The answer is computed **before** the yield, and the yield is
        # what makes the race a race: two requests that are not
        # serialized both get to see "the session does not exist" and
        # both go on to create it. Without the yield the first request
        # would run to the end without ever giving the second one the
        # CPU, and the test would be green for a reason that has
        # nothing to do with the lock.
        self.calls.append(args)
        cmd = args[0] if args else ""
        answer = (0, "", "")
        if cmd == "has-session":
            answer = (0, "", "") if self.created else (1, "", "")
        elif cmd == "new-session":
            self.new_session_calls += 1
            self.created = True
        elif cmd == "display-message":
            fmt = args[-1] if args else ""
            if "pane_current_path" in fmt:
                answer = (0, self.pane_cwd, "")
            elif "history_limit" in fmt:
                answer = (0, "1000", "")
            else:
                answer = (0, "0", "")
        elif cmd == "list-sessions":
            answer = (0, "agent-x\n", "")
        await asyncio.sleep(0)
        return answer


class _RecordingLock:
    """The registry lock, with a memory of whether it was held.

    The lock is not a decoration: the registry is written by several
    requests at once. A test that cannot see the lock being taken
    proves nothing about it, so this one counts.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.acquisitions = 0
        self.held = False

    async def __aenter__(self):
        await self._lock.acquire()
        self.acquisitions += 1
        self.held = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.held = False
        self._lock.release()
        return False

    def locked(self) -> bool:
        return self._lock.locked()


class _TripwireDict(dict):
    """A registry that remembers the writes that were not guarded.

    A write to the registry that happens while the registry lock is not
    held is exactly the race the lock exists for — and it is invisible
    from the outside, so it has to be watched from the inside.

    ``items_reads`` counts how many times the live registry was read.
    The listing must read it **once** and answer from that snapshot: a
    second read is the answer being built from a registry that may have
    moved on since.
    """

    def __init__(self) -> None:
        super().__init__()
        self.unguarded_writes: list[str] = []
        self.items_reads = 0
        self._lock_probe = None

    def set_lock_probe(self, probe) -> None:
        self._lock_probe = probe

    def __setitem__(self, key: str, value: object) -> None:
        if self._lock_probe is not None and not self._lock_probe():
            self.unguarded_writes.append(key)
        super().__setitem__(key, value)

    def items(self):
        self.items_reads += 1
        return super().items()


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-agent-token-xyz"}
        # The module object the app under test runs in. The harness
        # reloads ``agent_api`` for every test, so the module in
        # ``sys.modules`` and the one the app was built from can be two
        # different objects; the globals of the endpoint function are the
        # namespace the request really runs in.
        self.module = sys.modules["agent_api"]

    def _patched_tmux(self, double):
        endpoint = None
        for route in self.app.routes:
            func = getattr(route, "endpoint", None)
            if func is not None and getattr(route, "path", "") == "/sessions":
                endpoint = func
                break
        assert endpoint is not None, "the create route is not on the app under test"
        return patch.dict(endpoint.__globals__, {"_tmux": double})


class TestTheCreatePathIsSerialized(_Base):
    """Two requests for one id must not create two sessions."""

    def _two_requests(self, double: _CreateDouble, sid: str = "race") -> list[object]:
        module = self.module

        async def _run() -> list[object]:
            body = module.CreateSession(session_id=sid, cwd=self.tmpdir)
            other = module.CreateSession(session_id=sid, cwd=self.tmpdir)
            return await asyncio.gather(
                module.create_session(body),
                module.create_session(other),
                return_exceptions=True,
            )

        with self._patched_tmux(double):
            return asyncio.run(_run())

    def test_only_one_tmux_session_is_created(self) -> None:
        double = _CreateDouble(pane_cwd=self.tmpdir)
        self._two_requests(double)
        self.assertEqual(
            double.new_session_calls,
            1,
            "two tmux sessions were created for one session id",
        )

    def test_the_registry_holds_one_entry(self) -> None:
        double = _CreateDouble(pane_cwd=self.tmpdir)
        self._two_requests(double)
        self.assertIn("race", self.sessions)
        self.assertEqual(len([k for k in self.sessions if k == "race"]), 1)

    def test_the_registry_names_the_directory_the_pane_is_in(self) -> None:
        # The pane is in ``self.tmpdir``. If the registry named anything
        # else, the next command would answer from the wrong place —
        # the lie this lock exists to stop.
        double = _CreateDouble(pane_cwd=self.tmpdir)
        self._two_requests(double)
        self.assertEqual(self.sessions["race"]["cwd"], self.tmpdir)

    def test_both_requests_answer(self) -> None:
        double = _CreateDouble(pane_cwd=self.tmpdir)
        results = self._two_requests(double)
        for result in results:
            self.assertNotIsInstance(result, BaseException, f"a request failed: {result!r}")

    def test_the_registry_is_written_under_the_lock(self) -> None:
        """The create path writes the registry — under the lock.

        A write that is not guarded is the race the lock exists for, and
        it cannot be seen from the outside: the registry is a plain dict
        and a plain dict does not complain. The tripwire is inside it.
        """
        module = self.module
        recorder = _RecordingLock()
        tripwire = _TripwireDict()
        tripwire.set_lock_probe(lambda: recorder.held)

        original = module.SESSIONS
        original_lock = module.SESSIONS_LOCK
        module.SESSIONS = tripwire
        module.SESSIONS_LOCK = recorder
        try:
            double = _CreateDouble(pane_cwd=self.tmpdir)
            self._two_requests(double)
        finally:
            module.SESSIONS = original
            module.SESSIONS_LOCK = original_lock

        self.assertEqual(
            tripwire.unguarded_writes,
            [],
            "the registry was written while the registry lock was not held",
        )


class TestTheRegistryIsNeverWalkedWhileItChanges(_Base):
    """The listing and the persist path must not walk a changing dict."""

    def test_the_listing_answers_from_its_own_snapshot(self) -> None:
        """A registry that changes under the listing must not change it.

        The listing takes a snapshot, prunes the sessions that are gone,
        persists — and *then* builds its answer. A concurrent request
        that lands in that window must not leak into the answer, and the
        answer must not be built from a registry that has moved on.
        """
        module = self.module
        tripwire = _TripwireDict()
        tripwire["old"] = {"tmux_name": "agent-old", "created": 1.0, "cwd": self.tmpdir}

        async def _run() -> dict:
            original = module.SESSIONS
            original_save = module._save_sessions

            async def _save_that_loses_a_race() -> None:
                # A concurrent request lands after the listing took its
                # snapshot and before the listing answers.
                tripwire["racer"] = {
                    "tmux_name": "agent-racer",
                    "created": 2.0,
                    "cwd": self.tmpdir,
                }
                await original_save()

            module.SESSIONS = tripwire
            module._save_sessions = _save_that_loses_a_race
            try:
                with patch.dict(module.__dict__, {"_tmux": _CreateDouble(self.tmpdir)}):
                    return await module.list_sessions()
            finally:
                module.SESSIONS = original
                module._save_sessions = original_save

        result = asyncio.run(_run())
        self.assertEqual(
            [s["session_id"] for s in result["sessions"]],
            ["old"],
            "the listing answered with something other than its own snapshot",
        )
        self.assertEqual(
            tripwire.items_reads,
            1,
            "the live registry was read more than once — the answer is not the snapshot",
        )

    def test_the_persisted_registry_is_a_copy(self) -> None:
        module = self.module
        module.SESSIONS["live"] = {
            "tmux_name": "agent-live",
            "created": 1.0,
            "cwd": self.tmpdir,
        }
        dumped: list[object] = []
        real_dump = json.dump

        def _spy(obj, *args, **kwargs):
            dumped.append(obj)
            return real_dump(obj, *args, **kwargs)

        with patch("json.dump", _spy):
            asyncio.run(module._save_sessions())
        self.assertTrue(dumped, "the registry was never persisted")
        self.assertIsNot(
            dumped[0],
            module.SESSIONS,
            "the live registry was dumped — a change to it during the write would crash the dump",
        )
        self.assertEqual(dumped[0], module.SESSIONS)

    def test_the_registry_has_a_lock(self) -> None:
        import asyncio as _asyncio  # noqa: PLC0415

        self.assertIsInstance(self.module.SESSIONS_LOCK, _asyncio.Lock)

    def test_the_persist_path_takes_the_registry_lock(self) -> None:
        module = self.module
        recorder = _RecordingLock()
        original = module.SESSIONS_LOCK
        module.SESSIONS_LOCK = recorder
        try:
            asyncio.run(module._save_sessions())
        finally:
            module.SESSIONS_LOCK = original
        self.assertGreaterEqual(
            recorder.acquisitions,
            1,
            "the persist path copied the registry without taking the lock",
        )

    def test_the_listing_takes_the_registry_lock(self) -> None:
        module = self.module
        recorder = _RecordingLock()
        original = module.SESSIONS_LOCK
        module.SESSIONS_LOCK = recorder
        try:
            with patch.dict(module.__dict__, {"_tmux": _CreateDouble(self.tmpdir)}):
                asyncio.run(module.list_sessions())
        finally:
            module.SESSIONS_LOCK = original
        self.assertGreaterEqual(
            recorder.acquisitions,
            1,
            "the listing read the registry without taking the lock",
        )


class TestTheCreationLockIsNotDroppedWhileItIsHeld(_Base):
    """A lock in use must not be handed out a second time."""

    def test_a_held_creation_lock_is_not_dropped(self) -> None:
        module = self.module

        async def _run():
            lock = asyncio.Lock()
            module._SESSION_CREATE_LOCKS["held"] = lock
            await lock.acquire()
            module._forget_session_create_lock("held")
            still_there = module._SESSION_CREATE_LOCKS.get("held")
            lock.release()
            module._SESSION_CREATE_LOCKS.pop("held", None)
            return still_there

        still_there = asyncio.run(_run())
        self.assertIsNotNone(
            still_there,
            "a creation lock that was in use was dropped — a second request would get a fresh one",
        )

    def test_an_idle_creation_lock_is_dropped(self) -> None:
        module = self.module

        async def _run():
            module._SESSION_CREATE_LOCKS["idle"] = asyncio.Lock()
            module._forget_session_create_lock("idle")
            return module._SESSION_CREATE_LOCKS.get("idle")

        still_there = asyncio.run(_run())
        self.assertIsNone(still_there, "an idle creation lock was kept forever — the dict grows")


class TestTheExecAnswerContract(_Base):
    """Every answer says whether the command started."""

    def _busy(self) -> dict:
        double = _TmuxDouble(BUSY_PANE, history_size="12", history_limit="8448")
        with _patched_tmux(self.app, double):
            response = self.client.post(
                "/sessions/busy/exec",
                json={"command": "echo hi", "timeout": 1},
                headers=self.auth,
            )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_a_busy_session_says_the_command_did_not_start(self) -> None:
        result = self._busy()
        self.assertIn("command_started", result)
        self.assertFalse(result["command_started"])
        self.assertIn("did not start", result["stderr"])

    def test_every_answer_that_reports_a_timeout_also_reports_the_start(self) -> None:
        """The tripwire: an answer shape that grows a ``timed_out`` and
        forgets ``command_started`` would let a caller read a queued
        command as a killed one."""
        source = (ROOT / "agent_api.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Dict):
                    continue
                keys = [k.value for k in sub.keys if isinstance(k, ast.Constant)]
                if "timed_out" not in keys:
                    continue
                checked += 1
                self.assertIn(
                    "command_started",
                    keys,
                    f"{node.name}(): an answer with 'timed_out' has no 'command_started'",
                )
        self.assertGreaterEqual(checked, 5, "the exec answer shapes were not found")

    def test_the_contract_is_documented(self) -> None:
        handoff = (ROOT / "docs" / "HANDOFF-mcp.md").read_text(encoding="utf-8")
        # The rule, not the word: the word appears twice in the file, so
        # a check for the word alone would stay green after the answer's
        # field list had lost it.
        flat = " ".join(handoff.split())
        self.assertIn(
            "`command_started`, `session_id` and `cwd`.",
            handoff,
            "the list of fields an exec answer returns does not mention command_started",
        )
        self.assertIn("Every exec answer carries `command_started`", flat)
        self.assertIn("`true` when the command ran", flat)
        self.assertIn("`false` when it never started", flat)
        architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        self.assertIn("SESSIONS_LOCK", architecture)


if __name__ == "__main__":
    unittest.main()
