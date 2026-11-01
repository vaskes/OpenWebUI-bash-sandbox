"""The vision door — ``vision_analyze`` — and the fence around it.

The wave this file belongs to gave the agent eyes. What the door does is
small: it takes a picture that is **already in the workspace**, hands it
to the **operator's** endpoint and returns the answer as text.

What makes it a door and not a convenience is everything around that
sentence:

* the picture is a file inside the workspace, read through the same
  fence every other file operation uses — a path that points out is
  refused, and a symlink that points out is not followed;
* the endpoint is the operator's. The tool takes a **path**, not a URL:
  a door that fetches a URL the model names is a retrieval door wearing
  a different hat, and retrieval is not this project's (v2.13.0 — the
  research capability belongs to another MCP, on another host);
* the answer is bounded, and a truncated answer says that it is
  truncated;
* the door is **closed** unless the operator configured an endpoint, and
  a closed door says so instead of pretending.

And a check that cannot be made red is a decoration:
``tools/mutation-check-v2230.sh`` breaks each of these on purpose and
expects the named test to go red.
"""

from __future__ import annotations

import asyncio
import atexit
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

#: A one-pixel PNG. Small enough to be a string, real enough to be a
#: picture: the door never decodes it, but a file that is not a picture
#: would make the mime check untestable.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

#: The endpoint the tests point the door at. It is not a real host: the
#: transport is faked, so nothing leaves the machine.
ENDPOINT = "http://vision.invalid/v1/chat/completions"


def _fresh_app(**environment: str):
    """Reload the agent against a temp workspace and the given environment.

    Every knob is read from the environment once, at import time — so a
    module that is reloaded against a new environment needs the config
    reloaded with it.
    """
    tmpdir = tempfile.mkdtemp(prefix="agent-vision-test-")
    atexit.register(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
    os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmpdir, "sessions.json")
    os.environ["AGENT_WORKSPACE"] = tmpdir
    os.environ["AGENT_TOKEN"] = TOKEN
    os.environ.pop("AGENT_MCP_ALLOWED_HOSTS", None)
    for name in ("AGENT_VISION_URL", "AGENT_VISION_API_KEY", "AGENT_VISION_MODEL"):
        os.environ.pop(name, None)
    for name, value in environment.items():
        os.environ[name] = value
    for name in ("agent_api", "mcp_server", "agent_config"):
        if name in sys.modules:
            del sys.modules[name]
    from agent_api import SESSIONS, app  # noqa: E402

    SESSIONS.clear()
    return app, tmpdir


def _call(coro):
    """Run one tool call and return its result."""
    return asyncio.run(coro)


def _refusal(coro) -> str:
    """Run one tool call that must be refused, and return the reason."""
    with unittest.TestCase().assertRaises(Exception) as caught:
        _call(coro)
    return str(caught.exception)


class _Recorder:
    """A fake endpoint: it records what it was asked and answers.

    The handler is the httpx shape: it takes a request and returns a
    response. Every call is kept, so a test can assert that the door
    did **not** call the endpoint at all — which is the property that
    matters when the model tries to make it fetch a URL.
    """

    def __init__(self, answer: str = "a small picture of a cat", status: int = 200) -> None:
        self.answer = answer
        self.status = status
        self.calls: list = []

    def handler(self, request):
        import httpx

        self.calls.append(request)
        if self.status >= 400:
            return httpx.Response(self.status, text="the model is on fire")
        return httpx.Response(
            self.status,
            json={
                "choices": [{"message": {"role": "assistant", "content": self.answer}}],
            },
        )

    def transport(self):
        import httpx

        return httpx.MockTransport(self.handler)


class _Base(unittest.TestCase):
    """A fresh agent per test, with the door loaded against it."""

    def setUp(self) -> None:
        self.app, self.tmpdir = _fresh_app(
            AGENT_VISION_URL=ENDPOINT,
            AGENT_VISION_MODEL="the-vision-model",
        )
        self.ws = Path(self.tmpdir)
        from mcp_server import vision_analyze  # noqa: PLC0415

        self.vision_analyze = vision_analyze

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def put(self, name: str, data: bytes) -> Path:
        """Put a file into the workspace directly, as the model would."""
        target = self.ws / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def picture(self, name: str = "shot.png") -> Path:
        return self.put(name, PNG)


class TestTheDoorIsTheOperators(_Base):
    """The endpoint is the operator's, and only the operator's."""

    def test_the_door_is_closed_when_no_endpoint_is_configured(self) -> None:
        """A capability the operator has not granted is not a capability.

        The default deployment has no endpoint, and the tool says so —
        it does not fall back to something else, and it does not answer
        from its own knowledge.
        """
        self.app, self.tmpdir = _fresh_app()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        from mcp_server import vision_analyze  # noqa: PLC0415

        reason = _refusal(vision_analyze("shot.png"))
        self.assertIn("closed", reason)
        self.assertIn("AGENT_VISION_URL", reason)

    def test_the_tool_takes_no_url(self) -> None:
        """The model cannot hand the door a URL.

        The door takes a path inside the workspace and the operator's
        endpoint, and nothing else. A URL argument would turn this into
        a retrieval door, and retrieval is not this project's.
        """
        import mcp_server  # noqa: PLC0415

        listed = {tool.name: tool for tool in mcp_server.mcp._tool_manager.list_tools()}  # noqa: SLF001
        schema = listed["vision_analyze"].parameters
        self.assertIn("path", schema["properties"])
        self.assertIn("prompt", schema["properties"])
        for forbidden in ("url", "uri", "endpoint", "base_url", "image_url", "src"):
            self.assertNotIn(forbidden, schema["properties"])
        self.assertEqual(
            sorted(schema["properties"]),
            ["path", "prompt"],
        )

    def test_a_url_in_the_path_is_a_path_and_not_a_fetch(self) -> None:
        """A URL-shaped path is a path.

        It is resolved against the workspace like any other name, it is
        not there, and the door refuses it. Nothing is fetched: the
        endpoint is not called at all.
        """
        recorder = _Recorder()
        from agent_api import analyze_image  # noqa: PLC0415

        with self.assertRaises(Exception) as caught:
            asyncio.run(
                analyze_image(
                    "https://example.invalid/picture.png",
                    "what is this?",
                    transport=recorder.transport(),
                )
            )
        # The refusal is about a file that is not there — the URL was
        # resolved against the workspace like any other name.
        self.assertIn("not found", str(caught.exception))
        self.assertEqual(recorder.calls, [])


class TestThePictureStaysInsideTheWorkspace(_Base):
    """The picture is a file inside the workspace, and nowhere else."""

    def test_a_picture_outside_the_workspace_is_refused(self) -> None:
        outside = self.ws.parent / f"owbs-outside-{os.getpid()}.png"
        self.addCleanup(outside.unlink, True)
        outside.write_bytes(PNG)
        recorder = _Recorder()
        from agent_api import analyze_image  # noqa: PLC0415

        with self.assertRaises(Exception) as caught:
            asyncio.run(
                analyze_image(str(outside), "what is this?", transport=recorder.transport())
            )
        self.assertIn("outside the workspace", str(caught.exception))
        # The picture outside did not leave: the endpoint was not called.
        self.assertEqual(recorder.calls, [])

    def test_a_picture_through_a_symlink_that_points_out_is_refused(self) -> None:
        outside = self.ws.parent / f"owbs-link-{os.getpid()}.png"
        self.addCleanup(outside.unlink, True)
        outside.write_bytes(PNG)
        os.symlink(outside, self.ws / "shot.png")
        recorder = _Recorder()
        from agent_api import analyze_image  # noqa: PLC0415

        with self.assertRaises(Exception) as caught:
            asyncio.run(analyze_image("shot.png", "what is this?", transport=recorder.transport()))
        self.assertIn("outside the workspace", str(caught.exception))
        self.assertEqual(recorder.calls, [])

    def test_a_file_that_is_not_a_picture_is_refused(self) -> None:
        self.put("notes.txt", b"just text")
        recorder = _Recorder()
        from agent_api import analyze_image  # noqa: PLC0415

        with self.assertRaises(Exception) as caught:
            asyncio.run(analyze_image("notes.txt", "what is this?", transport=recorder.transport()))
        self.assertIn("not a picture", str(caught.exception))
        self.assertEqual(recorder.calls, [])


class TestTheDoorIsBounded(_Base):
    """Every budget of this door is a budget, and an over-budget thing is refused."""

    def test_a_picture_over_the_limit_is_refused(self) -> None:
        self.app, self.tmpdir = _fresh_app(
            AGENT_VISION_URL=ENDPOINT,
            AGENT_VISION_MODEL="the-vision-model",
            AGENT_VISION_MAX_BYTES="65536",
        )
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.ws = Path(self.tmpdir)
        self.put("big.png", b"\x89PNG" + b"0" * 200_000)
        recorder = _Recorder()
        from agent_api import analyze_image  # noqa: PLC0415

        with self.assertRaises(Exception) as caught:
            asyncio.run(analyze_image("big.png", "what is this?", transport=recorder.transport()))
        reason = str(caught.exception)
        self.assertIn("over the limit", reason)
        # The refusal names the size and the limit: a refusal that does
        # not is a refusal the model cannot act on.
        self.assertIn("65536", reason)
        self.assertEqual(recorder.calls, [])

    def test_a_question_over_the_limit_is_refused(self) -> None:
        self.app, self.tmpdir = _fresh_app(
            AGENT_VISION_URL=ENDPOINT,
            AGENT_VISION_MODEL="the-vision-model",
            AGENT_VISION_MAX_PROMPT_BYTES="16",
        )
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.ws = Path(self.tmpdir)
        self.picture()
        recorder = _Recorder()
        from agent_api import analyze_image  # noqa: PLC0415

        with self.assertRaises(Exception) as caught:
            asyncio.run(
                analyze_image(
                    "shot.png",
                    "x" * 100,
                    transport=recorder.transport(),
                )
            )
        self.assertIn("over the limit", str(caught.exception))
        self.assertEqual(recorder.calls, [])

    def test_the_answer_is_bounded_and_says_so(self) -> None:
        self.app, self.tmpdir = _fresh_app(
            AGENT_VISION_URL=ENDPOINT,
            AGENT_VISION_MODEL="the-vision-model",
            AGENT_VISION_MAX_ANSWER_BYTES="1024",
        )
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.ws = Path(self.tmpdir)
        self.picture()
        recorder = _Recorder(answer="y" * 5000)
        from agent_api import analyze_image  # noqa: PLC0415

        result = asyncio.run(
            analyze_image("shot.png", "what is this?", transport=recorder.transport())
        )
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["text"].encode("utf-8")), 1024)


class TestTheAnswerComesBack(_Base):
    """The picture goes to the operator's endpoint, and the answer comes back."""

    def test_the_picture_goes_to_the_configured_endpoint(self) -> None:
        self.picture()
        recorder = _Recorder()
        from agent_api import analyze_image  # noqa: PLC0415

        result = asyncio.run(
            analyze_image("shot.png", "what is this?", transport=recorder.transport())
        )
        self.assertEqual(len(recorder.calls), 1)
        request = recorder.calls[0]
        self.assertEqual(str(request.url), ENDPOINT)
        body = request.content.decode("utf-8")
        import json

        payload = json.loads(body)
        self.assertEqual(payload["model"], "the-vision-model")
        parts = payload["messages"][0]["content"]
        self.assertEqual(parts[0]["text"], "what is this?")
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(result["text"], "a small picture of a cat")
        self.assertFalse(result["truncated"])

    def test_the_bearer_is_sent_when_the_operator_gave_one(self) -> None:
        self.app, self.tmpdir = _fresh_app(
            AGENT_VISION_URL=ENDPOINT,
            AGENT_VISION_MODEL="the-vision-model",
            AGENT_VISION_API_KEY="s3cret",
        )
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.ws = Path(self.tmpdir)
        self.picture()
        recorder = _Recorder()
        from agent_api import analyze_image  # noqa: PLC0415

        asyncio.run(analyze_image("shot.png", "what is this?", transport=recorder.transport()))
        self.assertEqual(recorder.calls[0].headers.get("authorization"), "Bearer s3cret")

    def test_an_endpoint_that_fails_is_a_refusal_with_the_reason(self) -> None:
        self.picture()
        recorder = _Recorder(status=500)
        from agent_api import analyze_image  # noqa: PLC0415

        with self.assertRaises(Exception) as caught:
            asyncio.run(analyze_image("shot.png", "what is this?", transport=recorder.transport()))
        self.assertIn("500", str(caught.exception))

    def test_an_endpoint_that_says_nothing_is_a_refusal(self) -> None:
        self.picture()
        recorder = _Recorder(answer="")
        from agent_api import analyze_image  # noqa: PLC0415

        with self.assertRaises(Exception) as caught:
            asyncio.run(analyze_image("shot.png", "what is this?", transport=recorder.transport()))
        self.assertIn("no text", str(caught.exception))


class TestTheDoorLeavesATrace(_Base):
    """The counting is the audit trail this door exists to keep."""

    def test_the_request_is_counted(self) -> None:
        self.picture()
        recorder = _Recorder()
        from agent_api import METRICS, analyze_image  # noqa: PLC0415

        before = METRICS.render()
        asyncio.run(analyze_image("shot.png", "what is this?", transport=recorder.transport()))
        after = METRICS.render()
        self.assertIn("sandbox_agent_vision_requests_total 0", before)
        self.assertIn("sandbox_agent_vision_requests_total 1", after)

    def test_a_refusal_is_counted(self) -> None:
        from agent_api import METRICS  # noqa: PLC0415

        reason = _refusal(self.vision_analyze("nothing-here.png"))
        self.assertIn("cannot look at the picture", reason)
        self.assertIn("sandbox_agent_vision_refused_total 1", METRICS.render())


class TestTheToolOverTheWire(_Base):
    """The door as the model sees it."""

    def test_the_tool_answers_with_the_text(self) -> None:
        self.picture()
        import agent_api  # noqa: PLC0415

        async def fake(path: str, prompt: str, **_: object) -> dict:
            return {
                "name": "shot.png",
                "path": str(self.ws / "shot.png"),
                "size": len(PNG),
                "text": "a small picture of a cat",
                "truncated": False,
                "model": "the-vision-model",
            }

        original = agent_api.analyze_image
        agent_api.analyze_image = fake
        self.addCleanup(setattr, agent_api, "analyze_image", original)

        result = _call(self.vision_analyze("shot.png", "what is this?"))
        self.assertEqual(result["text"], "a small picture of a cat")
        self.assertFalse(result["truncated"])

    def test_the_tool_refuses_a_path_outside_the_workspace(self) -> None:
        outside = self.ws.parent / f"owbs-outside-{os.getpid()}.png"
        self.addCleanup(outside.unlink, True)
        outside.write_bytes(PNG)
        reason = _refusal(self.vision_analyze(str(outside)))
        self.assertIn("cannot look at the picture", reason)
        self.assertIn("outside the workspace", reason)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
