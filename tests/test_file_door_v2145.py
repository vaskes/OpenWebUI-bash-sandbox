"""The HTTP file door: the answer must describe the bytes it serves.

**What was wrong.** The download endpoint measured the file by its
*path* and then opened it by the same path, and the bytes came from
the descriptor.  Anything that changed the file in between — a writer,
a rename, a swap — left the answer promising N bytes and the body
carrying M.  The MCP door takes the size from the descriptor already;
the HTTP door did not.

**What the fix does.** The file is opened first, and the size is taken
from the descriptor the bytes are read from.  Both happen before any
header goes out, so a file the agent cannot read is still a 403 the
client can act on.
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


class TestTheSizeDescribesTheBytes(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-agent-token-xyz"}
        import agent_api

        self.agent_api = agent_api

    def test_the_size_is_taken_from_the_descriptor(self) -> None:
        """The file grows between the measurement and the reading.

        The wrapper below appends to the file as it is opened — the
        moment the old code measured it by path, the answer promised
        the old size.  The size now comes from the descriptor, so the
        answer describes the bytes that are actually served.
        """
        from unittest.mock import patch  # noqa: PLC0415

        path = os.path.join(self.tmpdir, "grows.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("0123456789")

        real_open = self.agent_api._open_for_download

        def open_and_grow(name: str):
            handle = real_open(name)
            with open(path, "a", encoding="utf-8") as grown:
                grown.write("ABCDE")
            return handle

        with patch.object(self.agent_api, "_open_for_download", open_and_grow):
            response = self.client.get("/files?path=grows.txt", headers=self.auth)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.content
        self.assertEqual(
            int(response.headers["Content-Length"]),
            len(body),
            "the answer promises a size the body does not have",
        )
        self.assertEqual(int(response.headers["X-File-Size"]), len(body))
        self.assertEqual(body, b"0123456789ABCDE")

    def test_the_ordinary_case_still_works(self) -> None:
        path = os.path.join(self.tmpdir, "plain.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("hello")
        response = self.client.get("/files?path=plain.txt", headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, b"hello")
        self.assertEqual(int(response.headers["Content-Length"]), 5)


if __name__ == "__main__":
    unittest.main()
