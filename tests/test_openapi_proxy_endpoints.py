"""Integration tests for :mod:`openapi_proxy` HTTP endpoints.

Covers the security-critical paths: bearer-token auth, CORS allowlist,
rate limiting, SSH host allowlist, and the bash command dispatch. The
singleton httpx client is patched per-test so the upstream sandbox-agent
is never actually called.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_app(env_overrides: dict[str, str] | None = None):
    """Reload openapi_proxy with a per-test env. Returns ``(app, Client)``."""
    # Required token for any auth-protected endpoint.
    env = {
        "OPENAPI_PROXY_TOKEN": "test-token-abc123",
        "OPENAPI_PROXY_CORS_ORIGINS": "",
        "OPENAPI_PROXY_SSH_ALLOWLIST": "lab.local,10.0.0.0/8",
        "OPENAPI_PROXY_RATE_LIMIT_RPM": "0",  # off by default in tests
    }
    if env_overrides:
        env.update(env_overrides)
    for k, v in env.items():
        os.environ[k] = v
    if "openapi_proxy" in sys.modules:
        del sys.modules["openapi_proxy"]
    from fastapi.testclient import TestClient  # noqa: E402

    from openapi_proxy import app  # noqa: E402

    return app, TestClient(app)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        self.app, raw_client = _fresh_app()
        # Use context manager so the lifespan handler runs — this is what
        # attaches the singleton httpx client to ``app.state.client``.
        self.client = raw_client.__enter__()
        self.addCleanup(raw_client.__exit__, None, None, None)
        self.headers = {"Authorization": "Bearer test-token-abc123"}

    def tearDown(self) -> None:
        for k in (
            "OPENAPI_PROXY_TOKEN",
            "OPENAPI_PROXY_CORS_ORIGINS",
            "OPENAPI_PROXY_SSH_ALLOWLIST",
            "OPENAPI_PROXY_RATE_LIMIT_RPM",
        ):
            os.environ.pop(k, None)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class TestAuth(_Base):
    def test_no_token_means_service_unavailable(self) -> None:
        # Empty PROXY_TOKEN must refuse all requests, even ones with a
        # bearer header, to prevent an unauthenticated deployment.
        self.app, raw = _fresh_app({"OPENAPI_PROXY_TOKEN": ""})
        self.client = raw.__enter__()
        self.addCleanup(raw.__exit__, None, None, None)
        r = self.client.get("/healthz", headers=self.headers)
        self.assertEqual(r.status_code, 503)

    def test_missing_auth_returns_401(self) -> None:
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 401)
        self.assertIn("WWW-Authenticate", r.headers)

    def test_wrong_token_returns_401(self) -> None:
        r = self.client.get("/healthz", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_wrong_scheme_returns_401(self) -> None:
        r = self.client.get("/healthz", headers={"Authorization": "Basic abc"})
        self.assertEqual(r.status_code, 401)

    def test_correct_token_passes(self) -> None:
        r = self.client.get("/healthz", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
class TestCors(_Base):
    def test_cors_disabled_by_default(self) -> None:
        r = self.client.options(
            "/bash",
            headers={
                "Origin": "https://attacker.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        # No CORS middleware = no Access-Control-Allow-Origin header in response.
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in r.headers})

    def test_cors_allowlist_enforced(self) -> None:
        self.app, raw = _fresh_app(
            {
                "OPENAPI_PROXY_CORS_ORIGINS": "https://app.example.com,http://localhost:3000",
            }
        )
        self.client = raw.__enter__()
        self.addCleanup(raw.__exit__, None, None, None)
        # Allowed origin gets the header.
        r = self.client.get(
            "/healthz",
            headers={"Origin": "https://app.example.com", **self.headers},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # Disallowed origin does not.
        r2 = self.client.get(
            "/healthz",
            headers={"Origin": "https://attacker.example.com", **self.headers},
        )
        self.assertEqual(r2.status_code, 200, r2.text)
        # Inspect raw headers to confirm we did NOT echo attacker.
        allow = r2.headers.get("access-control-allow-origin", "")
        self.assertNotEqual(allow, "https://attacker.example.com")


# ---------------------------------------------------------------------------
# SSH allowlist
# ---------------------------------------------------------------------------
class TestSshAllowlist(_Base):
    def _post_bash(self, command: str) -> dict:
        return self.client.post(
            "/bash",
            json={"command": command, "session_id": "x"},
            headers=self.headers,
        ).json()

    def test_ssh_to_allowed_hostname(self) -> None:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"stdout": "ok", "exit_code": 0})
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("openapi_proxy._client", new=lambda _req: mock_client):
            body = self._post_bash("ssh user@lab.local")
        self.assertEqual(body.get("exit_code"), 0)
        # Verify the patched StrictHostKeyChecking was injected.
        sent = mock_client.post.call_args.kwargs["json"]["command"]
        self.assertIn("StrictHostKeyChecking=accept-new", sent)

    def test_ssh_to_disallowed_host_rejected(self) -> None:
        body = self._post_bash("ssh root@attacker.example")
        self.assertIn("not in allowlist", body.get("error", ""))

    def test_ssh_to_allowed_cidr(self) -> None:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"stdout": "ok", "exit_code": 0})
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("openapi_proxy._client", new=lambda _req: mock_client):
            body = self._post_bash("ssh user@10.0.5.7")
        self.assertEqual(body.get("exit_code"), 0)

    def test_ssh_to_outside_cidr_rejected(self) -> None:
        body = self._post_bash("ssh root@192.168.1.5")
        self.assertIn("not in allowlist", body.get("error", ""))

    def test_ssh_disabled_when_allowlist_empty(self) -> None:
        self.app, raw = _fresh_app({"OPENAPI_PROXY_SSH_ALLOWLIST": ""})
        self.client = raw.__enter__()
        self.addCleanup(raw.__exit__, None, None, None)
        body = self.client.post(
            "/bash",
            json={"command": "ssh root@lab.local", "session_id": "x"},
            headers={"Authorization": "Bearer test-token-abc123"},
        ).json()
        self.assertIn("not in allowlist", body.get("error", ""))


# ---------------------------------------------------------------------------
# Bash dispatch
# ---------------------------------------------------------------------------
class TestBashDispatch(_Base):
    def test_bash_forwards_to_sandbox(self) -> None:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"stdout": "hi", "exit_code": 0, "duration_ms": 5})
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("openapi_proxy._client", new=lambda _req: mock_client):
            body = self.client.post(
                "/bash",
                json={"command": "echo hi", "session_id": "chat-1"},
                headers=self.headers,
            ).json()
        self.assertEqual(body["exit_code"], 0)
        # Verify URL and body shape.
        call = mock_client.post.call_args
        self.assertTrue(call.args[0].endswith("/sessions/chat-1/exec"))
        self.assertEqual(call.kwargs["json"]["command"], "echo hi")
        self.assertEqual(call.kwargs["json"]["timeout"], 60)

    def test_bash_upstream_error(self) -> None:
        import httpx as _httpx  # noqa: PLC0415

        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock(side_effect=_httpx.HTTPError("connection refused"))
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("openapi_proxy._client", new=lambda _req: mock_client):
            body = self.client.post(
                "/bash",
                json={"command": "ls", "session_id": "x"},
                headers=self.headers,
            ).json()
        self.assertIn("sandbox-agent request failed", body["error"])

    def test_the_search_shortcut_is_gone(self) -> None:
        # The proxy used to answer a ``web_search <q>`` pseudo-command by
        # querying a SearXNG instance. Search is another MCP's job, so the
        # shortcut is gone — and a command that starts with those words is
        # now an ordinary shell command, forwarded to the sandbox verbatim.
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("openapi_proxy._client", new=lambda _req: mock_client):
            self.client.post(
                "/bash",
                json={"command": "web_search python asyncio", "session_id": "x"},
                headers=self.headers,
            )
        # Forwarded to the agent, not answered locally: no GET at all.
        mock_client.get.assert_not_called()
        self.assertEqual(mock_client.post.call_count, 1)

    def test_shell_exec_alias(self) -> None:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"stdout": "ok", "exit_code": 0})
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("openapi_proxy._client", new=lambda _req: mock_client):
            r = self.client.post(
                "/shell_exec",
                json={"command": "echo ok", "session_id": "x"},
                headers=self.headers,
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["exit_code"], 0)


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------
class TestRateLimit(_Base):
    def test_rate_limit_returns_429(self) -> None:
        self.app, raw = _fresh_app(
            {
                "OPENAPI_PROXY_RATE_LIMIT_RPM": "3",  # 3 requests / min
            }
        )
        self.client = raw.__enter__()
        self.addCleanup(raw.__exit__, None, None, None)
        # Reset the global rate limiter so prior tests don't poison the bucket.
        from openapi_proxy import _rate_limiter  # noqa: PLC0415

        _rate_limiter._buckets.clear()
        # Fire 4 requests rapidly.
        statuses = [
            self.client.post(
                "/bash",
                json={"command": "echo x", "session_id": "x"},
                headers=self.headers,
            ).status_code
            for _ in range(4)
        ]
        self.assertEqual(statuses.count(200), 3)
        self.assertEqual(statuses.count(429), 1)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
class TestInputValidation(_Base):
    def test_empty_command_rejected(self) -> None:
        r = self.client.post(
            "/bash",
            json={"command": "", "session_id": "x"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 422)

    def test_missing_command_rejected(self) -> None:
        r = self.client.post(
            "/bash",
            json={"session_id": "x"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
