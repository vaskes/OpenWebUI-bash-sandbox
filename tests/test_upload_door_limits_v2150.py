"""Tests for the upload door's own limits — and for the limit that stayed.

The upload door (``POST /files``) is the one door that carries a
*file*. The command doors carry JSON, and 2 MB is a generous ceiling for
that: a body that weighs more is either a mistake or an attack. The
upload door carries tens of megabytes, so it needs a ceiling of its own
— and the general 2 MB guard would refuse the very bytes the door
exists to deliver.

Two things therefore have to be true at the same time, and each of them
can fail on its own:

* **A body over 2 MB reaches the upload door.** This is the property
  the 2 MB guard used to break: the file arrived, the guard cut it off,
  and the client was told the sandbox was broken.
* **The 2 MB guard still holds everywhere else.** A door that carries a
  command is still a door that a hostile padding must not be able to
  push megabytes through.

The tests below use *real* payloads, not a mock: a body of three
megabytes is what the operator's file looks like next to the guard, and
a test that fakes the size tests the fake.

The ceilings in play are two different numbers, and the difference is
the point:

* ``AGENT_BINARY_LIMIT`` — the *file policy*: how large a file may be
  in the sandbox. The production deployment allows 50 MiB.
* ``AGENT_UPLOAD_MAX_BYTES`` — the *transport ceiling* of the upload
  door: how many bytes are read before the request is refused. It has
  to be at least the file policy, or the transport refuses an upload the
  door itself would have accepted — with the vaguer of the two answers.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

AGENT_TOKEN = "test-agent-token-xyz"
PROXY_TOKEN = "test-proxy-token-xyz"

#: Bigger than the 2 MB guard, smaller than the transport ceiling. The
#: file policy in the tests, standing in for the production 50 MiB.
FILE_POLICY = "16m"
#: The transport ceiling of the upload door.
TRANSPORT_CEILING = "32m"

_MIB = 1024 * 1024


def _fresh_agent(**env: str):
    """Reload the agent with the given environment. Returns the app.

    ``agent_config`` reads the environment **once**, at import, so a
    test that wants a different ceiling has to reload that module too —
    not just ``agent_api``.
    """
    tmpdir = tempfile.mkdtemp(prefix="agent-upload-test-")
    atexit.register(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
    os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmpdir, "sessions.json")
    os.environ["AGENT_WORKSPACE"] = tmpdir
    os.environ["AGENT_TOKEN"] = AGENT_TOKEN
    os.environ["AGENT_BINARY_LIMIT"] = FILE_POLICY
    os.environ["AGENT_UPLOAD_MAX_BYTES"] = TRANSPORT_CEILING
    for key, value in env.items():
        os.environ[key] = str(value)
    for name in ("agent_api", "agent_config"):
        sys.modules.pop(name, None)
    from agent_api import app  # noqa: PLC0415

    return app, tmpdir


def _restore_agent_modules() -> None:
    """Leave the environment as it was, so the next test sees a clean one."""
    for key in ("AGENT_BINARY_LIMIT", "AGENT_UPLOAD_MAX_BYTES"):
        os.environ.pop(key, None)
    for name in ("agent_api", "agent_config"):
        sys.modules.pop(name, None)


class TestTheUploadDoorHasItsOwnCeiling(unittest.TestCase):
    """The agent's side: the file gets in, and the other doors do not open."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient  # noqa: PLC0415

        self.app, self.tmpdir = _fresh_agent()
        self.client = TestClient(self.app)
        self.auth = {"Authorization": f"Bearer {AGENT_TOKEN}"}

    def tearDown(self) -> None:
        _restore_agent_modules()

    def test_a_body_over_two_megabytes_reaches_the_door(self) -> None:
        """The gap the handoff measured: the guard used to cut the file off.

        Three megabytes is above the 2 MB guard that every other door
        keeps, and well below both the file policy and the transport
        ceiling. The file has to land.
        """
        payload = b"Z" * (3 * _MIB)

        response = self.client.post(
            "/files", params={"path": "big.bin"}, content=payload, headers=self.auth
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["size"], len(payload))
        landed = os.path.join(self.tmpdir, "big.bin")
        self.assertTrue(os.path.isfile(landed), "the file did not land in the workspace")
        self.assertEqual(os.path.getsize(landed), len(payload))

    def test_the_bytes_that_land_are_the_bytes_that_were_sent(self) -> None:
        """A relay that mangles the payload is worse than no door at all."""
        payload = bytes(range(256)) * (12 * _MIB // 256)  # 12 MiB, every byte value

        response = self.client.post(
            "/files", params={"path": "bytes.bin"}, content=payload, headers=self.auth
        )

        self.assertEqual(response.status_code, 200, response.text)
        with open(os.path.join(self.tmpdir, "bytes.bin"), "rb") as handle:
            self.assertEqual(handle.read(), payload)

    def test_the_exec_door_still_refuses_a_body_over_two_megabytes(self) -> None:
        """The 2 MB guard is not weakened for the door next to it."""
        payload = json.dumps({"command": "echo hi", "session_id": "x", "pad": "Z" * (3 * _MIB)})

        response = self.client.post(
            "/sessions/some-session/exec",
            content=payload,
            headers={**self.auth, "Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 413, response.text)
        self.assertIn("too large", response.text)

    def test_a_body_over_the_transport_ceiling_is_refused(self) -> None:
        """The ceiling is a ceiling: it stops the reading, not the writing."""
        chunk = b"Z" * _MIB
        sent = 0

        def _body():
            nonlocal sent
            for _ in range(33):  # 33 MiB against a 32 MiB ceiling
                sent += len(chunk)
                yield chunk

        response = self.client.post(
            "/files",
            params={"path": "huge.bin"},
            content=_body(),
            headers={**self.auth, "Content-Type": "application/octet-stream"},
        )

        self.assertEqual(response.status_code, 413, response.text)
        self.assertFalse(
            os.path.exists(os.path.join(self.tmpdir, "huge.bin")),
            "the refused upload left a file behind",
        )

    def test_the_file_policy_answers_for_a_file_over_it(self) -> None:
        """Over the file policy, over the transport ceiling: the policy speaks.

        The body is under the transport ceiling (32 MiB) and over the file
        policy (16 MiB), so the answer has to come from the door itself,
        with the file's own size in the message.
        """
        payload = b"Z" * (20 * _MIB)

        response = self.client.post(
            "/files", params={"path": "toobig.bin"}, content=payload, headers=self.auth
        )

        self.assertEqual(response.status_code, 413, response.text)
        self.assertIn(str(len(payload)), response.text)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "toobig.bin")))

    def test_the_limit_is_answered_per_path(self) -> None:
        """One guard, two ceilings: the path decides which one applies."""
        from body_limit import BodySizeLimit  # noqa: PLC0415

        guard = BodySizeLimit(
            app=lambda *a, **k: None, limit=2 * _MIB, overrides={"/files": 64 * _MIB}
        )

        self.assertEqual(guard.limit_for("/files"), 64 * _MIB)
        self.assertEqual(guard.limit_for("/files/meta"), 64 * _MIB)
        self.assertEqual(guard.limit_for("/bash"), 2 * _MIB)
        self.assertEqual(guard.limit_for("/sessions/x/exec"), 2 * _MIB)
        self.assertEqual(guard.limit_for("/filesystem"), 2 * _MIB)


class _FakeUpstream:
    """A stand-in for the upstream httpx response of an upload."""

    def __init__(self, status_code: int = 200, body: bytes = b"", headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {"content-type": "application/json"}
        self.closed = False

    async def aread(self) -> bytes:
        return self._body

    async def aiter_bytes(self):
        yield self._body

    async def aclose(self) -> None:
        self.closed = True


class _FakeClient:
    """Records what the proxy asked for and answers with a canned body.

    The upload is different from the download: the *request* carries the
    payload, so the fake has to consume the body the proxy streamed
    upstream and remember what it was.
    """

    def __init__(self, upstream: _FakeUpstream):
        self.upstream = upstream
        self.requests: list[dict] = []
        self.received = b""

    def build_request(self, method: str, url: str, **kwargs) -> object:
        return {"method": method, "url": url, **kwargs}

    async def send(self, request, stream: bool = False):
        self.requests.append({"request": request, "stream": stream})
        content = request.get("content")
        if content is not None and hasattr(content, "__aiter__"):
            chunks = []
            async for chunk in content:
                chunks.append(chunk)
            self.received = b"".join(chunks)
        elif isinstance(content, (bytes, bytearray)):
            self.received = bytes(content)
        return self.upstream


def _fresh_proxy(**env: str):
    """Reload the proxy module with a known token. Returns the module."""
    tempfile.mkdtemp(prefix="proxy-test-")
    os.environ["OPENAPI_PROXY_TOKEN"] = PROXY_TOKEN
    os.environ["SANDBOX_URL"] = "http://sandbox-agent:8082"
    os.environ["OPENAPI_PROXY_RATE_LIMIT_RPM"] = "0"
    os.environ["OPENAPI_PROXY_UPLOAD_MAX_BYTES"] = str(64 * _MIB)
    for key, value in env.items():
        os.environ[key] = str(value)
    sys.modules.pop("openapi_proxy", None)
    import openapi_proxy  # noqa: PLC0415

    return openapi_proxy


class TestTheProxyRelaysTheUpload(unittest.TestCase):
    """The proxy's side: the way in has to exist on the published door too."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient  # noqa: PLC0415

        self.proxy = _fresh_proxy()
        self.client = TestClient(self.proxy.app)
        self.auth = {"Authorization": f"Bearer {PROXY_TOKEN}"}

    def tearDown(self) -> None:
        os.environ.pop("OPENAPI_PROXY_UPLOAD_MAX_BYTES", None)
        sys.modules.pop("openapi_proxy", None)

    def _install(self, upstream: _FakeUpstream) -> _FakeClient:
        fake = _FakeClient(upstream)
        self.proxy.app.state.client = fake
        return fake

    def test_the_upload_door_exists_on_the_proxy(self) -> None:
        """Without the route, the file door is a one-way street."""
        self._install(_FakeUpstream(200, b'{"name":"a.bin","path":"/workspace/a.bin","size":3}'))
        response = self.client.post(
            "/files", params={"path": "a.bin"}, content=b"abc", headers=self.auth
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["name"], "a.bin")

    def test_a_body_over_two_megabytes_reaches_the_agent(self) -> None:
        """The gap the handoff measured, on the published door.

        Three megabytes is over the 2 MB guard the proxy keeps for its
        command doors. The bytes have to arrive at the agent unchanged.
        """
        payload = b"Q" * (3 * _MIB)
        fake = self._install(_FakeUpstream(200, b'{"name":"big.bin","size":3145728}'))

        response = self.client.post(
            "/files", params={"path": "big.bin"}, content=payload, headers=self.auth
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(fake.received), len(payload))
        self.assertEqual(fake.received, payload)

    def test_the_body_is_streamed_upstream(self) -> None:
        """Buffering the upload would trade a size ceiling for an OOM kill."""
        fake = self._install(_FakeUpstream(200, b'{"name":"a.bin","size":1}'))
        self.client.post("/files", params={"path": "a.bin"}, content=b"abc", headers=self.auth)
        self.assertTrue(fake.requests, "the proxy did not call the upstream at all")
        content = fake.requests[0]["request"]["content"]
        self.assertFalse(
            isinstance(content, (bytes, bytearray)),
            "the proxy buffered the whole body before sending it",
        )
        self.assertTrue(hasattr(content, "__aiter__"))

    def test_the_status_is_relayed(self) -> None:
        """A taken name stays a 409 and an oversized file stays a 413."""
        for status, payload in (
            (409, b'{"detail":"file already exists: a.bin"}'),
            (413, b'{"detail":"file is 999 bytes, over the binary limit"}'),
            (404, b'{"detail":"no such directory: gone"}'),
        ):
            with self.subTest(status=status):
                self._install(_FakeUpstream(status, payload))
                response = self.client.post(
                    "/files", params={"path": "a.bin"}, content=b"abc", headers=self.auth
                )
                self.assertEqual(response.status_code, status, response.text)
                self.assertNotIn("{", response.json()["detail"])

    def test_the_exec_door_still_refuses_a_body_over_two_megabytes(self) -> None:
        """The 2 MB guard is not weakened for the door next to it."""
        payload = json.dumps({"command": "echo hi", "session_id": "x", "pad": "Z" * (3 * _MIB)})

        response = self.client.post(
            "/bash",
            content=payload,
            headers={**self.auth, "Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 413, response.text)

    def test_the_upload_door_has_its_own_ceiling(self) -> None:
        """The proxy's transport ceiling is a ceiling too.

        The guard counts the bytes as the application reads them, so the
        upstream has to be there to read them: the ceiling fires while
        the body is on its way up.
        """
        self._install(_FakeUpstream(200, b'{"name":"huge.bin","size":1}'))
        chunk = b"Z" * _MIB

        def _body():
            for _ in range(65):  # 65 MiB against a 64 MiB ceiling
                yield chunk

        response = self.client.post(
            "/files",
            params={"path": "huge.bin"},
            content=_body(),
            headers={**self.auth, "Content-Type": "application/octet-stream"},
        )

        self.assertEqual(response.status_code, 413, response.text)


class TestTheDocumentedRefusalsAreTheRefusalsTheCodeProduces(unittest.TestCase):
    """The refusals are a contract, and a refactor changed one of them.

    The upload door needed the path rules of the file door and the shell
    door in one place, and unifying them into one helper is the obvious
    way to do it. The helper also unified their *refusal wording* --
    which is not a detail: the MCP handoff lists the refusals a caller
    may see, and the production verifier asserts one of them against the
    running stack. The unification quietly changed what the client
    reads, and the only thing that noticed was the live contract.

    So the wording is pinned here, against the document that promises it.
    """

    #: The refusals the MCP handoff promises a caller. Every one of them
    #: has to be the text the code actually produces.
    REFUSALS = (
        "path outside the workspace: ",
        "not a regular file: ",
        "file not found: ",
        "permission denied: ",
        "the file was replaced while it was being opened: ",
    )

    def test_every_documented_refusal_exists_in_the_code(self) -> None:
        contract = (ROOT / "docs" / "HANDOFF-mcp.md").read_text(encoding="utf-8")
        code = "\n".join(
            (ROOT / name).read_text(encoding="utf-8") for name in ("agent_api.py", "mcp_server.py")
        )
        for refusal in self.REFUSALS:
            with self.subTest(refusal=refusal):
                self.assertIn(
                    refusal,
                    contract,
                    f"the MCP handoff does not document the refusal {refusal!r}",
                )
                self.assertIn(
                    refusal,
                    code,
                    f"the code does not produce the refusal {refusal!r} that the "
                    "handoff documents - a client that reads the refusal sees something else",
                )

    def test_the_shell_door_keeps_its_own_wording(self) -> None:
        """The two doors said two different things, and both keep saying it."""
        source = (ROOT / "agent_api.py").read_text(encoding="utf-8")
        self.assertIn('refusal="cwd outside workspace"', source)
        self.assertIn('detail=f"path outside the workspace: {candidate}"', source)


if __name__ == "__main__":
    unittest.main()
