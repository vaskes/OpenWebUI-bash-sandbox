"""The door takes files in as well as out (v2.15.0).

The door opened outwards only: ``GET /files`` handed bytes out,
``DELETE /files`` threw them away, and nothing ever came in. The chat
could not hand the sandbox a file -- and the operator's case of
2026-10-29 is exactly that: a binary uploaded to the chat, with the
instruction to put it in the sandbox, and a bot that could only answer
that the file was too large.

What is pinned here:

* the bytes that land are the bytes that were sent, and the answer
  describes what landed;
* the write is atomic -- a refused upload leaves nothing behind, not
  even a temporary file;
* the door never overwrites: a file that is already there stays exactly
  as it was, because a delivery that removes what it was not told to
  remove is a surprise;
* the refusals say what happened -- no token, an empty body, a
  directory that is not there, a file over the limit, a path outside
  the workspace.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agent_api_endpoints import _fresh_app  # noqa: E402


class TestTheDoorTakesFilesIn(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-agent-token-xyz"}
        import agent_api

        self.agent_api = agent_api

    def test_the_bytes_land_and_can_be_read_back(self) -> None:
        payload = b"\x00\x01binary\xff payload\n"

        response = self.client.post(
            "/files", params={"path": "hello.bin"}, content=payload, headers=self.auth
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["name"], "hello.bin")
        self.assertEqual(body["size"], len(payload))
        self.assertTrue(body["path"].endswith("/hello.bin"))

        back = self.client.get("/files", params={"path": "hello.bin"}, headers=self.auth)
        self.assertEqual(back.status_code, 200, back.text)
        self.assertEqual(back.content, payload)

    def test_the_file_is_where_the_client_asked(self) -> None:
        os.mkdir(os.path.join(self.tmpdir, "inbox"))

        response = self.client.post(
            "/files", params={"path": "inbox/deep.bin"}, content=b"x", headers=self.auth
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(
            os.path.isfile(os.path.join(self.tmpdir, "inbox", "deep.bin")),
            "the file did not land in the directory the client named",
        )

    def test_a_logical_workspace_path_is_the_same_place(self) -> None:
        """The model knows the workspace by its logical path.

        In production the logical root (``/workspace``) and the real one
        are not the same directory -- the workspace is a bind mount and
        the container's view of it is symlinked. The door has to take
        the logical path and write into the real one.
        """
        logical = os.path.abspath(self.agent_api.WORKSPACE)

        response = self.client.post(
            "/files", params={"path": f"{logical}/logical.bin"}, content=b"x", headers=self.auth
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(os.path.isfile(os.path.join(self.tmpdir, "logical.bin")))

    def test_no_temporary_file_is_left_behind(self) -> None:
        self.client.post("/files", params={"path": "clean.bin"}, content=b"x", headers=self.auth)

        leftovers = [name for name in os.listdir(self.tmpdir) if name.startswith(".")]
        self.assertEqual(leftovers, [], f"the upload left {leftovers!r} behind")

    def test_a_refused_token_is_refused(self) -> None:
        response = self.client.post("/files", params={"path": "nope.bin"}, content=b"x")

        self.assertEqual(response.status_code, 401)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "nope.bin")))

    def test_an_empty_body_is_refused(self) -> None:
        response = self.client.post(
            "/files", params={"path": "empty.bin"}, content=b"", headers=self.auth
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "empty.bin")))

    def test_a_missing_directory_is_refused(self) -> None:
        response = self.client.post(
            "/files", params={"path": "nowhere/file.bin"}, content=b"x", headers=self.auth
        )

        self.assertEqual(response.status_code, 404)
        self.assertIn("no such directory", response.json()["detail"])

    def test_an_existing_file_is_not_overwritten(self) -> None:
        target = os.path.join(self.tmpdir, "taken.bin")
        with open(target, "wb") as handle:
            handle.write(b"original")

        response = self.client.post(
            "/files", params={"path": "taken.bin"}, content=b"replacement", headers=self.auth
        )

        self.assertEqual(response.status_code, 409)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"original")

    def test_a_file_over_the_limit_is_refused_with_its_size(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        payload = b"x" * 2048
        with patch.object(self.agent_api, "BINARY_LIMIT_BYTES", 1024):
            response = self.client.post(
                "/files", params={"path": "big.bin"}, content=payload, headers=self.auth
            )

        self.assertEqual(response.status_code, 413)
        detail = response.json()["detail"]
        self.assertIn(str(len(payload)), detail)
        self.assertIn("1024", detail)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "big.bin")))
        leftovers = [name for name in os.listdir(self.tmpdir) if name.startswith(".")]
        self.assertEqual(leftovers, [], f"the refused upload left {leftovers!r} behind")

    def test_the_limit_is_enforced_without_a_content_length(self) -> None:
        """A header is a claim; the stream is the truth.

        The size check that runs before the first byte is read trusts
        ``Content-Length``. A body that does not carry one -- a chunked
        upload, a client that lies about the length -- has to be caught
        by the check that runs while the bytes arrive, or the limit is
        only a limit on clients that behave.
        """
        from unittest.mock import patch  # noqa: PLC0415

        def chunks():
            yield b"x" * 1024
            yield b"x" * 1024
            yield b"x" * 1024

        with patch.object(self.agent_api, "BINARY_LIMIT_BYTES", 1024):
            response = self.client.post(
                "/files", params={"path": "streamed.bin"}, content=chunks(), headers=self.auth
            )

        self.assertEqual(response.status_code, 413)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "streamed.bin")))

    def test_a_path_outside_the_workspace_is_refused(self) -> None:
        # The name is unique to this test: the path leaves the workspace
        # upwards, and ".." is shared by every test that runs here.
        outside = f"escape-{os.path.basename(self.tmpdir)}.bin"

        response = self.client.post(
            "/files", params={"path": f"../{outside}"}, content=b"x", headers=self.auth
        )

        self.assertIn(response.status_code, (400, 404))
        self.assertFalse(
            os.path.exists(os.path.join(self.tmpdir, "..", outside)),
            "the upload wrote outside the workspace",
        )

    def test_a_directory_is_not_a_file_name(self) -> None:
        response = self.client.post(
            "/files", params={"path": "/workspace/"}, content=b"x", headers=self.auth
        )

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
