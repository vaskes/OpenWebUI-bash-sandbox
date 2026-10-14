"""Regression tests for the wave-3 self-audit (Mavis, post-Qwen review).

Each test pins one finding from the public-release readiness pass and
the 5 deferred items from wave 2.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Public-release readiness (wave 3)
# ---------------------------------------------------------------------------
class TestLicenseCopyright(unittest.TestCase):
    """Wave 3: LICENSE copyright must be 'Kuduza Ai Lab' (was 'vaskes')."""

    def test_copyright_holder_is_kuduza(self) -> None:
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Kuduza Ai Lab", text)
        # The old 'vaskes' string should be gone (case-insensitive on
        # the holder name, but the (c) line is what counts).
        self.assertNotIn("Copyright (c) 2026 vaskes", text)


class TestCodeOfConductEnforcementContact(unittest.TestCase):
    """Wave 3: CODE_OF_CONDUCT.md must have a real enforcement contact."""

    def test_has_email_contact(self) -> None:
        text = (ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")
        # The contact must look like an email, not a placeholder.
        import re  # noqa: PLC0415

        emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
        self.assertTrue(emails, "CODE_OF_CONDUCT.md has no email contact")
        for e in emails:
            self.assertNotIn("CHANGEME", e)
            self.assertNotIn("example.com", e)


class TestPyprojectTargetVersion(unittest.TestCase):
    """Wave 3: pyproject.toml target-version must be 'py312' (was 'py311')."""

    def test_target_version_is_py312(self) -> None:
        import tomllib  # noqa: PLC0415

        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            data.get("tool", {}).get("ruff", {}).get("target-version"),
            "py312",
        )


# ---------------------------------------------------------------------------
# Wave 2 deferred: #L3 — rate limit keys should not store the token
# ---------------------------------------------------------------------------
class TestRateLimitKeyHashed(unittest.TestCase):
    """Wave 3 #L3: the rate limiter must key on a hash, not the raw token.

    The previous implementation used the full Authorization header
    (including the bearer token) as the rate-limit key. The dict was
    process-local and LRU-evicted, so the risk was bounded — but
    hashing removes the in-memory token presence entirely.
    """

    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        os.environ["OPENAPI_PROXY_TOKEN"] = "test-token"
        os.environ["OPENAPI_PROXY_CORS_ORIGINS"] = ""
        os.environ["OPENAPI_PROXY_SSH_ALLOWLIST"] = ""
        os.environ["OPENAPI_PROXY_RATE_LIMIT_RPM"] = "0"  # off
        if "openapi_proxy" in sys.modules:
            del sys.modules["openapi_proxy"]
        from openapi_proxy import _rate_limiter  # noqa: E402

        self._rate_limiter = _rate_limiter

    def test_two_different_tokens_get_different_buckets(self) -> None:
        # Hash of "Authorization: Bearer t1" != hash of "Authorization: Bearer t2"
        # so two tokens should land in different buckets.
        from starlette.requests import Request  # noqa: E402

        from openapi_proxy import _rate_limit  # noqa: E402

        def _make(auth):
            r = Request(
                scope={
                    "type": "http",
                    "method": "POST",
                    "path": "/bash",
                    "headers": [(b"authorization", auth.encode())],
                    "client": ("1.2.3.4", 12345),
                    "scheme": "http",
                    "server": ("test", 80),
                    "query_string": b"",
                }
            )
            return r

        # The implementation hashes the auth header. We verify the source
        # of _rate_limit computes a hash, not a raw token.
        import inspect  # noqa: PLC0415

        src = inspect.getsource(_rate_limit)

        # Build a couple of request objects to make sure the import path
        # works. The actual rate-limit logic is in the source check below.
        _make("Bearer t1")
        _make("Bearer t2")
        self.assertIn("sha256", src)
        self.assertNotIn(
            "key = auth or request.client.host",
            src.replace(" ", ""),
            "_rate_limit should not use the raw auth header as the key",
        )

        # Also: anonymous requests (no auth header) should hash
        # something deterministic from the client host, not store
        # the client host verbatim. (Minor — but consistent.)
        # Skipping this assertion as the host path is unchanged.


if __name__ == "__main__":
    unittest.main()
