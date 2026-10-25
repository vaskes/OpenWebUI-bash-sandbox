"""Tests for the proxy's binary door.

The agent side of the file endpoint is covered in
``test_binary_endpoint.py``. What is tested here is the hop in between,
and it has exactly one job: **relay the bytes without turning them into
something else**.

Three properties matter, and each of them has a way of failing that a
unit test with a mocked transport would happily wave through:

* The proxy must not buffer. A file that is larger than the proxy's
  memory limit would kill the container if the whole thing were read
  into a buffer first. The upstream request therefore has to be made in
  streaming mode, and the response has to be a stream too.
* The proxy must not flatten the upstream status. A file over the
  policy limit is a 413, and a missing file is a 404 — turning either
  into a 500 would tell the client "the sandbox is broken" when in fact
  the answer is a plain "no".
* The client's ``Range`` header must reach the agent. Without it, a
  file that is too large for one request cannot be pulled in pieces at
  all — which is the reason the endpoint exists.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOKEN = "test-proxy-token-xyz"


def _fresh_proxy():
    """Reload the proxy module with a known token. Returns the app."""
    tempfile.mkdtemp(prefix="proxy-test-")
    os.environ["OPENAPI_PROXY_TOKEN"] = TOKEN
    os.environ["SANDBOX_URL"] = "http://sandbox-agent:8082"
    os.environ["OPENAPI_PROXY_RATE_LIMIT_RPM"] = "0"
    if "openapi_proxy" in sys.modules:
        del sys.modules["openapi_proxy"]
    import openapi_proxy

    return openapi_proxy


class _FakeUpstream:
    """A stand-in for the upstream httpx response.

    It has to look enough like an httpx response for the relay code:
    ``status_code``, ``headers``, ``aiter_bytes()``, ``aread()`` and
    ``aclose()``.
    """

    def __init__(self, status_code: int = 200, body: bytes = b"", headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {
            "content-type": "application/octet-stream",
            "accept-ranges": "bytes",
            "content-length": str(len(body)),
        }
        self.closed = False

    async def aiter_bytes(self):
        # Deliberately chunked: a relay that read the whole body at once
        # would still pass a byte-equality check.
        for offset in range(0, len(self._body), 7):
            yield self._body[offset : offset + 7]

    async def aread(self) -> bytes:
        return self._body

    def json(self):
        import json as _json

        return _json.loads(self._body.decode("utf-8"))

    async def aclose(self) -> None:
        self.closed = True


class _FakeClient:
    """Records what the proxy asked for and answers with a canned body."""

    def __init__(self, upstream: _FakeUpstream):
        self.upstream = upstream
        self.requests: list[dict] = []

    def build_request(self, method: str, url: str, **kwargs) -> object:
        return {"method": method, "url": url, **kwargs}

    async def send(self, request, stream: bool = False):
        self.requests.append({"request": request, "stream": stream})
        return self.upstream

    async def get(self, url: str, **kwargs):
        self.requests.append({"request": {"method": "GET", "url": url, **kwargs}, "stream": False})
        return self.upstream

    async def delete(self, url: str, **kwargs):
        self.requests.append(
            {"request": {"method": "DELETE", "url": url, **kwargs}, "stream": False}
        )
        return self.upstream


class TestProxyBinaryRelay(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        self.proxy = _fresh_proxy()
        self.client = TestClient(self.proxy.app)
        self.auth = {"Authorization": f"Bearer {TOKEN}"}

    def _install(self, upstream: _FakeUpstream) -> _FakeClient:
        fake = _FakeClient(upstream)
        self.proxy.app.state.client = fake
        return fake

    def test_requires_auth(self) -> None:
        self._install(_FakeUpstream(200, b"secret"))
        response = self.client.get("/files?path=a.bin")
        self.assertIn(response.status_code, (401, 403))

    def test_relays_the_bytes_unchanged(self) -> None:
        payload = bytes(range(256)) * 4
        self._install(_FakeUpstream(200, payload))
        response = self.client.get("/files?path=a.bin", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, payload)

    def test_the_upstream_request_is_streamed(self) -> None:
        """Buffering here would trade a size ceiling for an OOM kill."""
        fake = self._install(_FakeUpstream(200, b"x" * 1024))
        self.client.get("/files?path=a.bin", headers=self.auth)
        self.assertTrue(fake.requests, "the proxy did not call the upstream at all")
        self.assertTrue(
            fake.requests[0]["stream"],
            "the proxy asked the upstream for a buffered response",
        )

    def test_the_client_range_is_forwarded(self) -> None:
        fake = self._install(_FakeUpstream(206, b"456", {"content-range": "bytes 3-5/10"}))
        response = self.client.get("/files?path=a.bin", headers={**self.auth, "Range": "bytes=3-5"})
        self.assertEqual(response.status_code, 206)
        sent = fake.requests[0]["request"]["headers"]
        self.assertEqual(sent.get("Range"), "bytes=3-5")

    def test_an_oversized_file_keeps_its_413(self) -> None:
        self._install(
            _FakeUpstream(413, b'{"detail":"file is 3000000 bytes, over the binary limit"}')
        )
        response = self.client.get("/files?path=big.bin", headers=self.auth)
        self.assertEqual(response.status_code, 413)
        self.assertIn("3000000", response.text)

    def test_the_error_is_not_wrapped_twice(self) -> None:
        """The message has to be readable, not a JSON string in a JSON string.

        The agent answers ``{"detail": "..."}``. Relaying that document
        as the proxy's own detail renders it as ``{"detail": "{\\"detail\\":
        ...}"}`` — faithful, and unreadable.
        """
        self._install(_FakeUpstream(413, b'{"detail":"file is 3000000 bytes, over the limit"}'))
        response = self.client.get("/files?path=big.bin", headers=self.auth)
        body = response.json()
        self.assertEqual(body["detail"], "file is 3000000 bytes, over the limit")
        self.assertNotIn("{", body["detail"])

    def test_a_missing_file_keeps_its_404(self) -> None:
        self._install(_FakeUpstream(404, b'{"detail":"file not found: nope.bin"}'))
        response = self.client.get("/files?path=nope.bin", headers=self.auth)
        self.assertEqual(response.status_code, 404)
        self.assertIn("not found", response.text)

    def test_an_upstream_failure_is_a_502_not_a_500(self) -> None:
        import httpx

        class _Broken:
            def build_request(self, *args, **kwargs):
                return {}

            async def send(self, request, stream: bool = False):
                raise httpx.ConnectError("sandbox-agent is unreachable")

        self.proxy.app.state.client = _Broken()
        response = self.client.get("/files?path=a.bin", headers=self.auth)
        self.assertEqual(response.status_code, 502)

    def test_the_file_door_is_not_rate_limited(self) -> None:
        """A per-minute budget would break a chunked transfer.

        Pulling a large file takes several requests by design. If those
        requests counted against the same budget as the interactive
        calls, a legitimate transfer would fail for a reason that has
        nothing to do with the transfer.
        """
        self._install(_FakeUpstream(200, b"payload"))
        for _ in range(25):
            response = self.client.get("/files?path=a.bin", headers=self.auth)
            self.assertEqual(response.status_code, 200)

    def test_meta_is_relayed(self) -> None:
        self._install(
            _FakeUpstream(
                200,
                b'{"name":"a.bin","size":10,"mtime":0,"mime":"application/octet-stream","limit":0}',
                {"content-type": "application/json"},
            )
        )
        response = self.client.get("/files/meta?path=a.bin", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "a.bin")


class TestProxyPurgeRelay(unittest.TestCase):
    """The purge hop: the sandbox copy is removed after delivery.

    Same three properties as the read hop — the status is relayed rather
    than flattened, the upstream is reached, and an unreachable agent is a
    502 and not a 500.
    """

    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        self.proxy = _fresh_proxy()
        self.client = TestClient(self.proxy.app)
        self.auth = {"Authorization": f"Bearer {TOKEN}"}

    def _install(self, upstream: _FakeUpstream) -> _FakeClient:
        fake = _FakeClient(upstream)
        self.proxy.app.state.client = fake
        return fake

    def test_requires_auth(self) -> None:
        self._install(_FakeUpstream(204))
        self.assertIn(self.client.delete("/files?path=a.bin").status_code, (401, 403))

    def test_the_delete_reaches_the_upstream_with_the_path(self) -> None:
        fake = self._install(_FakeUpstream(204))
        response = self.client.delete("/files?path=a.bin", headers=self.auth)
        self.assertEqual(response.status_code, 204)
        self.assertTrue(fake.requests, "the proxy did not call the upstream at all")
        sent = fake.requests[0]["request"]
        self.assertEqual(sent["method"], "DELETE")
        self.assertTrue(sent["url"].endswith("/files"), sent["url"])
        self.assertEqual(sent["params"]["path"], "a.bin")

    def test_a_missing_file_keeps_its_404(self) -> None:
        self._install(_FakeUpstream(404, b'{"detail":"file not found: nope.bin"}'))
        response = self.client.delete("/files?path=nope.bin", headers=self.auth)
        self.assertEqual(response.status_code, 404)
        self.assertIn("not found", response.text)

    def test_the_error_is_not_wrapped_twice(self) -> None:
        self._install(_FakeUpstream(404, b'{"detail":"file not found: nope.bin"}'))
        response = self.client.delete("/files?path=nope.bin", headers=self.auth)
        body = response.json()
        self.assertEqual(body["detail"], "file not found: nope.bin")
        self.assertNotIn("{", body["detail"])

    def test_an_upstream_failure_is_a_502_not_a_500(self) -> None:
        import httpx

        class _Broken:
            def build_request(self, *args, **kwargs):
                return {}

            async def send(self, request, stream: bool = False):
                raise httpx.ConnectError("sandbox-agent is unreachable")

            async def delete(self, *args, **kwargs):
                raise httpx.ConnectError("sandbox-agent is unreachable")

        self.proxy.app.state.client = _Broken()
        response = self.client.delete("/files?path=a.bin", headers=self.auth)
        self.assertEqual(response.status_code, 502)

    def test_the_purge_is_not_rate_limited(self) -> None:
        """Same reasoning as the read path: the file door has no budget.

        A per-minute request budget would break a legitimate multi-request
        transfer for a reason that has nothing to do with the transfer.
        """
        self._install(_FakeUpstream(204))
        for _ in range(25):
            response = self.client.delete("/files?path=a.bin", headers=self.auth)
            self.assertEqual(response.status_code, 204)


if __name__ == "__main__":
    unittest.main()
