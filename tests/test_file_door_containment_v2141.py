"""The file door keeps its promise: what it serves is inside the workspace.

**What the review found.** ``_safe_file`` vets a path by resolving it,
and the answer — a string — is opened later. Between the two moments a
process inside the sandbox can replace a **directory** of the path with
a symlink that points out. ``O_NOFOLLOW`` guards the last component
only, so the kernel follows the substituted directory and opens a file
outside the workspace. Measured, before this fix:

```
resolved: /tmp/toctou-…/sub/hostname
(after the parent was replaced by a symlink to /etc)
O_NOFOLLOW opened: b'24962ccbebb5\\n'   ← /etc/hostname, not the vetted file
```

The severity is bounded, and it was measured too: the bash door reads
the same bytes with the same uid (the agent's own token is visible to
``env`` in the shell), so the race grants no new capability to whoever
runs in the sandbox. What it breaks is the **promise** the door makes —
"a file inside the workspace" — and a promise that can be broken is
worse than no promise.

**The fix.** The file is opened *relative to a directory descriptor* of
the workspace, one component at a time, every component with
``O_NOFOLLOW``. A component that is a symlink fails the open instead of
being followed, so the file that comes out is the file the check
vetted — by construction, not by timing.

**The size in the answer comes from the descriptor too.** A stat of the
path and an open of the path can disagree; the bytes and the size that
describes them must not.

The tests below check the property, not the shape of the refusal: what
matters is that the bytes of the file *outside* the workspace are never
served. A refusal that names the reason is checked separately.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agent_api_endpoints import _fresh_app  # noqa: E402

OUTSIDE = b"OUTSIDE-OF-THE-WORKSPACE\n"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()
        import agent_api  # noqa: PLC0415

        self.agent_api = agent_api
        self.outside = tempfile.mkdtemp(prefix="agent-outside-")
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)
        # The file the swapped directory would point at. Its name is the
        # name of the file inside the workspace, so a door that follows
        # the swap serves these bytes instead of the vetted ones.
        with open(os.path.join(self.outside, "inside.txt"), "wb") as handle:
            handle.write(OUTSIDE)

    def _write(self, relative: str, content: str) -> str:
        path = os.path.join(self.workspace_real, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def _served(self, resolved: str) -> bytes | None:
        """The bytes the door would serve, or None if it refused."""
        try:
            handle = self.agent_api._open_for_download(resolved)
        except Exception:  # noqa: BLE001 - a refusal is a valid answer
            return None
        with handle:
            return handle.read()


class TestASwappedDirectoryIsNotFollowed(_Base):
    """The race, reproduced deterministically instead of hopefully."""

    def test_the_file_outside_the_workspace_is_not_served(self) -> None:
        self._write("sub/inside.txt", "INSIDE\n")
        resolved = self.agent_api._safe_file("sub/inside.txt")
        self.assertTrue(resolved.startswith(self.agent_api.WORKSPACE_PREFIX))

        # The attacker swaps the directory for a symlink that points out.
        target = os.path.join(self.workspace_real, "sub")
        shutil.rmtree(target)
        os.symlink(self.outside, target)

        served = self._served(resolved)
        self.assertNotIn(OUTSIDE, served or b"")

    def test_the_refusal_says_the_file_was_replaced(self) -> None:
        self._write("sub/inside.txt", "INSIDE\n")
        resolved = self.agent_api._safe_file("sub/inside.txt")
        target = os.path.join(self.workspace_real, "sub")
        shutil.rmtree(target)
        os.symlink(self.outside, target)

        with self.assertRaises(Exception) as ctx:
            self.agent_api._open_for_download(resolved)
        self.assertIn("replaced while it was being opened", str(ctx.exception))

    def test_a_swapped_file_is_not_followed_either(self) -> None:
        # The case v2.12.1 closed: the last component itself turns into
        # a symlink. It must keep failing the same way.
        path = self._write("inside.txt", "INSIDE\n")
        resolved = self.agent_api._safe_file("inside.txt")
        os.unlink(path)
        os.symlink(os.path.join(self.outside, "inside.txt"), path)

        served = self._served(resolved)
        self.assertNotIn(OUTSIDE, served or b"")

    def test_a_swapped_workspace_root_is_not_followed(self) -> None:
        # The root itself is a mount point in the deployment, but it is
        # an ordinary directory in a test, and the guard must not depend
        # on which one it is.
        self._write("inside.txt", "INSIDE\n")
        resolved = self.agent_api._safe_file("inside.txt")
        root = self.workspace_real
        moved = root + "-moved"
        os.rename(root, moved)
        os.symlink(self.outside, root)

        def restore() -> None:
            if os.path.islink(root):
                os.unlink(root)
            if os.path.isdir(moved):
                os.rename(moved, root)

        self.addCleanup(restore)

        served = self._served(resolved)
        self.assertNotIn(OUTSIDE, served or b"")

    def test_the_file_inside_is_still_served(self) -> None:
        # The guard must not turn the door into one that serves nothing.
        self._write("sub/inside.txt", "INSIDE\n")
        resolved = self.agent_api._safe_file("sub/inside.txt")
        self.assertEqual(self._served(resolved), b"INSIDE\n")


class TestTheOpenIsRelativeToTheWorkspace(_Base):
    """The guard is the mechanism, not a flag on a path."""

    def test_the_open_uses_a_directory_descriptor(self) -> None:
        source = (ROOT / "agent_api.py").read_text(encoding="utf-8")
        block = source.split("def _open_inside_workspace", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("dir_fd=dir_fd", block)

    def test_every_component_is_opened_without_following(self) -> None:
        source = (ROOT / "agent_api.py").read_text(encoding="utf-8")
        block = source.split("def _open_inside_workspace", 1)[1].split("\ndef ", 1)[0]
        opens = [line for line in block.splitlines() if "os.open(" in line]
        self.assertTrue(opens, "the helper opens nothing")
        for line in opens:
            self.assertIn("os.O_NOFOLLOW", line, f"an open without O_NOFOLLOW: {line.strip()}")


class TestTheSizeDescribesTheBytes(_Base):
    """The size in the answer is the size of the file that was read."""

    def test_the_size_comes_from_the_descriptor(self) -> None:
        import mcp_server  # noqa: PLC0415

        self._write("sized.txt", "0123456789")
        real_getsize = os.path.getsize
        # The stat of the path and the open of the path can disagree: a
        # name can be pointed at another file in between. The size that
        # goes into the answer has to be the size of the bytes.
        os.path.getsize = lambda path: 999999
        try:
            result = _call_read_file(mcp_server, "sized.txt")
        finally:
            os.path.getsize = real_getsize

        self.assertEqual(result.structured_content["size"], 10)
        self.assertIn("0123456789", result.content[0].text)


def _call_read_file(mcp_server, path: str):
    """Call the MCP ``read_file`` tool and return its result."""
    import asyncio  # noqa: PLC0415

    from mcp_server import read_file  # noqa: PLC0415

    return asyncio.run(read_file(path))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
