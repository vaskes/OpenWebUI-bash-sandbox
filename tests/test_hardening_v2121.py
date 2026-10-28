"""The v2.12.1 fixes, exercised as behaviour.

The source-level pins live in ``test_review_v2120.py``. This file does
the other half: it *runs* the things that were broken and shows that
they are broken no more.

* a body that arrives in chunks, with no length to read, is refused
  with 413 — in both services;
* a file that turned into a symlink between the check and the open is
  refused with a named reason;
* an MCP read cannot cost the machine more than the ceiling, and a file
  that grows while it is read is refused;
* the MCP relay waits longer than the agent can work.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


def _has_tmux() -> bool:
    """Is tmux on this machine?  The classes that drive a real tmux
    session are skipped when it is not — a missing tool is an
    environment, not a defect."""
    import shutil  # noqa: PLC0415

    return shutil.which("tmux") is not None


requires_tmux = unittest.skipUnless(_has_tmux(), "tmux is not installed")


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOKEN = "test-agent-token-xyz"


def _fresh_agent():
    """Reload the agent against a temp workspace and return (app, workspace)."""
    tmpdir = tempfile.mkdtemp(prefix="agent-harden-test-")
    os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmpdir, "sessions.json")
    os.environ["AGENT_WORKSPACE"] = tmpdir
    os.environ["AGENT_TOKEN"] = TOKEN
    for name in ("agent_api", "mcp_server"):
        sys.modules.pop(name, None)
    from agent_api import app  # noqa: PLC0415

    return app, tmpdir


@requires_tmux
class TestAChunkedBodyIsCounted(unittest.TestCase):
    """A body with no length to read is still a body, and it still counts."""

    def test_the_agent_refuses_a_chunked_body(self) -> None:
        import asyncio  # noqa: PLC0415

        import httpx  # noqa: PLC0415

        app, _ = _fresh_agent()

        seen: list[dict] = []

        async def recorder(scope, receive, send):
            seen.append({k.decode(): v.decode() for k, v in scope.get("headers", [])})
            await app(scope, receive, send)

        async def chunks():
            # Sixty-four chunks of 64 KiB — 4 MiB, over the agent's 2 MiB
            # limit. There is no Content-Length to read, so only the
            # count can catch this.
            for _ in range(64):
                yield b"x" * (64 * 1024)

        async def go():
            transport = httpx.ASGITransport(app=recorder)
            async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
                return await client.post(
                    "/sessions/probe/exec",
                    content=chunks(),
                    headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "Content-Type": "application/json",
                    },
                )

        response = asyncio.run(go())
        self.assertEqual(response.status_code, 413, response.text)
        self.assertIn("too large", response.text.lower())
        # The test is only meaningful if there was no length to read.
        self.assertNotIn("content-length", seen[0])

    def test_the_proxy_refuses_a_chunked_body(self) -> None:
        import asyncio  # noqa: PLC0415

        import httpx  # noqa: PLC0415

        sys.modules.pop("openapi_proxy", None)
        os.environ["OPENAPI_PROXY_TOKEN"] = TOKEN
        os.environ["SANDBOX_URL"] = "http://sandbox-agent:8082"
        os.environ["OPENAPI_PROXY_RATE_LIMIT_RPM"] = "0"
        import openapi_proxy  # noqa: PLC0415

        class _FakeUpstream:
            status_code = 200
            headers = {"content-type": "application/json"}

            async def aiter_bytes(self):
                yield b"{}"

            async def aclose(self) -> None:
                return None

        class _FakeClient:
            def build_request(self, method, url, **kwargs):
                return {"method": method, "url": url, **kwargs}

            async def send(self, request, stream=False):
                return _FakeUpstream()

        # The transport below does not run the lifespan, so the client
        # the route asks for is the one installed here.
        openapi_proxy.app.state.client = _FakeClient()

        async def chunks():
            for _ in range(64):
                yield b"x" * (64 * 1024)

        async def go():
            transport = httpx.ASGITransport(app=openapi_proxy.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
                return await client.post(
                    "/mcp",
                    content=chunks(),
                    headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "Content-Type": "application/json",
                    },
                )

        response = asyncio.run(go())
        self.assertEqual(response.status_code, 413, response.text)

    def test_a_small_chunked_body_still_works(self) -> None:
        # The limit is not a ban on chunked bodies: a legitimate client
        # that streams must keep working. If this fails, the limiter
        # refuses everything and the guard is worse than the hole.
        import asyncio  # noqa: PLC0415

        import httpx  # noqa: PLC0415

        app, _ = _fresh_agent()

        async def chunks():
            yield b'{"command":'
            yield b'"echo hi"}'

        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
                return await client.post(
                    "/sessions/probe/exec",
                    content=chunks(),
                    headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "Content-Type": "application/json",
                    },
                )

        response = asyncio.run(go())
        self.assertNotEqual(response.status_code, 413, response.text)


class TestTheOpenRefusesASymlink(unittest.TestCase):
    """A file that became a symlink between the check and the open is refused."""

    def test_a_symlink_is_not_followed(self) -> None:
        app, tmpdir = _fresh_agent()
        from fastapi import HTTPException  # noqa: PLC0415

        from agent_api import _open_for_download  # noqa: PLC0415

        target = os.path.join(tmpdir, "target.txt")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("secret")
        link = os.path.join(tmpdir, "link.txt")
        os.symlink(target, link)

        with self.assertRaises(HTTPException) as caught:
            _open_for_download(link)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("symlink", str(caught.exception.detail).lower())


class TestTheReadHasACeiling(unittest.TestCase):
    """One MCP read is bounded, whatever the configuration says."""

    def _mcp(self):
        sys.modules.pop("agent_api", None)
        sys.modules.pop("mcp_server", None)
        import mcp_server  # noqa: PLC0415

        return mcp_server

    def test_a_file_over_the_limit_is_refused(self) -> None:
        mcp_server = self._mcp()
        tmpdir = tempfile.mkdtemp(prefix="mcp-read-test-")
        os.environ["AGENT_WORKSPACE"] = tmpdir
        os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmpdir, "sessions.json")
        os.environ["AGENT_TOKEN"] = TOKEN
        sys.modules.pop("agent_api", None)
        import agent_api  # noqa: PLC0415

        path = os.path.join(tmpdir, "big.bin")
        with open(path, "wb") as handle:
            handle.write(b"x" * 4096)

        original = mcp_server._read_limit
        mcp_server._read_limit = lambda: 1024
        try:
            with self.assertRaises(Exception) as caught:
                await_result = agent_api  # noqa: F841 - keep the import meaningful
                import asyncio  # noqa: PLC0415

                asyncio.run(mcp_server.read_file(path))
        finally:
            mcp_server._read_limit = original
        self.assertIn("over the limit", str(caught.exception))

    def test_a_file_that_grows_while_it_is_read_is_refused(self) -> None:
        # The stat said the file was small; by the time the bytes were
        # read it was not. The read asks for one byte more than the
        # limit allows, and the extra byte is the proof.
        mcp_server = self._mcp()
        tmpdir = tempfile.mkdtemp(prefix="mcp-grow-test-")
        os.environ["AGENT_WORKSPACE"] = tmpdir
        os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmpdir, "sessions.json")
        os.environ["AGENT_TOKEN"] = TOKEN
        sys.modules.pop("agent_api", None)
        import agent_api  # noqa: PLC0415

        path = os.path.join(tmpdir, "grow.bin")
        with open(path, "wb") as handle:
            handle.write(b"x" * 4096)

        original_limit = mcp_server._read_limit
        original_getsize = mcp_server.os.path.getsize
        mcp_server._read_limit = lambda: 1024
        # The stat lies: the file reports itself as small.
        mcp_server.os.path.getsize = lambda _p: 100
        try:
            with self.assertRaises(Exception) as caught:
                import asyncio  # noqa: PLC0415

                asyncio.run(mcp_server.read_file(path))
        finally:
            mcp_server._read_limit = original_limit
            mcp_server.os.path.getsize = original_getsize
        self.assertIn("grew", str(caught.exception))

    def test_the_read_does_not_pull_the_whole_file_into_memory(self) -> None:
        # The refusal has to cost the machine nothing: the read asks
        # for one byte more than the limit allows and stops there. A
        # read of the whole file would take a gigabyte of memory to
        # find out that the file is over the limit — the very thing
        # the limit exists to prevent.
        mcp_server = self._mcp()
        tmpdir = tempfile.mkdtemp(prefix="mcp-readsize-test-")
        os.environ["AGENT_WORKSPACE"] = tmpdir
        os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmpdir, "sessions.json")
        os.environ["AGENT_TOKEN"] = TOKEN
        sys.modules.pop("agent_api", None)
        import agent_api  # noqa: PLC0415

        path = os.path.join(tmpdir, "small.bin")
        with open(path, "wb") as handle:
            handle.write(b"x" * 100)

        asked: list[int | None] = []

        class _Spy:
            def __init__(self, real):
                self._real = real

            def read(self, size=None):
                asked.append(size)
                return self._real.read(size)

            def fileno(self):
                # The real handle is a file object, and the read asks it
                # for the size of the file it points at. A double that
                # does not model that is a double that fails for the
                # wrong reason.
                return self._real.fileno()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._real.close()
                return False

        original_limit = mcp_server._read_limit
        original_open = agent_api._open_for_download
        mcp_server._read_limit = lambda: 1024
        agent_api._open_for_download = lambda name: _Spy(open(name, "rb"))
        try:
            import asyncio  # noqa: PLC0415

            asyncio.run(mcp_server.read_file(path))
        finally:
            mcp_server._read_limit = original_limit
            agent_api._open_for_download = original_open
        self.assertEqual(asked, [1025], f"the read asked for {asked!r}")


class TestTheRelayWaitsLongerThanTheAgentWorks(unittest.TestCase):
    """The relay's timeout has to be above the agent's own cap."""

    def test_the_relay_timeout_is_above_the_agent_cap(self) -> None:
        import openapi_proxy  # noqa: PLC0415

        self.assertGreater(openapi_proxy.MCP_RELAY_TIMEOUT_SECONDS, 600)
        self.assertGreater(
            openapi_proxy.MCP_RELAY_TIMEOUT_SECONDS,
            openapi_proxy.PROXY_REQUEST_TIMEOUT_SECONDS,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
