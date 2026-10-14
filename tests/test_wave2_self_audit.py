"""Regression tests for the wave-2 self-audit (Mavis, post-Qwen review).

Each test pins one finding from the self-audit so a future refactor
can't silently reintroduce the leak.
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


# ---------------------------------------------------------------------------
# agent_api fixes
# ---------------------------------------------------------------------------
def _fresh_agent_app():
    tmp = tempfile.mkdtemp(prefix="agent-w2-")
    os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmp, "sessions.json")
    os.environ["AGENT_WORKSPACE"] = tmp
    os.environ["AGENT_TOKEN"] = "test-token"
    if "agent_api" in sys.modules:
        del sys.modules["agent_api"]
    from agent_api import SESSIONS, app  # noqa: E402

    SESSIONS.clear()
    return app, SESSIONS, tmp


class _AgentBase(unittest.TestCase):
    def setUp(self) -> None:
        import shutil  # noqa: PLC0415
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        if not shutil.which("tmux"):
            self.skipTest("tmux not installed in test env")
        self.app, self.sessions, self.tmpdir = _fresh_agent_app()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-token"}


class TestHealthzMinimal(_AgentBase):
    """Wave 2 #H1: /healthz is unauthenticated. It must return only
    "status" — leaking the workspace path or session count to an
    unauthenticated prober is an info leak that costs nothing to fix.
    """

    def test_no_workspace_path_in_response(self) -> None:
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertNotIn("workspace", body)
        self.assertNotIn("sessions", body)
        self.assertEqual(body, {"status": "ok"})

    def test_workspace_path_does_not_appear_anywhere(self) -> None:
        r = self.client.get("/healthz")
        # The resolved workspace path must not appear in the body
        # even indirectly (e.g. as a substring of another field).
        self.assertNotIn(self.tmpdir, r.text)


class TestSecurityHeadersAgent(_AgentBase):
    """Wave 2 #L1: add X-Content-Type-Options and Cache-Control headers
    to every response so an attacker can't trick a browser into
    MIME-sniffing or cache a bearer-token response.
    """

    def test_healthz_has_security_headers(self) -> None:
        r = self.client.get("/healthz")
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    def test_authenticated_endpoint_has_security_headers(self) -> None:
        r = self.client.get("/sessions", headers=self.auth)
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")


class TestBodySizeLimitAgent(_AgentBase):
    """Wave 2 #M1: reject request bodies larger than 2 MB with HTTP 413
    before they hit Pydantic. Without this, a 1 GB body would be
    loaded into memory just to be rejected by the field validator.
    """

    def test_oversized_body_returns_413(self) -> None:
        # 3 MB body — well over the 2 MB cap
        big = "A" * (3 * 1024 * 1024)
        r = self.client.post(
            "/sessions/abc/exec",
            headers=self.auth,
            json={"command": big, "timeout": 5},
        )
        self.assertEqual(r.status_code, 413)

    def test_undersized_body_is_not_rejected_by_size_limit(self) -> None:
        # 1 KB body — well under the limit
        r = self.client.post(
            "/sessions/s1/exec",
            headers=self.auth,
            json={"command": "echo hello", "timeout": 5},
        )
        # 200 (executed) or 5xx (no tmux in sandbox); not 413
        self.assertNotEqual(r.status_code, 413)


# ---------------------------------------------------------------------------
# openapi_proxy fixes
# ---------------------------------------------------------------------------
class _ProxyBase(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        os.environ["OPENAPI_PROXY_TOKEN"] = "test-token"
        os.environ["OPENAPI_PROXY_CORS_ORIGINS"] = ""
        os.environ["OPENAPI_PROXY_SSH_ALLOWLIST"] = ""
        os.environ["OPENAPI_PROXY_RATE_LIMIT_RPM"] = "0"
        if "openapi_proxy" in sys.modules:
            del sys.modules["openapi_proxy"]
        from openapi_proxy import app  # noqa: E402

        self.app = app
        # Use context manager so the lifespan handler runs — this is what
        # attaches the singleton httpx client to ``app.state.client``.
        raw = TestClient(self.app)
        self.client = raw.__enter__()
        self.addCleanup(raw.__exit__, None, None, None)
        self.tmpdir = ""
        self.auth = {"Authorization": "Bearer test-token"}


class TestSecurityHeadersProxy(_ProxyBase):
    """Wave 2 #L1 (proxy): same security headers as agent_api."""

    def test_healthz_has_security_headers(self) -> None:
        r = self.client.get("/healthz", headers=self.auth)
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    def test_wrong_token_response_has_security_headers(self) -> None:
        # Even error responses should have the headers — the middleware
        # is the last thing that runs.
        r = self.client.get("/healthz", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")


class TestBodySizeLimitProxy(_ProxyBase):
    """Wave 2 #M1 (proxy): same body size cap as agent_api."""

    def test_oversized_bash_body_returns_413(self) -> None:
        big = "A" * (3 * 1024 * 1024)
        r = self.client.post(
            "/bash",
            headers=self.auth,
            json={"command": big, "session_id": "s1"},
        )
        self.assertEqual(r.status_code, 413)

    def test_undersized_body_is_not_rejected_by_size_limit(self) -> None:
        r = self.client.post(
            "/bash",
            headers=self.auth,
            json={"command": "echo hi", "session_id": "s1"},
        )
        self.assertNotEqual(r.status_code, 413)


if __name__ == "__main__":
    unittest.main()
