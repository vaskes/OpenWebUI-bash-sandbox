"""The file door: the policy is checked before the file is opened.

**What was wrong.** Two things about the order of operations in the
download door, found by the third-wave review:

* the **limit** was checked after the file was opened, so a file that
  was both unreadable and over the limit answered 403 ("no permission")
  where the honest answer is 413 ("too big") — and 413 is the one a
  client can act on, because it tells it the file has to be cut into
  pieces;
* the **descriptor** opened before the range was parsed was not closed
  on the path where the range is unsatisfiable (a 416).  CPython would
  close it when the frame unwound, but "the file is closed on every
  path" is a property, not a detail of the garbage collector.

**What the fix does.** The limit is a *policy*, so it is checked
before the file is opened — against the size the path reports, which
is good enough for a policy and keeps "too big" and "no permission"
apart.  The size the answer promises still comes from the descriptor
the bytes are read from, so the answer and the body cannot disagree.
And every path that does not hand the descriptor to the response
closes it.
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


class _RecordingHandle:
    """A file object that remembers whether it was closed."""

    def __init__(self, real) -> None:
        self._real = real
        self.closed_by_us = False
        self.reads = 0

    def fileno(self):
        return self._real.fileno()

    def read(self, *args):
        self.reads += 1
        return self._real.read(*args)

    def seek(self, *args):
        return self._real.seek(*args)

    def close(self) -> None:
        self.closed_by_us = True
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestTheFileDoorChecksTheLimitFirst(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient  # noqa: PLC0415

        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-agent-token-xyz"}
        import agent_api  # noqa: PLC401

        self.agent_api = agent_api

    def _write(self, name: str, size: int) -> str:
        path = os.path.join(self.tmpdir, name)
        with open(path, "wb") as handle:
            handle.write(b"x" * size)
        return path

    def test_a_file_over_the_limit_is_refused_with_413(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        self._write("big.bin", 4096)
        with patch.object(self.agent_api, "BINARY_LIMIT_BYTES", 1024):
            response = self.client.get("/files?path=big.bin", headers=self.auth)
        self.assertEqual(response.status_code, 413, response.text)
        self.assertIn(str(1024), response.text)

    def test_the_limit_is_checked_before_the_file_is_opened(self) -> None:
        """A file that is over the limit is never opened."""
        from unittest.mock import patch  # noqa: PLC0415

        self._write("big.bin", 4096)
        opened = []
        real = self.agent_api._open_for_download

        def spy(name):
            opened.append(name)
            return real(name)

        with patch.object(self.agent_api, "BINARY_LIMIT_BYTES", 1024):
            with patch.object(self.agent_api, "_open_for_download", spy):
                response = self.client.get("/files?path=big.bin", headers=self.auth)
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(opened, [], "the file was opened although it is over the limit")

    def test_an_unreadable_file_over_the_limit_answers_413(self) -> None:
        """Not 403: the client is told the file is too big, not that it may not read it."""
        from unittest.mock import patch  # noqa: PLC0415

        self._write("big.bin", 4096)

        def refuse(name):
            raise PermissionError(name)

        with patch.object(self.agent_api, "BINARY_LIMIT_BYTES", 1024):
            with patch.object(self.agent_api, "_open_for_download", refuse):
                response = self.client.get("/files?path=big.bin", headers=self.auth)
        self.assertEqual(response.status_code, 413, response.text)

    def test_the_descriptor_is_closed_on_the_416_path(self) -> None:
        """An unsatisfiable range is a 416, and the descriptor is closed."""
        from unittest.mock import patch  # noqa: PLC0415

        self._write("small.bin", 10)
        handles: list[_RecordingHandle] = []
        real = self.agent_api._open_for_download

        def recording(name):
            handle = _RecordingHandle(real(name))
            handles.append(handle)
            return handle

        with patch.object(self.agent_api, "_open_for_download", recording):
            response = self.client.get(
                "/files?path=small.bin",
                headers={**self.auth, "Range": "bytes=500-600"},
            )
        self.assertEqual(response.status_code, 416, response.text)
        self.assertTrue(handles, "the file was never opened")
        self.assertTrue(handles[0].closed_by_us, "the descriptor was left open on the 416 path")

    def test_the_descriptor_is_handed_to_the_response_on_the_happy_path(self) -> None:
        """The cut must not close the descriptor the response streams from."""
        from unittest.mock import patch  # noqa: PLC0415

        self._write("small.bin", 10)
        handles: list[_RecordingHandle] = []
        real = self.agent_api._open_for_download

        def recording(name):
            handle = _RecordingHandle(real(name))
            handles.append(handle)
            return handle

        with patch.object(self.agent_api, "_open_for_download", recording):
            response = self.client.get("/files?path=small.bin", headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, b"x" * 10)
        # The descriptor may be closed by the time the answer is here —
        # the stream closes it.  What must not happen is that it was
        # closed *before* the body was read.
        self.assertGreater(
            handles[0].reads,
            0,
            "the body was never read from the descriptor — it was closed first",
        )


if __name__ == "__main__":
    unittest.main()
