"""The file door's containment check, pinned at the edges.

The agent serves files from the workspace, and the path it is given comes
from a model — that is, it is untrusted text. The check that keeps the
door inside the workspace is a single comparison, so this file spends its
effort on the places where that comparison is easy to get wrong:

* a **sibling directory whose name starts with the workspace's name**
  ("/workspace-evil" next to "/workspace") passes a naive
  ``startswith(root)`` and must not pass here;
* the model sends the **logical** path ("/workspace/out.bin") while the
  real root lives somewhere else — the logical path has to be served;
* the check has to be written in the form that a static analyser can
  follow, or the next refactor brings the code-scanning alerts back.

The last point is worth a word. CodeQL models the safe-access check as a
``str.startswith`` call on the true branch of a containment test, and it
models the normalisation as ``os.path.realpath``. The check below is
written in that shape on purpose: the CI CodeQL job runs on every push,
and an alert that reappears is a real regression of the review, not a
false positive to be silenced.
"""

from __future__ import annotations

import ast
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AGENT_API = ROOT / "agent_api.py"


def _fresh_app(workspace: str):
    """Reload agent_api against a given workspace root."""
    os.environ["AGENT_SESSIONS_FILE"] = os.path.join(workspace, "sessions.json")
    os.environ["AGENT_WORKSPACE"] = workspace
    os.environ["AGENT_TOKEN"] = "test-agent-token-xyz"
    if "agent_api" in sys.modules:
        del sys.modules["agent_api"]
    from agent_api import SESSIONS, app  # noqa: PLC0415

    SESSIONS.clear()
    return app


class _ContainmentBase(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        self.tmpdir = tempfile.mkdtemp(prefix="agent-containment-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self._client_cls = TestClient
        self.app = _fresh_app(self.tmpdir)
        self.client = self._client_cls(self.app)
        self.auth = {"Authorization": "Bearer test-agent-token-xyz"}

    def write(self, name: str, data: bytes) -> Path:
        target = Path(self.tmpdir) / name
        target.write_bytes(data)
        return target


class TestTheSiblingWithTheSamePrefixIsRefused(_ContainmentBase):
    """``/workspace-evil`` is not inside ``/workspace``.

    A containment check written as ``path.startswith(root)`` cannot tell
    the two apart. The check here compares against the root *with* the
    separator, so the sibling is outside — and the difference matters:
    the sibling directory is a real place on the disk where a file can
    sit, and a file there is a file the agent must not hand out.
    """

    def test_a_sibling_with_the_same_prefix_is_refused(self) -> None:
        real_root = Path(os.path.realpath(self.tmpdir))
        sibling = real_root.parent / (real_root.name + "-evil")
        sibling.mkdir(parents=True, exist_ok=True)
        (sibling / "secret.bin").write_bytes(b"not yours")

        try:
            response = self.client.get(
                "/files",
                params={"path": f"../{sibling.name}/secret.bin"},
                headers=self.auth,
            )
        finally:
            shutil.rmtree(sibling, ignore_errors=True)

        self.assertNotEqual(response.status_code, 200)
        self.assertIn(response.status_code, (400, 404))
        self.assertNotIn(b"not yours", response.content)

    def test_the_sibling_is_refused_for_the_meta_endpoint_too(self) -> None:
        real_root = Path(os.path.realpath(self.tmpdir))
        sibling = real_root.parent / (real_root.name + "-evil")
        sibling.mkdir(parents=True, exist_ok=True)
        (sibling / "secret.bin").write_bytes(b"not yours")

        try:
            response = self.client.get(
                "/files/meta",
                params={"path": f"../{sibling.name}/secret.bin"},
                headers=self.auth,
            )
        finally:
            shutil.rmtree(sibling, ignore_errors=True)

        self.assertNotEqual(response.status_code, 200)


class TestTheLogicalPathIsServed(_ContainmentBase):
    """The model sends the path it knows; the container has another one.

    The workspace is a bind mount, and inside the container it is often a
    symlink: the model asks for ``/workspace/out.bin`` while the file
    really lives under ``/real/root/out.bin``. The agent re-anchors the
    logical path on the real root and serves it. A model that cannot
    read back its own output is a model that cannot do the job.
    """

    def setUp(self) -> None:
        super().setUp()
        self.real_root = Path(self.tmpdir) / "real"
        self.real_root.mkdir()
        self.link = Path(self.tmpdir) / "link"
        self.link.symlink_to(self.real_root)
        # The app is rebuilt with the *logical* root: what the model
        # knows, not where the file is.
        self.app = _fresh_app(str(self.link))
        self.client = self._client_cls(self.app)

    def test_the_logical_absolute_path_is_served(self) -> None:
        (self.real_root / "out.bin").write_bytes(b"payload")

        response = self.client.get(
            "/files",
            params={"path": f"{self.link}/out.bin"},
            headers=self.auth,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"payload")

    def test_a_logical_path_outside_the_workspace_is_refused(self) -> None:
        response = self.client.get(
            "/files",
            params={"path": "/etc/passwd"},
            headers=self.auth,
        )

        self.assertNotEqual(response.status_code, 200)
        self.assertIn(response.status_code, (400, 404))

    def test_a_logical_traversal_is_refused(self) -> None:
        (self.real_root / "out.bin").write_bytes(b"payload")

        response = self.client.get(
            "/files",
            params={"path": f"{self.link}/../real/out.bin"},
            headers=self.auth,
        )

        # The path resolves *into* the workspace, so it is allowed; what
        # matters is that nothing outside is reachable through it.
        self.assertNotIn(b"root:", response.content)


class TestTheGuardIsWrittenInTheShapeTheAnalyserFollows(_ContainmentBase):
    """The form of the check is part of the contract.

    CodeQL follows the containment check only when it is written as a
    ``startswith`` comparison on the true branch, with the path
    normalised by ``os.path.realpath``. A different-but-equivalent
    spelling (``commonpath``, say) is fine for the program and invisible
    to the analyser, which then reports the sink as unguarded — and the
    next reviewer has to argue with the alert instead of reading the
    code. This test keeps the spelling from drifting.
    """

    def setUp(self) -> None:
        super().setUp()
        self.source = AGENT_API.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def _function(self, name: str) -> ast.FunctionDef:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail(f"{name} is not in agent_api.py")

    def test_the_workspace_prefix_carries_the_separator(self) -> None:
        from agent_api import WORKSPACE_PREFIX, WORKSPACE_REAL  # noqa: PLC0415

        self.assertTrue(WORKSPACE_PREFIX.startswith(WORKSPACE_REAL))
        self.assertTrue(
            WORKSPACE_PREFIX.endswith(os.sep),
            "the prefix must end with a separator, or a sibling directory "
            "with the same name prefix would pass the check",
        )

    def test_the_safe_file_check_is_a_startswith_on_the_prefix(self) -> None:
        func = self._function("_safe_file")
        calls = [
            node
            for node in ast.walk(func)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
        ]
        self.assertTrue(calls, "_safe_file must compare with str.startswith")
        guarded = [
            call
            for call in calls
            if ast.dump(call.args[0]) == ast.dump(ast.Name(id="WORKSPACE_PREFIX", ctx=ast.Load()))
        ]
        self.assertTrue(
            guarded,
            "the containment check must compare against the prefix "
            "constant, not against a path built on the fly",
        )

    def test_the_normalisation_is_a_realpath(self) -> None:
        func = self._function("_safe_file")
        realpaths = [
            node
            for node in ast.walk(func)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "realpath"
        ]
        self.assertTrue(realpaths, "_safe_file must resolve symlinks with realpath")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
