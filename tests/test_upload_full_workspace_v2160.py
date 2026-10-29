"""A full workspace is a normal answer, and the door has to name it.

The workspace became a tmpfs with a ceiling in v2.16.0, and a ceiling
turns "no space left on device" from an accident into an event. The
door has to answer it the way it answers everything else: with the
status that says what happened and a detail that names the cause.

What is pinned here:

* an upload that cannot land because the workspace is full answers
  **507**, not a 500 with a traceback in it;
* the detail names the workspace and the ceiling -- a refusal that does
  not name the cause is a refusal the caller has to guess about;
* nothing is left behind: a refused upload leaves no temporary file;
* the published door relays that answer as it is.

The refusals the door already had were pinned in v2.15.0
(``tests/test_file_door_upload_v2150.py``). This file adds the one the
ceiling introduced.
"""

from __future__ import annotations

import errno
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agent_api_endpoints import _fresh_app  # noqa: E402

_NO_SPACE = OSError(errno.ENOSPC, "No space left on device")


class TestAFullWorkspaceIsNamed(unittest.TestCase):
    """The agent's side: the ceiling has an answer."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        self.app, self.sessions, self.tmpdir, self.workspace_real = _fresh_app()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-agent-token-xyz"}

    def test_the_file_that_cannot_land_answers_507(self) -> None:
        with mock.patch("agent_api.os.replace", side_effect=_NO_SPACE):
            response = self.client.post(
                "/files", params={"path": "big.bin"}, content=b"payload", headers=self.auth
            )

        self.assertEqual(response.status_code, 507, response.text)
        detail = response.json()["detail"]
        self.assertIn(
            "workspace",
            detail,
            "the refusal does not say what refused the file — the caller "
            "has to guess between the transport, the policy and the disk",
        )
        self.assertIn("AGENT_WORKSPACE_SIZE", detail)

    def test_a_workspace_that_cannot_take_the_file_answers_507(self) -> None:
        """The other end of the same wall: not even the temp file fits."""
        with mock.patch("agent_api.tempfile.mkstemp", side_effect=_NO_SPACE):
            response = self.client.post(
                "/files", params={"path": "big.bin"}, content=b"payload", headers=self.auth
            )

        self.assertEqual(response.status_code, 507, response.text)
        self.assertIn("workspace", response.json()["detail"])

    def test_nothing_is_left_behind_when_the_workspace_is_full(self) -> None:
        """A refused upload leaves no temporary file — not even a full one."""
        with mock.patch("agent_api.os.replace", side_effect=_NO_SPACE):
            self.client.post(
                "/files", params={"path": "big.bin"}, content=b"payload", headers=self.auth
            )

        leftovers = [name for name in os.listdir(self.tmpdir) if name.startswith(".")]
        self.assertEqual(
            leftovers,
            [],
            "the refused upload left a temporary file behind in the "
            "workspace — a full workspace that also collects garbage is "
            "a workspace that stays full",
        )

    def test_the_file_that_could_not_land_is_not_there(self) -> None:
        with mock.patch("agent_api.os.replace", side_effect=_NO_SPACE):
            self.client.post(
                "/files", params={"path": "big.bin"}, content=b"payload", headers=self.auth
            )

        self.assertFalse(
            os.path.exists(os.path.join(self.tmpdir, "big.bin")),
            "the file landed even though the door refused it",
        )


class TestTheProxyRelaysTheFullWorkspace(unittest.TestCase):
    """The proxy's side: the answer the agent gave is the answer given."""

    PROXY_TOKEN = "proxy-token-for-the-full-workspace-test"

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        os.environ["OPENAPI_PROXY_TOKEN"] = self.PROXY_TOKEN
        os.environ["SANDBOX_URL"] = "http://sandbox-agent:8082"
        os.environ["OPENAPI_PROXY_RATE_LIMIT_RPM"] = "0"
        sys.modules.pop("openapi_proxy", None)
        import openapi_proxy

        self.proxy = openapi_proxy
        self.client = TestClient(self.proxy.app)
        self.auth = {"Authorization": f"Bearer {self.PROXY_TOKEN}"}

    def tearDown(self) -> None:
        sys.modules.pop("openapi_proxy", None)

    def _install(self, status_code: int, body: bytes):
        class _Upstream:
            def __init__(self) -> None:
                self.status_code = status_code
                self.headers = {"content-type": "application/json"}
                self._body = body

            async def aread(self) -> bytes:
                return self._body

            async def aclose(self) -> None:
                return None

        class _Client:
            def build_request(self, method, url, **kwargs):
                return {"method": method, "url": url, **kwargs}

            async def send(self, request, stream: bool = False):
                return _Upstream()

        self.proxy.app.state.client = _Client()

    def test_the_status_is_relayed(self) -> None:
        self._install(507, b'{"detail":"the sandbox workspace is full"}')
        response = self.client.post(
            "/files", params={"path": "big.bin"}, content=b"payload", headers=self.auth
        )
        self.assertEqual(response.status_code, 507, response.text)
        self.assertIn("workspace", response.json()["detail"])


class TestTheDocumentedRefusalIsTheRefusalTheCodeProduces(unittest.TestCase):
    """The refusal is a contract, and the contract is the handoff.

    A client reads the message of a refusal and acts on it. The handoff
    promises a wording, so the wording is pinned here against that
    document — the same way the MCP handoff's refusals are pinned.

    The check is on the *sentence*, not on the word "workspace": the
    detail mentions the workspace twice, and a check for the word would
    stay green after the sentence it promises had been rewritten.
    """

    REFUSAL = "the sandbox workspace is full — the file did not land."

    @staticmethod
    def _prose(name: str) -> str:
        """The document with its line breaks collapsed.

        A claim about prose must not depend on where the line happened to
        wrap: the sentence is the claim, not the line it sits on.
        """
        return " ".join((ROOT / name).read_text(encoding="utf-8").split())

    def test_the_handoff_promises_this_refusal(self) -> None:
        contract = self._prose("docs/HANDOFF-binary-transport.md")
        self.assertIn(
            self.REFUSAL,
            contract,
            "the handoff does not promise the full-workspace refusal — a "
            "client that reads the contract cannot tell what to do",
        )

    def test_the_code_produces_this_refusal(self) -> None:
        code = self._prose("agent_api.py")
        self.assertIn(
            self.REFUSAL,
            code,
            "the code does not produce the refusal the handoff promises — "
            "a client that reads the refusal sees something else",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
