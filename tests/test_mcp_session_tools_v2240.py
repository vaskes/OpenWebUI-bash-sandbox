"""The session tools — ``todo_list``, ``memory``, ``session_search``, ``clarify``.

Four small things an agent actually feels missing: a plan it can rewrite,
notes it can keep, a way to find what it ran, and a way to ask the
operator something only the operator knows.

What is pinned here:

* the task list is **replaceable** and **bounded** — over the limit is
  refused, and the list that is refused is the list that was there;
* the memory is **a file**: the operator can read it, and it survives
  the agent's restart;
* the recall finds the commands a session ran, and its answer is
  bounded;
* the question goes back to the caller — the door has no channel of its
  own, and that is a measured fact about the transport, not a guess;
* the tools are counted.

And a check that cannot be made red is a decoration:
``tools/mutation-check-v2240.sh`` breaks each of these on purpose and
expects the named test to go red.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOKEN = "test-agent-token-xyz"


def _fresh_app(**environment: str):
    """Reload the agent against a temp workspace and the given environment."""
    tmpdir = tempfile.mkdtemp(prefix="agent-session-tools-test-")
    atexit.register(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
    os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmpdir, "sessions.json")
    os.environ["AGENT_WORKSPACE"] = tmpdir
    os.environ["AGENT_TOKEN"] = TOKEN
    os.environ.pop("AGENT_MCP_ALLOWED_HOSTS", None)
    # A knob a test set must not leak into the next one: the config is
    # read from the environment at import time, so a leftover knob
    # would change the next test's limits.
    for name in (
        "AGENT_TODO_MAX_ITEMS",
        "AGENT_TODO_MAX_ITEM_BYTES",
        "AGENT_MEMORY_MAX_NOTES",
        "AGENT_MEMORY_MAX_NOTE_BYTES",
        "AGENT_SESSION_HISTORY_MAX_COMMANDS",
        "AGENT_SESSION_HISTORY_MAX_COMMAND_BYTES",
        "AGENT_SESSION_SEARCH_MAX_RESULTS",
    ):
        os.environ.pop(name, None)
    for name, value in environment.items():
        os.environ[name] = value
    for name in ("agent_api", "mcp_server", "agent_config"):
        if name in sys.modules:
            del sys.modules[name]
    from agent_api import SESSIONS, app  # noqa: E402

    SESSIONS.clear()
    return app, tmpdir


def _call(coro):
    """Run one tool call and return its result.

    A tool that returns a shape of its own is called directly here, so
    the answer is the shape it returned. The tools that build their own
    result (``session_search``) answer with a ``CallToolResult``, and
    the structured answer is in ``structured_content``.
    """
    return asyncio.run(coro)


def _refusal(coro) -> str:
    """Run one tool call that must be refused, and return the reason."""
    with unittest.TestCase().assertRaises(Exception) as caught:
        asyncio.run(coro)
    return str(caught.exception)


class _Base(unittest.TestCase):
    """A fresh agent per test, with the tools loaded against it."""

    def setUp(self) -> None:
        self.app, self.tmpdir = _fresh_app()
        self.ws = Path(self.tmpdir)
        from mcp_server import clarify, memory, session_search, todo_list  # noqa: PLC0415

        self.todo_list = todo_list
        self.memory = memory
        self.session_search = session_search
        self.clarify = clarify

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class _Http(_Base):
    """The HTTP door, over the same app the tools live in."""

    def _alive_session(self, sid: str = "mcp") -> None:
        """Put a session in the registry, and keep the reaper off it.

        The reaper drops a session whose tmux pane is gone — and it
        drops the session's task list and pending question with it. The
        test has no pane, so it says the pane is there.
        """
        import agent_api  # noqa: PLC0415

        agent_api.SESSIONS[sid] = {
            "tmux_name": f"owbs-{sid}",
            "created": 1.0,
            "cwd": str(self.ws),
        }
        original = agent_api._tmux  # noqa: SLF001 - the test stands in for tmux

        async def fake_tmux(*args: str) -> tuple[int, str, str]:
            if args and args[0] == "list-sessions":
                return (0, f"owbs-{sid}\n", "")
            return (0, "", "")

        agent_api._tmux = fake_tmux  # noqa: SLF001
        self.addCleanup(setattr, agent_api, "_tmux", original)

    async def _sessions(self) -> dict:
        import httpx  # noqa: PLC0415

        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
            answer = await client.get("/sessions", headers={"Authorization": f"Bearer {TOKEN}"})
        return answer.json()


class TestTheTaskListIsAPlan(_Http):
    """A plan the model can rewrite, and cannot let grow without bound."""

    def test_the_list_is_read_when_nothing_is_passed(self) -> None:
        result = _call(self.todo_list(["write the code", "[x] read the code"]))
        self.assertEqual(result["items"], ["write the code", "[x] read the code"])
        self.assertEqual(result["done"], 1)
        self.assertEqual(result["count"], 2)
        again = _call(self.todo_list())
        self.assertEqual(again["items"], ["write the code", "[x] read the code"])

    def test_the_list_is_replaced_and_not_merged(self) -> None:
        _call(self.todo_list(["one", "two", "three"]))
        result = _call(self.todo_list(["four"]))
        self.assertEqual(result["items"], ["four"])
        self.assertEqual(result["count"], 1)
        # And the list that is kept is the one that was written: a
        # replacement that only looks like one is a merge.
        self.assertEqual(_call(self.todo_list())["items"], ["four"])

    def test_the_list_of_one_session_is_not_the_list_of_another(self) -> None:
        _call(self.todo_list(["mine"], session_id="one"))
        _call(self.todo_list(["yours"], session_id="two"))
        self.assertEqual(_call(self.todo_list(session_id="one"))["items"], ["mine"])
        self.assertEqual(_call(self.todo_list(session_id="two"))["items"], ["yours"])

    def test_a_list_over_the_limit_is_refused_and_the_old_list_stays(self) -> None:
        _call(self.todo_list(["keep me"]))
        self.app, self.tmpdir = _fresh_app(AGENT_TODO_MAX_ITEMS="2")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        from mcp_server import todo_list  # noqa: PLC0415

        reason = _refusal(todo_list(["a", "b", "c"]))
        self.assertIn("over the limit", reason)
        self.assertIn("2", reason)
        self.assertEqual(_call(todo_list())["items"], [])

    def test_an_item_over_the_limit_is_refused(self) -> None:
        self.app, self.tmpdir = _fresh_app(AGENT_TODO_MAX_ITEM_BYTES="32")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        from mcp_server import todo_list  # noqa: PLC0415

        reason = _refusal(todo_list(["x" * 200]))
        self.assertIn("over the limit", reason)
        self.assertEqual(_call(todo_list())["items"], [])

    def test_the_session_record_carries_the_list(self) -> None:
        self._alive_session()
        _call(self.todo_list(["a step"], session_id="mcp"))
        answer = asyncio.run(self._sessions())
        record = [s for s in answer["sessions"] if s["session_id"] == "mcp"]
        self.assertEqual(len(record), 1)
        self.assertEqual(record[0]["todo"], ["a step"])


class TestTheMemoryIsAFile(_Base):
    """Notes the model keeps, in a file the operator can read and delete."""

    def test_the_memory_starts_empty(self) -> None:
        result = _call(self.memory())
        self.assertEqual(result["notes"], [])
        self.assertEqual(result["count"], 0)

    def test_a_note_is_added_and_read_back(self) -> None:
        _call(self.memory(action="add", text="the project is called lol"))
        result = _call(self.memory(action="list"))
        self.assertEqual(result["notes"], ["the project is called lol"])
        self.assertEqual(result["count"], 1)

    def test_the_memory_is_a_file_the_operator_can_read(self) -> None:
        _call(self.memory(action="add", text="a note"))
        from agent_api import MEMORY_FILE  # noqa: PLC0415

        self.assertTrue(MEMORY_FILE.endswith(".agent-memory.json"))
        with open(MEMORY_FILE, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), ["a note"])

    def test_the_memory_survives_the_agent_going_away(self) -> None:
        _call(self.memory(action="add", text="still here"))
        # A fresh agent against the same state directory: the memory is
        # on disk, not in the process.
        os.environ["AGENT_SESSIONS_FILE"] = os.path.join(self.tmpdir, "sessions.json")
        for name in ("agent_api", "mcp_server", "agent_config"):
            sys.modules.pop(name, None)
        from mcp_server import memory  # noqa: PLC0415

        self.assertEqual(_call(memory(action="list"))["notes"], ["still here"])

    def test_a_note_is_deleted_by_its_number(self) -> None:
        _call(self.memory(action="add", text="first"))
        _call(self.memory(action="add", text="second"))
        result = _call(self.memory(action="delete", index=0))
        self.assertEqual(result["notes"], ["second"])

    def test_the_memory_can_be_emptied(self) -> None:
        _call(self.memory(action="add", text="first"))
        result = _call(self.memory(action="clear"))
        self.assertEqual(result["notes"], [])

    def test_an_unknown_action_is_refused(self) -> None:
        reason = _refusal(self.memory(action="erase-everything"))
        self.assertIn("unknown action", reason)

    def test_an_empty_note_is_refused(self) -> None:
        reason = _refusal(self.memory(action="add", text="   "))
        self.assertIn("empty", reason)

    def test_a_note_over_the_limit_is_refused(self) -> None:
        self.app, self.tmpdir = _fresh_app(AGENT_MEMORY_MAX_NOTE_BYTES="64")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        from mcp_server import memory  # noqa: PLC0415

        reason = _refusal(memory(action="add", text="y" * 500))
        self.assertIn("over the limit", reason)
        self.assertEqual(_call(memory(action="list"))["notes"], [])

    def test_the_memory_is_bounded(self) -> None:
        self.app, self.tmpdir = _fresh_app(AGENT_MEMORY_MAX_NOTES="2")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        from mcp_server import memory  # noqa: PLC0415

        _call(memory(action="add", text="one"))
        _call(memory(action="add", text="two"))
        reason = _refusal(memory(action="add", text="three"))
        self.assertIn("limit", reason)
        self.assertEqual(_call(memory(action="list"))["count"], 2)


class TestTheRecallFindsWhatRan(_Base):
    """The commands a session ran, and a search over them."""

    def setUp(self) -> None:
        super().setUp()
        from agent_api import remember_command  # noqa: PLC0415

        remember_command("mcp", "ls -la /workspace")
        remember_command("mcp", "pytest -q tests")
        remember_command("other", "grep -r todo .")

    def test_the_command_is_found(self) -> None:
        result = _call(self.session_search("pytest")).structured_content
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["command"], "pytest -q tests")
        self.assertEqual(result["matches"][0]["session_id"], "mcp")

    def test_the_pattern_is_literal_text(self) -> None:
        """A regular expression would be a different door."""
        result = _call(self.session_search("p.*ytest")).structured_content
        self.assertEqual(result["matches"], [])

    def test_the_search_can_be_narrowed_to_one_session(self) -> None:
        result = _call(self.session_search("grep", session_id="other")).structured_content
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["session_id"], "other")

    def test_an_empty_pattern_is_refused(self) -> None:
        reason = _refusal(self.session_search("  "))
        self.assertIn("empty", reason)

    def test_the_answer_is_bounded(self) -> None:
        from agent_api import remember_command  # noqa: PLC0415

        for number in range(20):
            remember_command("mcp", f"echo {number}")
        result = _call(self.session_search("echo", max_results=5)).structured_content
        self.assertEqual(len(result["matches"]), 5)
        self.assertTrue(result["truncated"])
        self.assertIn("limit", result["reason"])

    def test_more_than_the_limit_is_refused(self) -> None:
        reason = _refusal(self.session_search("echo", max_results=100000))
        self.assertIn("over the limit", reason)

    def test_the_history_is_bounded(self) -> None:
        self.app, self.tmpdir = _fresh_app(AGENT_SESSION_HISTORY_MAX_COMMANDS="3")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        from agent_api import remember_command  # noqa: PLC0415
        from mcp_server import session_search  # noqa: PLC0415

        for number in range(10):
            remember_command("mcp", f"echo {number}")
        result = _call(session_search("echo")).structured_content
        self.assertEqual(len(result["matches"]), 3)
        # The oldest commands are the ones that go.
        self.assertEqual(result["matches"][0]["command"], "echo 7")


class TestTheQuestionGoesToTheCaller(_Http):
    """The door has no channel of its own; the caller is the channel."""

    def test_the_question_is_asked_and_remembered(self) -> None:
        result = _call(self.clarify("which environment should I deploy to?"))
        self.assertTrue(result["asked"])
        self.assertEqual(result["question"], "which environment should I deploy to?")
        self.assertEqual(result["session_id"], "mcp")

        self._alive_session()
        answer = asyncio.run(self._sessions())
        record = [s for s in answer["sessions"] if s["session_id"] == "mcp"][0]
        self.assertEqual(record["question"], "which environment should I deploy to?")

    def test_an_empty_question_is_refused(self) -> None:
        reason = _refusal(self.clarify("   "))
        self.assertIn("empty", reason)


class TestTheSessionToolsLeaveATrace(_Base):
    """The counting is the audit trail."""

    def test_the_task_list_is_counted(self) -> None:
        from agent_api import METRICS  # noqa: PLC0415

        before = METRICS.render()
        _call(self.todo_list(["a step"]))
        after = METRICS.render()
        self.assertIn("sandbox_agent_todo_ops_total 0", before)
        self.assertIn("sandbox_agent_todo_ops_total 1", after)

    def test_the_memory_is_counted(self) -> None:
        from agent_api import METRICS  # noqa: PLC0415

        before = METRICS.render()
        _call(self.memory(action="add", text="a note"))
        after = METRICS.render()
        self.assertIn("sandbox_agent_memory_ops_total 0", before)
        self.assertIn("sandbox_agent_memory_ops_total 1", after)

    def test_the_recall_is_counted(self) -> None:
        from agent_api import METRICS  # noqa: PLC0415

        before = METRICS.render()
        _call(self.session_search("nothing"))
        after = METRICS.render()
        self.assertIn("sandbox_agent_session_searches_total 0", before)
        self.assertIn("sandbox_agent_session_searches_total 1", after)

    def test_the_question_is_counted(self) -> None:
        from agent_api import METRICS  # noqa: PLC0415

        before = METRICS.render()
        _call(self.clarify("a question"))
        after = METRICS.render()
        self.assertIn("sandbox_agent_clarify_total 0", before)
        self.assertIn("sandbox_agent_clarify_total 1", after)


class TestTheSessionToolsAreTheDocumentedTools(_Base):
    """The catalogue is pinned, and it moves with the code."""

    def test_the_tool_names_are_the_ones_the_code_registers(self) -> None:
        from mcp_server import tool_names  # noqa: PLC0415

        self.assertEqual(
            tool_names(),
            [
                "bash",
                "browser_close",
                "browser_open",
                "browser_screenshot",
                "clarify",
                "delete_file",
                "file_info",
                "memory",
                "patch",
                "read_file",
                "search_files",
                "session_search",
                "todo_list",
                "vision_analyze",
                "write_file",
            ],
        )

    def test_the_memory_says_it_is_not_permission(self) -> None:
        """The memory is the model's own notes, and a note is not an order.

        The boundary of this project: only the operator's words are
        executable instructions. A note the model wrote — or that
        arrived in a file the model read — is context, never
        permission.
        """
        from mcp_server import mcp  # noqa: PLC0415

        listed = {tool.name: tool for tool in mcp._tool_manager.list_tools()}  # noqa: SLF001
        description = listed["memory"].description or ""
        self.assertIn("context", description)
        self.assertIn("never permission", description)
        self.assertIn("the operator's words", description)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
