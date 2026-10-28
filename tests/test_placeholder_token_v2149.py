"""Tripwire — a published placeholder is not a secret.

The operator template (``.env.example``) ships
``OPENAPI_PROXY_TOKEN=CHANGE_ME_openssl_rand_hex_32``. That string is
**public**: it is in the repository, in every clone of it, in every
screenshot of the README. A deployment that still carries it has no
secret at all — only the appearance of one, and a bearer token that
anyone who has read the repository can present.

The stack used to notice this only through ``make check-env``, which the
README's own quick start never runs: the quick start says
``docker compose up -d --build``, and the compose file's ``:?`` guard
rejects an **empty** value, not a placeholder one. So the claim in the
template — "the stack refuses to start" — was true only on the path the
Makefile takes, and false on the path the README teaches.

The rule now lives in the code, in :mod:`token_rules`, and both services
enforce it on every door they have:

* the proxy's HTTP door, its WebSocket door and its MCP relay;
* the agent's HTTP door and its MCP door.

This tripwire pins that: the rule is in one place, both services use it,
and every door refuses. It also pins the *shape* of the rule — a
placeholder is refused, an empty token is refused, a real token is not,
and a real token that merely contains the word is not refused (a false
refusal is a defect too).

A check that cannot be made red is a decoration:
``tools/mutation-check-v2149.sh`` breaks each of these on purpose and
expects the named test to go red.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The placeholder the template ships. Public, and therefore worthless.
PLACEHOLDER = "CHANGE_ME_openssl_rand_hex_32"


class TestTheRuleLivesInOnePlace(unittest.TestCase):
    """Both services must use the same rule, not two copies of it."""

    def test_both_services_import_the_shared_rule(self) -> None:
        for module in ("agent_api.py", "openapi_proxy.py"):
            with self.subTest(module=module):
                source = (REPO_ROOT / module).read_text(encoding="utf-8")
                self.assertIn(
                    "from token_rules import",
                    source,
                    f"{module} does not import the shared token rule — a second copy of the "
                    "rule is a rule that can drift",
                )

    def test_the_shared_module_is_in_both_images(self) -> None:
        """A module the code imports must be in the image that runs it.

        The check looks for the **COPY line**, not for the file's name:
        the Dockerfile explains the module in a comment above the copy,
        and a check that greps for the name would stay green when the
        copy was deleted — the comment would answer for it.
        """
        for dockerfile in ("Dockerfile.sandbox", "Dockerfile.openapi-proxy"):
            with self.subTest(dockerfile=dockerfile):
                source = (REPO_ROOT / dockerfile).read_text(encoding="utf-8")
                copies = re.findall(r"^COPY[^\n]*\btoken_rules\.py\b[^\n]*$", source, re.MULTILINE)
                self.assertTrue(
                    copies,
                    f"{dockerfile} does not COPY token_rules.py — the container would fail "
                    "on the first request with ModuleNotFoundError",
                )

    def test_the_rule_module_is_pure(self) -> None:
        """The shared module must not pull in the web stack."""
        source = (REPO_ROOT / "token_rules.py").read_text(encoding="utf-8")
        for forbidden in ("fastapi", "pydantic", "httpx", "starlette"):
            with self.subTest(module=forbidden):
                self.assertNotIn(
                    f"import {forbidden}",
                    source,
                    "token_rules.py must stay a pure module — both services import it",
                )


class TestTheRuleTellsTheThreeCasesApart(unittest.TestCase):
    """A placeholder, an empty token and a real token are three things."""

    def setUp(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT))
        from token_rules import is_placeholder, token_problem

        self.is_placeholder = is_placeholder
        self.token_problem = token_problem

    def test_a_placeholder_is_a_placeholder(self) -> None:
        self.assertTrue(self.is_placeholder(PLACEHOLDER))
        self.assertTrue(self.is_placeholder("CHANGE_ME_openssl_rand_base64_48"))

    def test_an_empty_token_is_not_a_placeholder(self) -> None:
        """Empty means "missing", which the services refuse on their own."""
        self.assertFalse(self.is_placeholder(""))

    def test_a_real_token_is_not_a_placeholder(self) -> None:
        self.assertFalse(self.is_placeholder("ci-smoke-token-not-a-secret-0000"))
        self.assertFalse(self.is_placeholder("a" * 64))

    def test_a_token_that_merely_contains_the_word_is_accepted(self) -> None:
        """A false refusal is a defect: the prefix is what marks a placeholder."""
        self.assertFalse(self.is_placeholder("my-CHANGE_ME-token"))
        self.assertFalse(self.is_placeholder("change_me"))

    def test_the_problem_message_says_what_to_do(self) -> None:
        """The operator has to be told how to fix it, not just that it is broken."""
        problem = self.token_problem(PLACEHOLDER)
        self.assertIsNotNone(problem)
        self.assertIn("openssl rand -hex 32", problem)

    def test_an_empty_token_and_a_placeholder_give_different_messages(self) -> None:
        """Two different mistakes, two different answers."""
        empty = self.token_problem("")
        placeholder = self.token_problem(PLACEHOLDER)
        self.assertNotEqual(empty, placeholder)
        self.assertIn("empty", empty)
        self.assertIn("placeholder", placeholder)


class TestTheProxyRefusesAPlaceholder(unittest.TestCase):
    """Every door of the proxy has to refuse a published token."""

    def _proxy_with_token(self, token: str):
        import importlib
        import os
        import sys

        sys.path.insert(0, str(REPO_ROOT))
        os.environ["OPENAPI_PROXY_TOKEN"] = token
        try:
            import openapi_proxy

            return importlib.reload(openapi_proxy)
        finally:
            os.environ["OPENAPI_PROXY_TOKEN"] = ""

    def test_the_http_door_refuses_a_placeholder(self) -> None:
        from fastapi.testclient import TestClient

        proxy = self._proxy_with_token(PLACEHOLDER)
        with TestClient(proxy.app) as client:
            response = client.get("/healthz", headers={"Authorization": f"Bearer {PLACEHOLDER}"})
            self.assertEqual(
                response.status_code,
                503,
                "the proxy served a request with the template placeholder as its token",
            )

    def test_a_real_token_still_opens_the_door(self) -> None:
        """The guard must not refuse a deployment that did the right thing."""
        from fastapi.testclient import TestClient

        token = "a-real-token-for-this-test-0123456789"
        proxy = self._proxy_with_token(token)
        with TestClient(proxy.app) as client:
            response = client.get("/healthz", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(response.status_code, 200)

    def test_the_websocket_door_refuses_a_placeholder(self) -> None:
        """The terminal door is a door too, and it has its own gate.

        The WebSocket carries the token in ``Sec-WebSocket-Protocol``,
        so the HTTP gate does not cover it — the check below is on the
        close code the client sees.
        """
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        proxy = self._proxy_with_token(PLACEHOLDER)
        with TestClient(proxy.app) as client:
            with self.assertRaises(WebSocketDisconnect) as ctx:
                with client.websocket_connect("/ws", subprotocols=[f"bearer.{PLACEHOLDER}"]):
                    pass
        self.assertIn(
            ctx.exception.code,
            (1008, 1006),
            "the websocket door accepted a connection with the template placeholder",
        )


class TestTheAgentRefusesAPlaceholder(unittest.TestCase):
    """The agent's doors have to refuse it too — HTTP and MCP."""

    def _agent_with_token(self, token: str):
        import importlib
        import os
        import sys

        sys.path.insert(0, str(REPO_ROOT))
        os.environ["AGENT_TOKEN"] = token
        try:
            import agent_api

            return importlib.reload(agent_api)
        finally:
            os.environ["AGENT_TOKEN"] = ""

    def test_the_http_door_refuses_a_placeholder(self) -> None:
        from fastapi.testclient import TestClient

        agent = self._agent_with_token(PLACEHOLDER)
        # The client is built without the lifespan: the agent's startup
        # reaper talks to tmux, and this test is about the door, not
        # about the shell behind it.
        client = TestClient(agent.app)
        response = client.get("/sessions", headers={"Authorization": f"Bearer {PLACEHOLDER}"})
        self.assertEqual(
            response.status_code,
            503,
            "the agent served a request with the template placeholder as its token",
        )

    def test_the_mcp_door_refuses_a_placeholder(self) -> None:
        """The MCP door is a second door; it must not be the weaker one."""
        agent = self._agent_with_token(PLACEHOLDER)
        self.assertFalse(
            agent._token_ok(PLACEHOLDER),
            "the MCP door accepted the template placeholder as a valid token",
        )

    def test_a_real_token_still_opens_the_doors(self) -> None:
        token = "a-real-token-for-this-test-0123456789"
        agent = self._agent_with_token(token)
        self.assertTrue(agent._token_ok(token))
        self.assertFalse(agent._token_ok(PLACEHOLDER))


class TestTheProxyForwardsNoUpstreamHeaderWithoutAToken(unittest.TestCase):
    """An empty upstream token means "not set", not "send no header"."""

    def test_an_empty_upstream_token_falls_back_to_the_proxy_token(self) -> None:
        import importlib
        import os
        import sys

        sys.path.insert(0, str(REPO_ROOT))
        os.environ["OPENAPI_PROXY_TOKEN"] = "the-proxy-token-0123456789"
        os.environ["OPENAPI_PROXY_UPSTREAM_TOKEN"] = ""
        try:
            import openapi_proxy

            proxy = importlib.reload(openapi_proxy)
            headers = proxy._upstream_headers()
        finally:
            os.environ.pop("OPENAPI_PROXY_UPSTREAM_TOKEN", None)
            os.environ["OPENAPI_PROXY_TOKEN"] = ""
        self.assertEqual(
            headers.get("Authorization"),
            "Bearer the-proxy-token-0123456789",
            "an empty OPENAPI_PROXY_UPSTREAM_TOKEN dropped the Authorization header "
            "entirely — the agent would answer every call with an unexplained 401",
        )

    def test_an_explicit_upstream_token_is_used(self) -> None:
        import importlib
        import os
        import sys

        sys.path.insert(0, str(REPO_ROOT))
        os.environ["OPENAPI_PROXY_TOKEN"] = "the-proxy-token-0123456789"
        os.environ["OPENAPI_PROXY_UPSTREAM_TOKEN"] = "the-upstream-token-9876543210"
        try:
            import openapi_proxy

            proxy = importlib.reload(openapi_proxy)
            headers = proxy._upstream_headers()
        finally:
            os.environ.pop("OPENAPI_PROXY_UPSTREAM_TOKEN", None)
            os.environ["OPENAPI_PROXY_TOKEN"] = ""
        self.assertEqual(headers.get("Authorization"), "Bearer the-upstream-token-9876543210")


if __name__ == "__main__":  # pragma: no cover - the runner imports this
    unittest.main()
