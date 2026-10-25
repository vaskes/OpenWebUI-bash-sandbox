"""Integration tests for the binary transfer endpoints.

The binary door exists because text and bytes cannot share a road: stdout
travels through a terminal pane, where it is wrapped, bounded and lossy at
the top. Bytes get their own endpoint, and this file pins the properties
that make it safe to use:

* the bytes come out **unchanged** — no base64, no re-encoding;
* the path stays **inside the workspace** — traversal and escaping symlinks
  are refused, not served;
* the size limit is **policy with a clear error**, not a silent cut;
* a ``Range`` request returns that range and nothing else.

The path checks are the security-relevant ones, so they get the most cases.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_app():
    """Reload agent_api against a fresh temp workspace."""
    tmpdir = tempfile.mkdtemp(prefix="agent-binary-test-")
    os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmpdir, "sessions.json")
    os.environ["AGENT_WORKSPACE"] = tmpdir
    os.environ["AGENT_TOKEN"] = "test-agent-token-xyz"
    if "agent_api" in sys.modules:
        del sys.modules["agent_api"]
    from agent_api import SESSIONS, app  # noqa: E402

    SESSIONS.clear()
    return app, tmpdir


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        self.app, self.tmpdir = _fresh_app()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-agent-token-xyz"}
        self.ws = Path(self.tmpdir)

    def write(self, name: str, data: bytes) -> Path:
        target = self.ws / name
        target.write_bytes(data)
        return target


class TestDownloadFile(_Base):
    def test_requires_auth(self) -> None:
        self.write("a.bin", b"hello")
        self.assertEqual(self.client.get("/files", params={"path": "a.bin"}).status_code, 401)

    def test_returns_the_exact_bytes(self) -> None:
        payload = bytes(range(256)) * 40
        self.write("blob.bin", payload)
        response = self.client.get("/files", params={"path": "blob.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, payload)

    def test_binary_payload_is_not_re_encoded(self) -> None:
        # A payload that is invalid UTF-8 must survive: no base64, no
        # decode, no replacement characters.
        payload = bytes([0xFF, 0xFE, 0x00, 0x01, 0x80, 0x81])
        self.write("raw.bin", payload)
        response = self.client.get("/files", params={"path": "raw.bin"}, headers=self.auth)
        self.assertEqual(response.content, payload)

    def test_empty_file(self) -> None:
        self.write("empty.bin", b"")
        response = self.client.get("/files", params={"path": "empty.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")

    def test_content_disposition_carries_the_name(self) -> None:
        self.write("report.bin", b"x")
        response = self.client.get("/files", params={"path": "report.bin"}, headers=self.auth)
        self.assertIn("report.bin", response.headers.get("content-disposition", ""))

    def test_non_ascii_name_is_encoded(self) -> None:
        self.write("отчёт.bin", b"x")
        response = self.client.get("/files", params={"path": "отчёт.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn("filename*=UTF-8''", response.headers.get("content-disposition", ""))

    def test_missing_file_is_404(self) -> None:
        response = self.client.get("/files", params={"path": "nope.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 404)

    def test_unreadable_file_is_403_and_not_a_truncated_200(self) -> None:
        """A file the agent cannot read must be a status, not a broken body.

        The defect this pins was found from the bot side (2026-10-25): the
        endpoint opened the file *lazily*, inside the response generator.
        A file owned by root with mode 0600 could not be opened by the
        agent (uid 1000), the 200 and the ``Content-Length`` had already
        gone out, and the client received 0 of the promised bytes. httpx
        reads that as a broken connection, and the bot reported "the
        sandbox side-car is unreachable" — blaming the network instead of
        the file's mode.

        The open therefore has to happen before the response starts. This
        test pins that: if the open moves back into the generator, the
        ``PermissionError`` escapes the response and the test fails with
        it, instead of quietly answering 403.
        """
        self.write("root-only.bin", b"secret")

        real_open = Path.open

        def fake_open(self, *args, **kwargs):
            if self.name == "root-only.bin":
                raise PermissionError(13, "Permission denied")
            return real_open(self, *args, **kwargs)

        with mock.patch.object(Path, "open", fake_open):
            response = self.client.get(
                "/files", params={"path": "root-only.bin"}, headers=self.auth
            )

        self.assertEqual(response.status_code, 403)
        detail = response.json().get("detail", "")
        self.assertIn("permission denied", detail.lower())
        # The message has to say what to do, not just that it failed.
        self.assertIn("chmod", detail.lower())

    def test_unreadable_file_with_a_range_is_also_403(self) -> None:
        """The range path opens the same file, so it must fail the same way."""
        self.write("root-only.bin", b"secret")

        real_open = Path.open

        def fake_open(self, *args, **kwargs):
            if self.name == "root-only.bin":
                raise PermissionError(13, "Permission denied")
            return real_open(self, *args, **kwargs)

        with mock.patch.object(Path, "open", fake_open):
            response = self.client.get(
                "/files",
                params={"path": "root-only.bin"},
                headers={**self.auth, "Range": "bytes=0-2"},
            )

        self.assertEqual(response.status_code, 403)

    def test_directory_is_refused(self) -> None:
        (self.ws / "subdir").mkdir()
        response = self.client.get("/files", params={"path": "subdir"}, headers=self.auth)
        self.assertEqual(response.status_code, 400)

    def test_path_outside_the_workspace_is_refused(self) -> None:
        response = self.client.get("/files", params={"path": "/etc/passwd"}, headers=self.auth)
        self.assertEqual(response.status_code, 400)

    def test_traversal_is_refused(self) -> None:
        response = self.client.get("/files", params={"path": "../../etc/passwd"}, headers=self.auth)
        self.assertEqual(response.status_code, 400)

    def test_symlink_out_of_the_workspace_is_refused(self) -> None:
        link = self.ws / "escape.bin"
        try:
            link.symlink_to("/etc/passwd")
        except OSError:
            self.skipTest("symlinks unavailable")
        response = self.client.get("/files", params={"path": "escape.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 400)

    def test_symlink_inside_the_workspace_is_served(self) -> None:
        self.write("real.bin", b"inside")
        link = self.ws / "link.bin"
        try:
            link.symlink_to(self.ws / "real.bin")
        except OSError:
            self.skipTest("symlinks unavailable")
        response = self.client.get("/files", params={"path": "link.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"inside")


class TestBinaryLimit(_Base):
    def test_file_over_the_limit_is_refused_with_the_real_size(self) -> None:
        import agent_api

        original = agent_api.BINARY_LIMIT_BYTES
        agent_api.BINARY_LIMIT_BYTES = 1024
        self.addCleanup(lambda: setattr(agent_api, "BINARY_LIMIT_BYTES", original))
        self.write("big.bin", b"x" * 4096)
        response = self.client.get("/files", params={"path": "big.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 413)
        self.assertIn("4096", response.text)

    def test_file_at_the_limit_is_served(self) -> None:
        import agent_api

        original = agent_api.BINARY_LIMIT_BYTES
        agent_api.BINARY_LIMIT_BYTES = 4096
        self.addCleanup(lambda: setattr(agent_api, "BINARY_LIMIT_BYTES", original))
        self.write("exact.bin", b"x" * 4096)
        response = self.client.get("/files", params={"path": "exact.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.content), 4096)

    def test_zero_means_no_limit(self) -> None:
        import agent_api

        original = agent_api.BINARY_LIMIT_BYTES
        agent_api.BINARY_LIMIT_BYTES = 0
        self.addCleanup(lambda: setattr(agent_api, "BINARY_LIMIT_BYTES", original))
        self.write("big.bin", b"x" * 200_000)
        response = self.client.get("/files", params={"path": "big.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.content), 200_000)


class TestRange(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.payload = bytes(range(256)) * 8
        self.write("r.bin", self.payload)

    def test_range_returns_that_slice(self) -> None:
        headers = {**self.auth, "Range": "bytes=10-19"}
        response = self.client.get("/files", params={"path": "r.bin"}, headers=headers)
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, self.payload[10:20])

    def test_range_reports_the_content_range(self) -> None:
        headers = {**self.auth, "Range": "bytes=0-9"}
        response = self.client.get("/files", params={"path": "r.bin"}, headers=headers)
        self.assertEqual(response.headers.get("content-range"), f"bytes 0-9/{len(self.payload)}")

    def test_open_ended_range(self) -> None:
        headers = {**self.auth, "Range": "bytes=2040-"}
        response = self.client.get("/files", params={"path": "r.bin"}, headers=headers)
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, self.payload[2040:])

    def test_suffix_range(self) -> None:
        headers = {**self.auth, "Range": "bytes=-16"}
        response = self.client.get("/files", params={"path": "r.bin"}, headers=headers)
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, self.payload[-16:])

    def test_unsatisfiable_range_is_416(self) -> None:
        headers = {**self.auth, "Range": "bytes=999999-"}
        response = self.client.get("/files", params={"path": "r.bin"}, headers=headers)
        self.assertEqual(response.status_code, 416)

    def test_malformed_range_is_416(self) -> None:
        headers = {**self.auth, "Range": "kilobytes=1-2"}
        response = self.client.get("/files", params={"path": "r.bin"}, headers=headers)
        self.assertEqual(response.status_code, 416)

    def test_accept_ranges_is_advertised(self) -> None:
        response = self.client.get("/files", params={"path": "r.bin"}, headers=self.auth)
        self.assertEqual(response.headers.get("accept-ranges"), "bytes")


class TestFileMeta(_Base):
    def test_reports_size_and_name(self) -> None:
        self.write("m.bin", b"x" * 1234)
        response = self.client.get("/files/meta", params={"path": "m.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["size"], 1234)
        self.assertEqual(body["name"], "m.bin")

    def test_reports_the_configured_limit(self) -> None:
        self.write("m.bin", b"x")
        response = self.client.get("/files/meta", params={"path": "m.bin"}, headers=self.auth)
        self.assertIn("limit", response.json())

    def test_requires_auth(self) -> None:
        self.write("m.bin", b"x")
        self.assertEqual(self.client.get("/files/meta", params={"path": "m.bin"}).status_code, 401)

    def test_missing_file_is_404(self) -> None:
        response = self.client.get("/files/meta", params={"path": "nope.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 404)

    def test_traversal_is_refused(self) -> None:
        response = self.client.get(
            "/files/meta", params={"path": "../../etc/passwd"}, headers=self.auth
        )
        self.assertEqual(response.status_code, 400)


class TestPurgeFile(_Base):
    """The sandbox is a scratchpad: after delivery, the copy is removed.

    Every property the download endpoint has about *paths* applies here
    too, because both go through the same resolution. A delete endpoint
    that can reach outside the workspace would be a much better denial of
    service than the download endpoint ever was.
    """

    def test_requires_auth(self) -> None:
        self.write("a.bin", b"hello")
        self.assertEqual(self.client.delete("/files", params={"path": "a.bin"}).status_code, 401)

    def test_deletes_the_file(self) -> None:
        target = self.write("doomed.bin", b"payload")
        self.assertTrue(target.exists())
        response = self.client.delete("/files", params={"path": "doomed.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 204)
        self.assertFalse(target.exists(), "the file is still on disk after a 204")

    def test_the_file_is_gone_from_the_download_endpoint(self) -> None:
        self.write("gone.bin", b"payload")
        self.client.delete("/files", params={"path": "gone.bin"}, headers=self.auth)
        response = self.client.get("/files", params={"path": "gone.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 404)

    def test_deleting_twice_is_404_not_500(self) -> None:
        self.write("twice.bin", b"payload")
        first = self.client.delete("/files", params={"path": "twice.bin"}, headers=self.auth)
        second = self.client.delete("/files", params={"path": "twice.bin"}, headers=self.auth)
        self.assertEqual(first.status_code, 204)
        self.assertEqual(second.status_code, 404)

    def test_missing_file_is_404(self) -> None:
        response = self.client.delete("/files", params={"path": "nope.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 404)

    def test_path_outside_the_workspace_is_refused(self) -> None:
        response = self.client.delete("/files", params={"path": "/etc/passwd"}, headers=self.auth)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Path("/etc/passwd").exists(), "a refused delete removed the file anyway")

    def test_traversal_is_refused(self) -> None:
        response = self.client.delete(
            "/files", params={"path": "../../etc/passwd"}, headers=self.auth
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Path("/etc/passwd").exists(), "a refused delete removed the file anyway")

    def test_directory_is_refused(self) -> None:
        (self.ws / "subdir").mkdir()
        response = self.client.delete("/files", params={"path": "subdir"}, headers=self.auth)
        self.assertEqual(response.status_code, 400)
        self.assertTrue((self.ws / "subdir").is_dir(), "a refused delete removed the directory")

    def test_symlink_out_of_the_workspace_is_refused(self) -> None:
        link = self.ws / "escape.bin"
        try:
            link.symlink_to("/etc/passwd")
        except OSError:
            self.skipTest("symlinks unavailable")
        response = self.client.delete("/files", params={"path": "escape.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Path("/etc/passwd").exists(), "a refused delete removed the target anyway")

    def test_symlink_inside_the_workspace_removes_the_target(self) -> None:
        target = self.write("real.bin", b"inside")
        link = self.ws / "link.bin"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlinks unavailable")
        response = self.client.delete("/files", params={"path": "link.bin"}, headers=self.auth)
        self.assertEqual(response.status_code, 204)
        self.assertFalse(target.exists(), "the target of the link is still there")
        self.assertTrue(link.is_symlink(), "the link itself should survive, dangling")

    def test_empty_path_is_refused(self) -> None:
        response = self.client.delete("/files", params={"path": "  "}, headers=self.auth)
        self.assertEqual(response.status_code, 400)
