"""The file tools — ``write_file``, ``patch``, ``search_files`` — and the fence.

Before this wave the MCP surface had no write, no patch and no search:
the only way to put a file in the workspace was ``bash`` (``cat > …``),
and that path leaves nothing in the file door's own accounting. The
three tools close that gap, and the gap is only closed if the fence
holds — so most of this file is about the fence.

What is pinned here:

* a write outside the workspace is refused, and the file outside is not
  touched;
* a symlink that points out of the workspace is refused, both as the
  name itself and as a directory on the way to it;
* a write makes the directories it needs, replaces the file that is
  there, and is visible to ``read_file``;
* a patch that does not apply cleanly is refused **and changes
  nothing** — the property the tool is worth;
* the diff is applied here, in this process: a diff of two files, a
  diff without hunks and a hunk whose counts do not match are all
  refused;
* a search never leaves the workspace: a path outside it is refused
  with a reason, and a symlink that points out is not read;
* the search is bounded, and it says so when it stopped early;
* the operations are counted, because the counting is the audit trail
  this wave exists to restore.

And a check that cannot be made red is a decoration:
``tools/mutation-check-v2220.sh`` breaks each of these on purpose and
expects the named test to go red.
"""

from __future__ import annotations

import asyncio
import atexit
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
    """Reload the agent against a temp workspace and the given environment.

    Every knob is read from the environment once, at import time — so a
    module that is reloaded against a new environment needs the config
    reloaded with it.
    """
    tmpdir = tempfile.mkdtemp(prefix="agent-file-tools-test-")
    atexit.register(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
    os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmpdir, "sessions.json")
    os.environ["AGENT_WORKSPACE"] = tmpdir
    os.environ["AGENT_TOKEN"] = TOKEN
    os.environ.pop("AGENT_MCP_ALLOWED_HOSTS", None)
    for name, value in environment.items():
        os.environ[name] = value
    for name in ("agent_api", "mcp_server", "agent_config"):
        if name in sys.modules:
            del sys.modules[name]
    from agent_api import SESSIONS, app  # noqa: E402

    SESSIONS.clear()
    return app, tmpdir


def _call(coro):
    """Run one tool call and return its result."""
    return asyncio.run(coro)


def _refusal(coro) -> str:
    """Run one tool call that must be refused, and return the reason."""
    with unittest.TestCase().assertRaises(Exception) as caught:
        _call(coro)
    return str(caught.exception)


class _Base(unittest.TestCase):
    """A fresh agent per test, with the tools loaded against it."""

    def setUp(self) -> None:
        self.app, self.tmpdir = _fresh_app()
        self.ws = Path(self.tmpdir)
        from mcp_server import patch, search_files, write_file  # noqa: PLC0415

        self.write_file = write_file
        self.patch = patch
        self.search_files = search_files

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def put(self, name: str, data: str) -> Path:
        """Put a file into the workspace directly, as the model would."""
        target = self.ws / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data, encoding="utf-8")
        return target


class TestTheWriteStaysInsideTheWorkspace(_Base):
    """A write is a write inside the workspace, and nowhere else."""

    def test_a_write_outside_the_workspace_is_refused(self) -> None:
        # A sibling of the workspace: outside the perimeter, and in a
        # directory this test may write into if a mutation lets it
        # through — so a broken fence shows up as a file, not as damage
        # to the machine.
        outside = self.ws.parent / f"owbs-outside-{os.getpid()}.txt"
        self.addCleanup(outside.unlink, True)
        reason = _refusal(self.write_file(str(outside), "pwned"))
        self.assertIn("outside the workspace", reason)
        # The refusal names the path the caller asked for: a refusal
        # that names something else is a refusal the model cannot act on.
        self.assertIn(str(outside), reason)
        self.assertFalse(outside.exists())

    def test_a_write_through_a_symlink_that_points_out_is_refused(self) -> None:
        """The name itself can be the symlink.

        The check resolves the name before it is used, so a link that
        points out of the workspace is refused rather than followed.
        """
        outside = self.ws.parent / f"outside-{os.getpid()}.txt"
        outside.write_text("untouched", encoding="utf-8")
        self.addCleanup(outside.unlink, True)
        os.symlink(outside, self.ws / "link.txt")

        reason = _refusal(self.write_file("link.txt", "pwned"))
        self.assertIn("outside the workspace", reason)
        # The refusal names the path the caller asked for: a refusal
        # that names something else is a refusal the model cannot act on.
        self.assertIn("link.txt", reason)
        self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")

    def test_a_write_through_a_symlinked_directory_is_refused(self) -> None:
        """A directory on the way can be the symlink."""
        outside = self.ws.parent / f"outside-dir-{os.getpid()}"
        outside.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, outside, True)
        os.symlink(outside, self.ws / "linkdir")

        reason = _refusal(self.write_file("linkdir/owbs-owned", "pwned"))
        self.assertIn("outside the workspace", reason)
        self.assertIn("linkdir/owbs-owned", reason)
        self.assertFalse((outside / "owbs-owned").exists())

    def test_a_write_makes_the_directories_it_needs(self) -> None:
        """Otherwise the first write of every task needs a shell first."""
        _call(self.write_file("notes/deep/report.md", "hello"))
        self.assertEqual((self.ws / "notes" / "deep" / "report.md").read_text(), "hello")

    def test_a_write_replaces_the_file_that_is_there(self) -> None:
        """This is the model's scratchpad, not storage."""
        self.put("notes.txt", "old")
        _call(self.write_file("notes.txt", "new"))
        self.assertEqual((self.ws / "notes.txt").read_text(), "new")

    def test_a_write_is_visible_to_read_file(self) -> None:
        """The wave's own promise: the model writes through the fence."""
        _call(self.write_file("notes/visible.txt", "visible"))
        from agent_api import _safe_file  # noqa: PLC0415

        target = _safe_file("notes/visible.txt")
        self.assertEqual(Path(target).read_text(), "visible")

    def test_a_write_over_the_limit_is_refused(self) -> None:
        from agent_config import model_write_limit  # noqa: PLC0415

        limit = model_write_limit()
        reason = _refusal(self.write_file("big.txt", "x" * (limit + 1)))
        self.assertIn("over the write limit", reason)
        self.assertFalse((self.ws / "big.txt").exists())

    def test_a_write_to_a_directory_is_refused(self) -> None:
        (self.ws / "adir").mkdir()
        reason = _refusal(self.write_file("adir", "x"))
        self.assertIn("directory", reason)

    def test_a_write_with_no_name_is_refused(self) -> None:
        reason = _refusal(self.write_file("", "x"))
        self.assertIn("path is required", reason)


class TestThePatchChangesNothingWhenItDoesNotApply(_Base):
    """A patch either applies or it does not happen."""

    def test_a_patch_that_does_not_apply_is_refused(self) -> None:
        self.put("code.py", "one\ntwo\nthree\n")
        reason = _refusal(
            self.patch("code.py", "@@ -1,2 +1,2 @@\n one\n-nope\n+yes\n"),
        )
        self.assertIn("does not apply", reason)

    def test_the_file_is_untouched_after_a_refused_patch(self) -> None:
        """The property the tool is worth."""
        self.put("code.py", "one\ntwo\nthree\n")
        _refusal(self.patch("code.py", "@@ -1,2 +1,2 @@\n one\n-nope\n+yes\n"))
        self.assertEqual((self.ws / "code.py").read_text(), "one\ntwo\nthree\n")

    def test_a_patch_applies_cleanly(self) -> None:
        self.put("code.py", "one\ntwo\nthree\n")
        _call(self.patch("code.py", "@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"))
        self.assertEqual((self.ws / "code.py").read_text(), "one\nTWO\nthree\n")

    def test_a_patch_outside_the_workspace_is_refused(self) -> None:
        outside = self.ws.parent / f"owbs-outside-{os.getpid()}.txt"
        outside.write_text("untouched\n", encoding="utf-8")
        self.addCleanup(outside.unlink, True)
        reason = _refusal(self.patch(str(outside), "@@ -1,1 +1,1 @@\n-untouched\n+touched\n"))
        self.assertIn("outside the workspace", reason)
        self.assertEqual(outside.read_text(encoding="utf-8"), "untouched\n")

    def test_a_patch_of_a_file_that_is_not_there_is_refused(self) -> None:
        reason = _refusal(self.patch("nowhere.py", "@@ -1,1 +1,1 @@\n-a\n+b\n"))
        self.assertIn("not found", reason)

    def test_a_patch_of_a_binary_file_is_refused(self) -> None:
        (self.ws / "blob.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
        reason = _refusal(self.patch("blob.bin", "@@ -1,1 +1,1 @@\n-a\n+b\n"))
        self.assertIn("not text", reason)

    def test_a_diff_of_two_files_is_refused(self) -> None:
        self.put("one.py", "a\n")
        self.put("two.py", "b\n")
        diff = "@@ -1,1 +1,1 @@\n-a\n+A\ndiff --git a/two.py b/two.py\n@@ -1,1 +1,1 @@\n-b\n+B\n"
        reason = _refusal(self.patch("one.py", diff))
        self.assertIn("more than one file", reason)
        self.assertEqual((self.ws / "one.py").read_text(), "a\n")
        self.assertEqual((self.ws / "two.py").read_text(), "b\n")

    def test_a_diff_without_hunks_is_refused(self) -> None:
        self.put("one.py", "a\n")
        reason = _refusal(self.patch("one.py", "--- a/one.py\n+++ b/one.py\n"))
        self.assertIn("no hunks", reason)


class TestTheSearchStaysInsideTheWorkspace(_Base):
    """The search is a door with a window, not a hole in the wall."""

    def test_a_search_over_a_path_outside_the_workspace_is_refused(self) -> None:
        outside = self.ws.parent / f"owbs-outside-dir-{os.getpid()}"
        outside.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "secret.txt").write_text("zaphod\n", encoding="utf-8")
        reason = _refusal(self.search_files("zaphod", path=str(outside)))
        self.assertIn("outside the workspace", reason)

    def test_the_refusal_says_why(self) -> None:
        """A search that returns nothing for a forbidden path looks like
        "there is nothing there" — and the model would believe it."""
        outside = self.ws.parent / f"owbs-outside-dir-{os.getpid()}"
        outside.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, outside, True)
        reason = _refusal(self.search_files("zaphod", path=str(outside)))
        self.assertIn(str(outside), reason)

    def test_a_search_does_not_read_through_a_symlink_that_points_out(self) -> None:
        outside = self.ws.parent / f"outside-secret-{os.getpid()}.txt"
        outside.write_text("the secret word is zaphod\n", encoding="utf-8")
        self.addCleanup(outside.unlink, True)
        os.symlink(outside, self.ws / "link.txt")

        result = _call(self.search_files("zaphod"))
        self.assertEqual(result.structured_content["matches"], [])

    def test_a_search_finds_by_content(self) -> None:
        self.put("notes.txt", "alpha\nbeta\n")
        result = _call(self.search_files("beta"))
        matches = result.structured_content["matches"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["path"], "notes.txt")
        self.assertEqual(matches[0]["line"], 2)

    def test_a_search_finds_by_name(self) -> None:
        self.put("src/app.py", "x = 1\n")
        self.put("src/readme.md", "hello\n")
        result = _call(self.search_files("*.py", kind="name"))
        paths = [match["path"] for match in result.structured_content["matches"]]
        self.assertEqual(paths, ["src/app.py"])

    def test_a_search_is_bounded_and_says_so(self) -> None:
        for index in range(5):
            self.put(f"many{index}.txt", "needle\n")
        result = _call(self.search_files("needle", max_results=2))
        self.assertEqual(len(result.structured_content["matches"]), 2)
        self.assertTrue(result.structured_content["truncated"])
        self.assertIn("stopped", result.structured_content["reason"])
        self.assertIn("stopped", result.content[0].text)

    def test_a_search_skips_a_file_over_the_read_limit(self) -> None:
        """A file the read door would refuse is a file the search does
        not read either — the same budget, the same rule."""
        self.app, self.tmpdir = _fresh_app(
            AGENT_MCP_MAX_READ_BYTES="1024",
            AGENT_BINARY_LIMIT="0",
        )
        self.ws = Path(self.tmpdir)
        from mcp_server import search_files  # noqa: PLC0415

        (self.ws / "big.txt").write_text("needle\n" + "x" * 4096, encoding="utf-8")
        (self.ws / "small.txt").write_text("needle\n", encoding="utf-8")
        result = _call(search_files("needle"))
        paths = [match["path"] for match in result.structured_content["matches"]]
        self.assertEqual(paths, ["small.txt"])
        self.assertEqual(result.structured_content["files_skipped"], 1)

    def test_an_unknown_kind_is_refused(self) -> None:
        reason = _refusal(self.search_files("x", kind="magic"))
        self.assertIn("unknown kind", reason)

    def test_an_empty_pattern_is_refused(self) -> None:
        reason = _refusal(self.search_files(""))
        self.assertIn("pattern is empty", reason)


class TestTheDiffApplier(unittest.TestCase):
    """The applier itself — a pure function, and the sharpest edge here."""

    def setUp(self) -> None:
        from agent_core import PatchError, apply_unified_diff  # noqa: PLC0415

        self.apply = apply_unified_diff
        self.PatchError = PatchError

    def test_a_hunk_that_does_not_match_is_refused(self) -> None:
        with self.assertRaises(self.PatchError) as caught:
            self.apply("one\ntwo\n", "@@ -1,1 +1,1 @@\n-nope\n+yes\n")
        self.assertIn("does not apply", caught.exception.reason)

    def test_the_no_newline_marker_is_honoured(self) -> None:
        result = self.apply(
            "one\ntwo",
            "@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n\\ No newline at end of file\n",
        )
        self.assertEqual(result, "one\nTWO")

    def test_a_diff_of_two_files_is_refused(self) -> None:
        with self.assertRaises(self.PatchError) as caught:
            self.apply(
                "one\n",
                "@@ -1,1 +1,1 @@\n-one\n+ONE\ndiff --git a/b b/b\n@@ -1,1 +1,1 @@\n-x\n+y\n",
            )
        self.assertIn("more than one file", caught.exception.reason)

    def test_a_hunk_whose_counts_do_not_match_is_refused(self) -> None:
        with self.assertRaises(self.PatchError) as caught:
            self.apply("one\n", "@@ -1,2 +1,2 @@\n-one\n+ONE\n")
        self.assertIn("hunk header", caught.exception.reason)

    def test_a_diff_without_hunks_is_refused(self) -> None:
        with self.assertRaises(self.PatchError) as caught:
            self.apply("one\n", "--- a/x\n+++ b/x\n")
        self.assertIn("no hunks", caught.exception.reason)

    def test_an_insertion_at_the_top_applies(self) -> None:
        self.assertEqual(
            self.apply("one\n", "@@ -0,0 +1,1 @@\n+zero\n"),
            "zero\none\n",
        )


class TestTheFileToolsLeaveATrace(_Base):
    """The audit trail the wave exists to restore."""

    def test_a_write_is_counted(self) -> None:
        from agent_api import METRICS  # noqa: PLC0415

        before = METRICS.render()
        _call(self.write_file("counted.txt", "x"))
        after = METRICS.render()
        self.assertIn("sandbox_agent_file_writes_total 0", before)
        self.assertIn("sandbox_agent_file_writes_total 1", after)

    def test_a_refusal_is_counted(self) -> None:
        from agent_api import METRICS  # noqa: PLC0415

        outside = self.ws.parent / f"owbs-outside-{os.getpid()}.txt"
        self.addCleanup(outside.unlink, True)
        _refusal(self.write_file(str(outside), "pwned"))
        self.assertIn("sandbox_agent_file_ops_refused_total 1", METRICS.render())

    def test_a_search_is_counted(self) -> None:
        from agent_api import METRICS  # noqa: PLC0415

        _call(self.search_files("nothing"))
        self.assertIn("sandbox_agent_file_searches_total 1", METRICS.render())


class TestTheToolsAreTheDocumentedTools(_Base):
    """The catalogue is pinned in three places, and they move together."""

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

    def test_the_door_advertises_them(self) -> None:
        """The handshake is the contract: what the model sees is what
        the server has."""
        tools = self.app.router.routes  # the route table is enough to find the app
        self.assertTrue(tools)
        from mcp_server import mcp  # noqa: PLC0415

        listed = [tool.name for tool in mcp._tool_manager.list_tools()]  # noqa: SLF001
        self.assertEqual(
            sorted(listed),
            sorted(
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
                ]
            ),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
