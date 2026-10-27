"""The proxy's MCP relay.

The agent serves the MCP endpoint; the proxy is the published surface,
so the endpoint is relayed. The relay has one job: **be a wire**.

* The caller's token is the proxy's token; the agent is called with the
  upstream token. The caller's own token is never forwarded — the agent
  would refuse it in a deployment where the two differ, and forwarding
  it would leak one trust boundary's secret into another.
* The body goes upstream byte for byte. JSON-RPC is not forgiving: a
  rewritten body is a broken request.
* The status is not flattened. A 401 from the agent is a 401 at the
  proxy, not a 500 — the caller has to be able to tell "your token is
  wrong" from "the sandbox is broken".
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

#: The token the proxy uses to call the agent. It is deliberately
#: **different** from the caller's: the property under test ("the
#: caller's token does not travel upstream") cannot be observed when the
#: two values are the same — a mutation that forwards the caller's token
#: would produce exactly the header the correct code produces.
UPSTREAM_TOKEN = "test-upstream-token-abc"


def _fresh_proxy(rate_limit_rpm: str = "0"):
    """Reload the proxy module with a known token. Returns the module."""
    tempfile.mkdtemp(prefix="proxy-mcp-test-")
    os.environ["OPENAPI_PROXY_TOKEN"] = TOKEN
    os.environ["OPENAPI_PROXY_UPSTREAM_TOKEN"] = UPSTREAM_TOKEN
    os.environ["SANDBOX_URL"] = "http://sandbox-agent:8082"
    os.environ["OPENAPI_PROXY_RATE_LIMIT_RPM"] = rate_limit_rpm
    if "openapi_proxy" in sys.modules:
        del sys.modules["openapi_proxy"]
    import openapi_proxy

    return openapi_proxy


class _FakeUpstream:
    """A stand-in for the upstream httpx response."""

    def __init__(self, status_code: int = 200, body: bytes = b"", headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {
            "content-type": "application/json",
            "content-length": str(len(body)),
        }
        self.closed = False

    async def aiter_bytes(self):
        for offset in range(0, len(self._body), 7):
            yield self._body[offset : offset + 7]

    async def aread(self) -> bytes:
        return self._body

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


class TestTheMcpDoorIsPostOnly(unittest.TestCase):
    """Only POST is relayed, and the refusal costs the pool nothing.

    The Streamable HTTP transport has three methods. Two of them have
    nothing to carry here, and one of them is expensive: ``GET`` opens
    the event stream, and relaying it would hold a connection out of
    the pool for as long as the client keeps it.

    The refusal happens **before** the upstream is called: a method
    that is not relayed must not take a connection out of the pool.
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

    def test_get_is_refused_without_asking_the_agent(self) -> None:
        fake = self._install(_FakeUpstream(200, b""))
        response = self.client.get("/mcp", headers=self.auth)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers.get("allow"), "POST")
        self.assertEqual(
            fake.requests,
            [],
            "the agent was asked — the refusal has to happen before the call",
        )

    def test_delete_is_refused_without_asking_the_agent(self) -> None:
        fake = self._install(_FakeUpstream(200, b""))
        response = self.client.delete("/mcp", headers=self.auth)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(
            fake.requests,
            [],
            "the agent was asked — the refusal has to happen before the call",
        )

    def test_post_is_still_relayed(self) -> None:
        """The door itself is untouched: POST goes upstream."""
        fake = self._install(_FakeUpstream(200, b'{"jsonrpc":"2.0","id":1,"result":{}}'))
        response = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1}, headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(fake.requests[0]["request"]["method"], "POST")


class TestTheProxyRelaysMcp(unittest.TestCase):
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
        self._install(_FakeUpstream(200, b"{}"))
        response = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertIn(response.status_code, (401, 403))

    def test_the_body_goes_upstream_unchanged(self) -> None:
        fake = self._install(_FakeUpstream(200, b'{"jsonrpc":"2.0","id":1,"result":{}}'))
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        response = self.client.post("/mcp", json=payload, headers=self.auth)
        self.assertEqual(response.status_code, 200)
        sent = fake.requests[0]["request"]
        self.assertEqual(sent["method"], "POST")
        self.assertTrue(sent["url"].endswith("/mcp"))
        import json  # noqa: PLC0415

        self.assertEqual(json.loads(sent["content"]), payload)

    def test_the_answer_comes_back_byte_for_byte(self) -> None:
        body = b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}'
        self._install(_FakeUpstream(200, body))
        response = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1}, headers=self.auth)
        self.assertEqual(response.content, body)

    def test_the_status_is_not_flattened(self) -> None:
        """A 401 from the agent stays a 401 at the proxy."""
        self._install(_FakeUpstream(401, b'{"detail":"invalid token"}'))
        response = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1}, headers=self.auth)
        self.assertEqual(response.status_code, 401)

    def test_the_transport_headers_are_forwarded(self) -> None:
        fake = self._install(_FakeUpstream(200, b"{}"))
        self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1},
            headers={
                **self.auth,
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            },
        )
        sent = fake.requests[0]["request"]["headers"]
        self.assertEqual(sent.get("Accept"), "application/json, text/event-stream")
        self.assertEqual(sent.get("Mcp-Protocol-Version"), "2025-06-18")

    def test_the_callers_host_is_forwarded(self) -> None:
        """The agent checks the host the *caller* used.

        The agent's MCP endpoint refuses a request whose Host is not one
        it knows — the protection against DNS rebinding. If the proxy
        rewrote the Host to the agent's own address inside the network,
        that check would pass for every caller and stop protecting
        anything.
        """
        fake = self._install(_FakeUpstream(200, b"{}"))
        self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1},
            headers={**self.auth, "Host": "sandbox.example.com"},
        )
        sent = fake.requests[0]["request"]["headers"]
        self.assertEqual(sent.get("Host"), "sandbox.example.com")

    def test_the_caller_token_is_not_forwarded(self) -> None:
        """One trust boundary's secret must not travel into another's.

        The caller's token is the proxy's token; the agent is called with
        the upstream token. The two are different values here on
        purpose — with one value the test cannot see the difference
        between "the upstream token was forwarded" and "the caller's
        token was forwarded", and the mutation that forwards the
        caller's token stays green.
        """
        self.assertNotEqual(
            TOKEN,
            UPSTREAM_TOKEN,
            "the two tokens are the same value — this test cannot see the difference",
        )
        fake = self._install(_FakeUpstream(200, b"{}"))
        self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1}, headers=self.auth)
        sent = fake.requests[0]["request"]["headers"]
        self.assertEqual(sent.get("Authorization"), f"Bearer {UPSTREAM_TOKEN}")
        self.assertNotEqual(
            sent.get("Authorization"),
            f"Bearer {TOKEN}",
            "the caller's token travelled upstream",
        )

    def test_the_response_is_streamed(self) -> None:
        fake = self._install(_FakeUpstream(200, b"x" * 1024))
        self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1}, headers=self.auth)
        self.assertTrue(
            fake.requests[0]["stream"],
            "the proxy asked the upstream for a buffered response",
        )


class TestTheMcpDoorIsGuarded(unittest.TestCase):
    """The MCP door is a door too.

    It has the same rate limit the exec door has — an unbounded
    tool-call loop is exactly what that limiter was written for — and a
    timeout of the relay names the cause instead of leaving an empty
    reason behind.
    """

    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        # One request per minute: the second one has to be refused.
        self.proxy = _fresh_proxy(rate_limit_rpm="1")
        self.client = TestClient(self.proxy.app)
        self.auth = {"Authorization": f"Bearer {TOKEN}"}

    def test_the_door_is_rate_limited(self) -> None:
        fake = _FakeClient(_FakeUpstream(200, b"{}"))
        self.proxy.app.state.client = fake
        first = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1}, headers=self.auth)
        second = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 2}, headers=self.auth)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            second.status_code,
            429,
            "the MCP door answers without a limit — an unbounded tool-call loop is "
            "what the limiter on the exec door exists for",
        )

    def test_a_timeout_of_the_relay_names_the_cause(self) -> None:
        """An empty reason sends the operator to the network instead of to the clock."""
        import httpx  # noqa: PLC0415

        class _TimingOut:
            def build_request(self, method: str, url: str, **kwargs) -> object:
                return {"method": method, "url": url, **kwargs}

            async def send(self, request, stream: bool = False):
                raise httpx.ReadTimeout("the read timed out")

        self.proxy.app.state.client = _TimingOut()
        response = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1}, headers=self.auth)
        self.assertEqual(response.status_code, 502)
        detail = response.json()["detail"]
        self.assertIn("timed out", detail)
        self.assertIn(
            str(int(self.proxy.MCP_RELAY_TIMEOUT_SECONDS)),
            detail,
            "the reason does not name the relay's horizon",
        )
        self.assertIn(
            str(self.proxy.AGENT_EXEC_TIMEOUT),
            detail,
            "the reason does not name the agent's horizon",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
