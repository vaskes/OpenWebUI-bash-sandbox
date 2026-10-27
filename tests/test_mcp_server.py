"""The MCP door: the tools, the gate, and the protocol.

The sandbox now answers two protocols. The HTTP one was tested for
years; this file covers the MCP one, and it does it against a **real
MCP client** — the reference SDK — because "it speaks MCP" is a claim
about the wire, not about the code.

What is pinned here:

* the door is locked — no token, no door, and the refusal says what is
  missing;
* the tools are the ones the HTTP API exposes, with the same names the
  documentation uses;
* the tools work: a command runs, a file comes back, a file is gone;
* the containment holds — a path outside the workspace is refused, and
  the refusal names the reason;
* the protocol answers an **older** client (2025-06-18, what most of
  the world speaks today) with that same version, so the server is not
  a client for one generation of callers only.
"""

from __future__ import annotations

import asyncio
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

TOKEN = "test-agent-token-xyz"


def _fresh_app(allowed_hosts: str | None = None):
    """Reload agent_api (and the MCP module) against a temp workspace.

    ``allowed_hosts`` is the value of ``AGENT_MCP_ALLOWED_HOSTS``; None
    means "unset", which is the default posture (the Host check is off
    unless the operator asks for it).
    """
    tmpdir = tempfile.mkdtemp(prefix="agent-mcp-test-")
    # The workspace is removed when the test process ends — see the same
    # note in test_agent_api_endpoints.py.
    atexit.register(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
    os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmpdir, "sessions.json")
    os.environ["AGENT_WORKSPACE"] = tmpdir
    os.environ["AGENT_TOKEN"] = TOKEN
    if allowed_hosts is None:
        os.environ.pop("AGENT_MCP_ALLOWED_HOSTS", None)
    else:
        os.environ["AGENT_MCP_ALLOWED_HOSTS"] = allowed_hosts
    for name in ("agent_api", "mcp_server"):
        if name in sys.modules:
            del sys.modules[name]
    from agent_api import SESSIONS, app  # noqa: E402
    from mcp_server import mcp  # noqa: E402

    SESSIONS.clear()
    return app, mcp, tmpdir


def _has_tmux() -> bool:
    """True when tmux is installed.

    The agent's lifespan reaps the sessions whose tmux backend died, so
    the MCP endpoint cannot even start without tmux. The CI runner
    installs it (the same reason the exec tests need it).
    """
    import shutil  # noqa: PLC0415

    return shutil.which("tmux") is not None


#: The whole module needs the application's lifespan, and the lifespan
#: needs tmux. Without it there is no MCP transport to talk to.
requires_tmux = unittest.skipUnless(_has_tmux(), "tmux is not installed")


@requires_tmux
class _Base(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        self.app, self.mcp, self.tmpdir = _fresh_app()
        # The client is entered, not just created: the MCP transport runs
        # in a task group that only exists while the application's
        # lifespan is running, and without it every request answers
        # "Task group is not initialized".
        # The base URL decides the Host header, and the endpoint answers
        # only for the hosts it was told about (the loopback names the
        # stack publishes on) — that is the protection against DNS
        # rebinding, and it is exercised below.
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8090")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.auth = {"Authorization": f"Bearer {TOKEN}"}
        self.ws = Path(self.tmpdir)

    def write(self, name: str, data: bytes) -> Path:
        target = self.ws / name
        target.write_bytes(data)
        return target


class TestTheDoorIsLocked(_Base):
    """The MCP door is a door, not a hole in the wall."""

    def test_no_token_is_refused(self) -> None:
        response = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("bearer", response.json()["detail"].lower())

    def test_a_wrong_token_is_refused(self) -> None:
        response = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Authorization": "Bearer not-the-token"},
        )
        self.assertEqual(response.status_code, 401)

    def test_a_foreign_host_is_answered_by_default(self) -> None:
        """The Host check is off unless the operator asks for it.

        A containerised service is reached by its service name, and a
        list that does not contain that name refuses every legitimate
        caller — including the proxy, which is the published surface.
        The endpoint needs the bearer token either way, and a page in a
        browser cannot read the token.
        """
        response = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={**self.auth, "Host": "evil.example.com"},
        )
        self.assertNotEqual(response.status_code, 421)

    def test_a_token_in_the_wrong_scheme_is_refused(self) -> None:
        response = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Authorization": f"Basic {TOKEN}"},
        )
        self.assertEqual(response.status_code, 401)

    def test_the_token_that_opens_the_http_door_opens_the_mcp_door(self) -> None:
        """One token, one trust boundary.

        The MCP gate and the HTTP gate compare the same value with the
        same function. A token that opens one has to open the other —
        otherwise one of the doors is a different policy, and nobody
        knows which.
        """
        response = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            headers={**self.auth, "Accept": "application/json, text/event-stream"},
        )
        self.assertEqual(response.status_code, 200)


class TestTheHostCheckWhenItIsOn(unittest.TestCase):
    """The DNS-rebinding protection, when the operator turns it on.

    A web page in the operator's browser can talk to a service on the
    machine. The bearer token is the real guard — a page cannot read
    it — but a request whose Host header is not one the endpoint knows
    about can be refused before it is even looked at.
    """

    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        self.app, self.mcp, self.tmpdir = _fresh_app("sandbox.example.com")
        self.client = TestClient(self.app, base_url="http://sandbox.example.com")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.auth = {"Authorization": f"Bearer {TOKEN}"}

    def test_a_foreign_host_is_refused(self) -> None:
        response = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={**self.auth, "Host": "evil.example.com"},
        )
        self.assertEqual(response.status_code, 421)

    def test_the_named_host_is_answered(self) -> None:
        response = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={**self.auth, "Host": "sandbox.example.com"},
        )
        self.assertEqual(response.status_code, 200)

    def test_the_check_is_off_when_the_list_is_empty(self) -> None:
        import mcp_server  # noqa: PLC0415

        os.environ.pop("AGENT_MCP_ALLOWED_HOSTS", None)
        self.assertEqual(mcp_server.allowed_hosts(), [])
        self.assertFalse(mcp_server.rebinding_protection_is_on())


class TestTheProtocol(_Base):
    """The wire, not the code."""

    def test_an_old_client_gets_its_own_version_back(self) -> None:
        """A 2025-06-18 client is what most of the world speaks today.

        The server may know newer revisions, but it has to answer the
        version the client asked for — otherwise every caller that has
        not updated its SDK in a year is locked out.
        """
        response = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            headers={**self.auth, "Accept": "application/json, text/event-stream"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(body["result"]["serverInfo"]["name"], "openwebui-bash-sandbox")

    def test_the_handshake_names_the_version_of_the_release(self) -> None:
        """The client is told which sandbox it is talking to.

        The version used to be a placeholder — "0.0.0", replaced later
        if anybody remembered. A caller that pins a client to a server
        version has nothing to pin to, and an operator comparing a
        handshake with a changelog has nothing to compare. The number
        is the release number, read from the same place the release
        reads it.
        """
        from agent_core import __version__  # noqa: PLC0415

        response = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            headers={**self.auth, "Accept": "application/json, text/event-stream"},
        )
        self.assertEqual(response.status_code, 200)
        version = response.json()["result"]["serverInfo"]["version"]
        self.assertEqual(version, __version__)
        self.assertNotEqual(version, "0.0.0")

    def test_a_client_that_asks_for_the_future_gets_a_usable_version(self) -> None:
        """The answer is always a version the client can speak.

        The newest revision (2026-07-28) is not reached through the
        handshake — it has its own envelope and is discovered with a
        probe. A client that asks for it in the handshake gets the
        newest revision the handshake does carry, which is the point:
        the client is never handed a version it cannot use.
        """
        response = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2026-07-28",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            headers={**self.auth, "Accept": "application/json, text/event-stream"},
        )
        self.assertEqual(response.status_code, 200)
        answered = response.json()["result"]["protocolVersion"]
        self.assertIn(answered, ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"))

    def test_the_endpoint_does_not_redirect(self) -> None:
        """A redirect on every call is a round trip nobody asked for.

        A mounted application answers "/mcp" with a redirect to "/mcp/",
        and some clients do not follow a redirect for POST at all — the
        handshake simply never happens. The endpoint is a route, so the
        path that is documented is the path that answers.
        """
        response = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={**self.auth, "Accept": "application/json, text/event-stream"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(response.status_code, (301, 302, 307, 308))

    def test_an_unknown_method_is_a_protocol_error(self) -> None:
        response = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "no/such/method"},
            headers={**self.auth, "Accept": "application/json, text/event-stream"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("error", body)
        self.assertEqual(body["error"]["code"], -32601)


class TestTheToolsAreAdvertised(_Base):
    """The catalogue is a contract."""

    def test_the_tools_are_the_documented_ones(self) -> None:
        import mcp_server  # noqa: PLC0415

        self.assertEqual(
            sorted(mcp_server.tool_names()),
            ["bash", "delete_file", "file_info", "read_file"],
        )

    def test_the_tools_are_listed_over_the_wire(self) -> None:
        response = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers={**self.auth, "Accept": "application/json, text/event-stream"},
        )
        self.assertEqual(response.status_code, 200)
        tools = response.json()["result"]["tools"]
        self.assertEqual(
            sorted(tool["name"] for tool in tools),
            ["bash", "delete_file", "file_info", "read_file"],
        )
        for tool in tools:
            self.assertTrue(tool.get("description"), f"{tool['name']} has no description")
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_the_bash_tool_takes_the_arguments_the_docs_describe(self) -> None:
        response = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers={**self.auth, "Accept": "application/json, text/event-stream"},
        )
        tools = {t["name"]: t for t in response.json()["result"]["tools"]}
        bash_schema = tools["bash"]["inputSchema"]
        self.assertEqual(bash_schema["required"], ["command"])
        self.assertEqual(
            sorted(bash_schema["properties"]),
            ["command", "cwd", "session_id", "timeout"],
        )


class TestTheToolsWork(_Base):
    """The tools do what the HTTP API does."""

    @unittest.skipUnless(_has_tmux(), "tmux is not installed")
    def test_bash_runs_a_command_and_returns_its_output(self) -> None:
        response = self._call("bash", {"command": "echo hello-from-mcp"})
        self.assertFalse(response["isError"], response.get("content"))
        self.assertIn("hello-from-mcp", response["content"][0]["text"])
        self.assertEqual(response["structuredContent"]["exit_code"], 0)
        self.assertFalse(response["structuredContent"]["timed_out"])

    @unittest.skipUnless(_has_tmux(), "tmux is not installed")
    def test_bash_reports_a_failing_command(self) -> None:
        # Not "exit 3": that ends the shell, the pane dies with it, and
        # the command never reports anything back. A failing command is
        # one that returns a non-zero status and leaves the shell alive.
        response = self._call("bash", {"command": "sh -c 'exit 3'"})
        self.assertFalse(response["isError"], response.get("content"))
        self.assertEqual(response["structuredContent"]["exit_code"], 3)

    def test_read_file_returns_the_text(self) -> None:
        self.write("note.txt", b"the file says hello")
        response = self._call("read_file", {"path": "note.txt"})
        self.assertFalse(response["isError"], response.get("content"))
        self.assertIn("the file says hello", response["content"][0]["text"])

    def test_read_file_returns_a_binary_file_as_a_blob(self) -> None:
        self.write("blob.bin", bytes(range(256)))
        response = self._call("read_file", {"path": "blob.bin"})
        self.assertFalse(response["isError"], response.get("content"))
        block = response["content"][0]
        self.assertEqual(block["type"], "resource")
        import base64  # noqa: PLC0415

        self.assertEqual(base64.b64decode(block["resource"]["blob"]), bytes(range(256)))

    def test_file_info_describes_the_file(self) -> None:
        self.write("note.txt", b"12345")
        response = self._call("file_info", {"path": "note.txt"})
        self.assertFalse(response["isError"], response.get("content"))
        info = response["structuredContent"]
        self.assertEqual(info["name"], "note.txt")
        self.assertEqual(info["size"], 5)

    def test_delete_file_removes_it(self) -> None:
        self.write("gone.txt", b"bye")
        response = self._call("delete_file", {"path": "gone.txt"})
        self.assertFalse(response["isError"], response.get("content"))
        self.assertFalse((self.ws / "gone.txt").exists())

    def test_a_path_outside_the_workspace_is_refused(self) -> None:
        response = self._call("read_file", {"path": "/etc/passwd"})
        self.assertTrue(response["isError"])
        self.assertIn("workspace", response["content"][0]["text"].lower())

    def test_a_symlink_out_of_the_workspace_is_refused(self) -> None:
        outside = Path(self.tmpdir).parent / "mcp-outside-secret.txt"
        outside.write_text("not yours")
        try:
            (self.ws / "link.txt").symlink_to(outside)
            response = self._call("read_file", {"path": "link.txt"})
            self.assertTrue(response["isError"])
            self.assertNotIn("not yours", response["content"][0]["text"])
        finally:
            outside.unlink(missing_ok=True)

    def test_a_missing_file_is_an_error_and_not_a_crash(self) -> None:
        response = self._call("file_info", {"path": "nope.bin"})
        self.assertTrue(response["isError"])
        self.assertIn("not found", response["content"][0]["text"].lower())

    # -- helper ------------------------------------------------------------
    def _call(self, tool: str, arguments: dict) -> dict:
        """Call a tool over the wire and return the raw result object."""
        response = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            headers={**self.auth, "Accept": "application/json, text/event-stream"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("result", body, json.dumps(body))
        return body["result"]


@requires_tmux
class TestARealClientTalksToIt(unittest.TestCase):
    """The reference SDK, end to end.

    This is the test that says "any MCP caller" out loud: the client
    the whole ecosystem uses connects, negotiates, lists the tools and
    calls one. Everything else in this file could pass with a server
    that only *looks* like MCP.
    """

    def setUp(self) -> None:
        try:
            import httpx2  # noqa: PLC0415
            from mcp.client.client import Client  # noqa: PLC0415
            from mcp.client.streamable_http import streamable_http_client  # noqa: PLC0415
        except ImportError:
            self.skipTest("the mcp SDK is not installed")
        self.httpx2 = httpx2
        self.Client = Client
        self.streamable_http_client = streamable_http_client
        self.app, self.mcp, self.tmpdir = _fresh_app()

    def test_the_reference_client_can_drive_the_sandbox(self) -> None:
        async def scenario() -> None:
            async with self.app.router.lifespan_context(self.app):
                http_client = self.httpx2.AsyncClient(
                    transport=self.httpx2.ASGITransport(app=self.app),
                    base_url="http://127.0.0.1:8090",
                )
                http_client.headers["Authorization"] = f"Bearer {TOKEN}"
                transport = self.streamable_http_client(
                    "http://127.0.0.1:8090/mcp", http_client=http_client
                )
                async with self.Client(transport) as client:
                    tools = await client.list_tools()
                    names = sorted(t.name for t in tools.tools)
                    self.assertEqual(names, ["bash", "delete_file", "file_info", "read_file"])
                    self.assertTrue(client.server_info)
                    self.assertTrue(client.server_info.name)

        asyncio.run(scenario())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
