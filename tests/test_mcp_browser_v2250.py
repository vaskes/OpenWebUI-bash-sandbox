"""The browser door — ``browser_open``, ``browser_screenshot``,
``browser_close`` — and the fence around it.

Wave F put a browser **inside the sandbox's own container**: the
operator's decision of 2026-11-02 is that the sandbox is one atomic
service of one Docker, so there is no browser sidecar and no second
container. Playwright drives Chromium, and both live in the image.

What makes the door a door and not a convenience is everything around it:

* the door is **closed** unless the operator listed the hosts the tool
  may fetch, and a closed door says so instead of pretending;
* a URL the model names is only fetched when its host is on that list,
  and the page must **still** be on the list when it has loaded — a
  redirect cannot walk the browser off it (measured: Chromium follows an
  HTTP redirect inside its own network stack and does not hand that
  follow-up to the route handler);
* the answer is bounded, and a truncated answer says that it is cut;
* the screenshot lands in the workspace through the same fence every
  other write uses;
* the browser keeps **no credentials**: every session gets a fresh
  context, with no cookie, no token and no saved password, and nothing
  is ever written into the workspace but the PNG the caller asked for.

The list is a policy about **one tool**, not a boundary: the jail's
egress is unrestricted and measured, so the model's own shell reaches the
open web with ``curl`` anyway. The document says so rather than implying
otherwise.

And a check that cannot be made red is a decoration:
``tools/mutation-check-v2250.sh`` breaks each of these on purpose and
expects the named test to go red.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOKEN = "test-agent-token-xyz"

#: A one-pixel PNG. The door never decodes it, but a file that is not a
#: picture would make the write untestable.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def _fresh_app(**environment: str):
    """Reload the agent against a temp workspace and the given environment.

    Every knob is read from the environment once, at import time — so a
    module that is reloaded against a new environment needs the config
    reloaded with it. ``agent_browser`` reads the browser knobs the same
    way, so it is reloaded with them.
    """
    tmpdir = tempfile.mkdtemp(prefix="agent-browser-test-")
    atexit.register(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
    os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmpdir, "sessions.json")
    os.environ["AGENT_WORKSPACE"] = tmpdir
    os.environ["AGENT_TOKEN"] = TOKEN
    os.environ.pop("AGENT_MCP_ALLOWED_HOSTS", None)
    os.environ.pop("AGENT_BROWSER_ALLOWED_HOSTS", None)
    for name in (
        "AGENT_BROWSER_TIMEOUT",
        "AGENT_BROWSER_MAX_TEXT_BYTES",
        "AGENT_BROWSER_MAX_SCREENSHOT_BYTES",
        "AGENT_BROWSER_SETTLE_MS",
    ):
        os.environ.pop(name, None)
    for name, value in environment.items():
        os.environ[name] = value
    for name in ("agent_api", "mcp_server", "agent_config", "agent_browser"):
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


class _FakeDriver:
    """A driver that answers without a browser.

    It records what it was asked, so a test can assert that the door did
    **not** reach the browser at all — which is the property that matters
    when the model tries to make it fetch a host that is not on the list.
    """

    def __init__(
        self,
        *,
        final_url: str = "https://allowed.example/page",
        text: str = "the rendered page",
        title: str = "A page",
        screenshot: bytes = PNG,
        sandbox: str = "on",
    ) -> None:
        self.final_url = final_url
        self.text = text
        self.title = title
        self.screenshot_bytes = screenshot
        self.sandbox = sandbox
        self.calls: list[tuple] = []
        self.sessions: set[str] = set()

    async def open(self, session_id: str, url: str) -> dict:
        self.calls.append(("open", session_id, url))
        self.sessions.add(session_id)
        return {
            "url": self.final_url,
            "title": self.title,
            "text": self.text,
            "truncated": False,
            "sandbox": self.sandbox,
        }

    async def screenshot(self, session_id: str, *, full_page: bool = False) -> bytes:
        self.calls.append(("screenshot", session_id, full_page))
        return self.screenshot_bytes

    async def close(self, session_id: str) -> bool:
        self.calls.append(("close", session_id))
        had = session_id in self.sessions
        self.sessions.discard(session_id)
        return had


class _FakeRoute:
    """A route, as the driver sees one."""

    def __init__(self) -> None:
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class _FakeFrame:
    def __init__(self, top: bool) -> None:
        self.parent_frame = None if top else object()


class _FakeRequest:
    """A request, as the driver sees one."""

    def __init__(self, url: str, *, navigation: bool = True, top: bool = True) -> None:
        self.url = url
        self._navigation = navigation
        self.frame = _FakeFrame(top)

    def is_navigation_request(self) -> bool:
        return self._navigation


class _FakePlaywrightBrowser:
    """A browser, as the driver sees it after a launch."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakePlaywright:
    """A Playwright, as the driver sees it.

    It records every launch, so a test can say **which** Chromium the
    driver asked for.
    """

    def __init__(self, launches: list[dict], *, fail_first: bool = False) -> None:
        self.launches = launches
        self.fail_first = fail_first
        self.chromium = self
        self.browser = _FakePlaywrightBrowser()

    async def launch(self, **kwargs) -> _FakePlaywrightBrowser:
        self.launches.append(kwargs)
        if self.fail_first and len(self.launches) == 1:
            raise RuntimeError("Failed to move to new namespace: sandbox is not available")
        return self.browser

    async def start(self) -> _FakePlaywright:
        return self

    async def stop(self) -> None:
        return None


class _Base(unittest.TestCase):
    """A fresh agent per test, with the browser door loaded against it."""

    HOSTS = "allowed.example,*.sub.example"

    def setUp(self) -> None:
        self.app, self.tmpdir = _fresh_app(
            AGENT_BROWSER_ALLOWED_HOSTS=self.HOSTS,
        )
        self.ws = Path(self.tmpdir)
        import agent_browser  # noqa: PLC0415
        from mcp_server import (  # noqa: PLC0415
            browser_close,
            browser_open,
            browser_screenshot,
        )

        self.agent_browser = agent_browser
        self.browser_open = browser_open
        self.browser_screenshot = browser_screenshot
        self.browser_close = browser_close

    def use_driver(self, driver: _FakeDriver) -> _FakeDriver:
        """Make the door talk to *driver* instead of to Chromium."""
        original = self.agent_browser.driver
        self.agent_browser.driver = lambda: driver  # type: ignore[assignment]
        self.addCleanup(setattr, self.agent_browser, "driver", original)
        return driver


class TestTheDriverLaunchesTheBrowserTheImageCarries(_Base):
    """The image has one Chromium, and the driver has to ask for that one.

    Playwright's ``headless=True`` **without a channel** launches the
    *headless shell* — a second Chromium, a separate download. This image
    installs the full build and skips the shell (``--no-shell``), so the
    driver has to say which binary it wants.

    Measured in the driver's own source: without a channel the executable
    name is ``options.headless ? "chromium-headless-shell" : "chromium"``.
    With ``channel="chromium"`` it is the full build — the one the image
    carries.

    This is not a detail: without it the browser in the image does not
    exist, and the tool answers "Executable doesn't exist at
    /ms-playwright/chromium_headless_shell-…".
    """

    def _drive_with(self, fake: _FakePlaywright) -> list[dict]:
        """Start a driver against a fake Playwright, and return the launches."""
        module = types.ModuleType("playwright")
        api = types.ModuleType("playwright.async_api")
        api.async_playwright = lambda: fake  # type: ignore[attr-defined]
        module.async_api = api  # type: ignore[attr-defined]
        saved = {name: sys.modules.get(name) for name in ("playwright", "playwright.async_api")}
        sys.modules["playwright"] = module
        sys.modules["playwright.async_api"] = api
        self.addCleanup(
            lambda: [
                sys.modules.pop(name, None)
                if saved[name] is None
                else sys.modules.__setitem__(name, saved[name])
                for name in saved
            ]
        )
        driver = self.agent_browser.PlaywrightDriver()
        _call(driver.start())
        self.addCleanup(shutil.rmtree, "/tmp/browser", ignore_errors=True)
        return fake.launches

    def test_the_launch_asks_for_the_full_chromium(self) -> None:
        launches = self._drive_with(_FakePlaywright([]))
        self.assertEqual(len(launches), 1, launches)
        self.assertEqual(launches[0].get("channel"), "chromium", launches)
        self.assertTrue(launches[0].get("headless"), launches)

    def test_the_fallback_asks_for_the_same_chromium(self) -> None:
        """The no-sandbox fallback must not ask for a different binary."""
        launches = self._drive_with(_FakePlaywright([], fail_first=True))
        self.assertEqual(len(launches), 2, launches)
        for kwargs in launches:
            self.assertEqual(kwargs.get("channel"), "chromium", kwargs)
        self.assertIn("--no-sandbox", launches[1].get("args", []), launches[1])
        self.assertNotIn("--no-sandbox", launches[0].get("args", []), launches[0])

    def test_the_browser_gets_writable_dirs_on_the_tmpfs(self) -> None:
        """Chromium takes its home from the passwd entry and ignores $HOME.

        In this container that home is on a read-only rootfs, so the
        crashpad handler cannot create its database and Chromium dies at
        launch ("chrome_crashpad_handler: --database is required",
        SIGTRAP — measured in the CI job that builds the image). The fix
        is the one Chromium documents: writable, pre-created
        XDG_CONFIG_HOME and XDG_CACHE_HOME on the tmpfs.
        """
        launches = self._drive_with(_FakePlaywright([]))
        env = launches[0].get("env") or {}
        for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
            self.assertIn(name, env, env)
            self.assertTrue(env[name].startswith("/tmp/"), env)
            self.assertTrue(Path(env[name]).is_dir(), f"{name} does not exist: {env[name]}")

    def test_the_browser_does_not_inherit_the_agent_environment(self) -> None:
        """The browser must not carry the agent's credential.

        Playwright's ``env`` replaces the environment rather than adding
        to it, and that is used here on purpose: the agent's token is in
        this process's environment, and a same-uid sibling can read
        ``/proc/<pid>/environ`` — so a browser that inherited the agent's
        environment would carry the agent's credential in a file the
        model can read.
        """
        os.environ["AGENT_BROWSER_TEST_SECRET"] = "must-not-reach-the-browser"
        self.addCleanup(os.environ.pop, "AGENT_BROWSER_TEST_SECRET", None)
        launches = self._drive_with(_FakePlaywright([]))
        env = launches[0].get("env") or {}
        self.assertNotIn("AGENT_BROWSER_TEST_SECRET", env, sorted(env))
        self.assertNotIn("AGENT_TOKEN", env, sorted(env))
        self.assertNotIn("AGENT_INBOUND_TOKEN", env, sorted(env))
        self.assertEqual(
            sorted(env),
            [
                "HOME",
                "LANG",
                "PATH",
                "TMPDIR",
                "XDG_CACHE_HOME",
                "XDG_CONFIG_HOME",
            ],
            sorted(env),
        )


class TestTheListIsTheOperators(unittest.TestCase):
    """The allowlist is the operator's, and it is closed by default."""

    def check(self, url: str, hosts: tuple[str, ...]) -> None:
        from agent_browser import check_url  # noqa: PLC0415

        check_url(url, hosts)

    def refusal(self, url: str, hosts: tuple[str, ...]) -> str:
        from agent_browser import BrowserRefusedError, check_url  # noqa: PLC0415

        with self.assertRaises(BrowserRefusedError) as caught:
            check_url(url, hosts)
        return str(caught.exception)

    def test_an_empty_list_closes_the_door(self) -> None:
        reason = self.refusal("https://example.com/", ())
        self.assertIn("AGENT_BROWSER_ALLOWED_HOSTS", reason)
        self.assertIn("closed", reason)

    def test_a_host_that_is_not_on_the_list_is_refused(self) -> None:
        reason = self.refusal("https://elsewhere.example/", ("allowed.example",))
        self.assertIn("elsewhere.example", reason)
        self.assertIn("not on the list", reason)

    def test_an_exact_host_matches_only_itself(self) -> None:
        self.check("https://allowed.example/page", ("allowed.example",))
        reason = self.refusal("https://www.allowed.example/", ("allowed.example",))
        self.assertIn("www.allowed.example", reason)

    def test_a_wildcard_covers_the_domain_and_what_is_under_it(self) -> None:
        self.check("https://sub.example/page", ("*.sub.example",))
        self.check("https://deep.sub.example/page", ("*.sub.example",))

    def test_a_wildcard_does_not_cover_a_lookalike(self) -> None:
        reason = self.refusal("https://notsub.example/", ("*.sub.example",))
        self.assertIn("notsub.example", reason)

    def test_the_star_covers_every_host(self) -> None:
        self.check("https://anything.example/", ("*",))

    def test_a_file_url_is_refused(self) -> None:
        reason = self.refusal("file:///etc/passwd", ("*",))
        self.assertIn("http and https", reason)

    def test_a_data_url_is_refused(self) -> None:
        reason = self.refusal("data:text/html,<b>x", ("*",))
        self.assertIn("http and https", reason)

    def test_a_bare_host_is_refused_with_the_fix(self) -> None:
        reason = self.refusal("allowed.example/page", ("allowed.example",))
        self.assertIn("http://", reason)
        self.assertIn("https://", reason)

    def test_a_url_with_no_host_is_refused(self) -> None:
        reason = self.refusal("http:///page", ("*",))
        self.assertIn("no host", reason)


class TestThePageCannotWalkOffTheList(_Base):
    """A redirect cannot walk the browser off the operator's list."""

    def test_a_page_that_ends_up_elsewhere_is_refused(self) -> None:
        self.use_driver(
            _FakeDriver(final_url="https://elsewhere.example/page"),
        )
        reason = _refusal(self.browser_open("https://allowed.example/page"))
        self.assertIn("cannot open the page", reason)
        self.assertIn("elsewhere.example", reason)

    def test_the_driver_is_not_reached_when_the_host_is_refused(self) -> None:
        driver = self.use_driver(_FakeDriver())
        _refusal(self.browser_open("https://elsewhere.example/page"))
        self.assertEqual(driver.calls, [])

    def test_a_page_that_stays_on_the_list_is_read(self) -> None:
        driver = self.use_driver(
            _FakeDriver(final_url="https://allowed.example/page", text="hello there"),
        )
        result = _call(self.browser_open("https://allowed.example/page"))
        self.assertEqual(result["text"], "hello there")
        self.assertFalse(result["truncated"])
        self.assertEqual(len(driver.calls), 1)

    def test_a_page_that_redirects_within_the_list_is_read(self) -> None:
        self.use_driver(_FakeDriver(final_url="https://deep.sub.example/page"))
        result = _call(self.browser_open("https://allowed.example/page"))
        self.assertEqual(result["url"], "https://deep.sub.example/page")


class TestTheAnswerIsBounded(_Base):
    """The text that comes back is bounded, and says when it was cut."""

    def test_a_long_page_is_cut_and_says_so(self) -> None:
        self.use_driver(_FakeDriver(text="x" * 100_000))
        result = _call(self.browser_open("https://allowed.example/page"))
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["text"].encode("utf-8")), 64 * 1024)

    def test_a_short_page_is_not_cut(self) -> None:
        self.use_driver(_FakeDriver(text="short"))
        result = _call(self.browser_open("https://allowed.example/page"))
        self.assertFalse(result["truncated"])
        self.assertEqual(result["text"], "short")


class TestTheScreenshotGoesThroughTheFence(_Base):
    """The PNG lands in the workspace, through wave A's fence."""

    def test_the_screenshot_lands_in_the_workspace(self) -> None:
        self.use_driver(_FakeDriver())
        _call(self.browser_open("https://allowed.example/page"))
        result = _call(self.browser_screenshot("shot.png"))
        written = self.ws / "shot.png"
        self.assertTrue(written.is_file())
        self.assertEqual(written.read_bytes(), PNG)
        self.assertEqual(result["size"], len(PNG))
        self.assertEqual(result["name"], "shot.png")

    def test_a_path_outside_the_workspace_is_refused(self) -> None:
        self.use_driver(_FakeDriver())
        _call(self.browser_open("https://allowed.example/page"))
        outside = self.ws.parent / f"owbs-outside-{os.getpid()}.png"
        self.addCleanup(outside.unlink, True)
        reason = _refusal(self.browser_screenshot(str(outside)))
        self.assertIn("cannot take the screenshot", reason)
        self.assertIn("outside the workspace", reason)
        # The refusal names the caller's **file**, not the directory it
        # happens to live in. That is what tells the fence apart from the
        # layers behind it: a write that got past the fence is caught by
        # the parent-directory check, and that check names the directory.
        self.assertIn(str(outside), reason)
        self.assertFalse(outside.exists())

    def test_a_screenshot_without_a_page_is_refused(self) -> None:
        driver = self.use_driver(_FakeDriver())

        async def no_page(session_id: str, *, full_page: bool = False) -> bytes:
            raise RuntimeError("there is no page open in this session")

        driver.screenshot = no_page  # type: ignore[assignment]
        reason = _refusal(self.browser_screenshot("shot.png"))
        self.assertIn("no page open", reason)

    def test_an_oversized_screenshot_is_refused(self) -> None:
        self.use_driver(_FakeDriver(screenshot=b"\x89PNG" + b"0" * (5 * 1024 * 1024)))
        _call(self.browser_open("https://allowed.example/page"))
        reason = _refusal(self.browser_screenshot("shot.png"))
        self.assertIn("over the limit", reason)
        self.assertFalse((self.ws / "shot.png").exists())


class TestTheBrowserKeepsNoCredentials(_Base):
    """Every session gets a clean context, and closing one drops it."""

    def test_each_session_gets_its_own_context(self) -> None:
        driver = self.use_driver(_FakeDriver())
        _call(self.browser_open("https://allowed.example/page", session_id="one"))
        _call(self.browser_open("https://allowed.example/page", session_id="two"))
        self.assertEqual(driver.sessions, {"one", "two"})

    def test_closing_a_session_drops_its_context(self) -> None:
        driver = self.use_driver(_FakeDriver())
        _call(self.browser_open("https://allowed.example/page", session_id="one"))
        result = _call(self.browser_close(session_id="one"))
        self.assertTrue(result["closed"])
        self.assertEqual(driver.sessions, set())

    def test_closing_a_session_that_had_no_page_says_so(self) -> None:
        self.use_driver(_FakeDriver())
        result = _call(self.browser_close(session_id="nothing"))
        self.assertFalse(result["closed"])

    def test_the_real_driver_says_so_too(self) -> None:
        """The real driver, not the fake: the context is what is counted.

        The fake keeps its own set of sessions, so a test that used only
        the fake would pin the fake. This one asks the driver the wave
        actually ships — and it needs no browser: closing a session that
        never opened a page is answered from the registry.
        """
        from agent_browser import PlaywrightDriver  # noqa: PLC0415

        real = PlaywrightDriver(allowed_hosts=("allowed.example",))
        self.assertFalse(_call(real.close("nothing")))


class TestTheRouteHandlerIsTheFence(_Base):
    """The driver's route handler: a navigation is checked, a subresource is not."""

    def route(self, request: _FakeRequest) -> _FakeRoute:
        from agent_browser import PlaywrightDriver  # noqa: PLC0415

        driver = PlaywrightDriver(allowed_hosts=("allowed.example",))
        route = _FakeRoute()
        _call(driver._route(route, request))  # noqa: SLF001 - the fence is the thing under test
        return route

    def test_a_navigation_to_a_host_that_is_not_on_the_list_is_aborted(self) -> None:
        route = self.route(_FakeRequest("https://elsewhere.example/page"))
        self.assertTrue(route.aborted)
        self.assertFalse(route.continued)

    def test_a_navigation_to_a_host_on_the_list_goes_through(self) -> None:
        route = self.route(_FakeRequest("https://allowed.example/page"))
        self.assertTrue(route.continued)
        self.assertFalse(route.aborted)

    def test_a_subresource_is_the_page_s_own_business(self) -> None:
        route = self.route(
            _FakeRequest("https://elsewhere.example/style.css", navigation=False),
        )
        self.assertTrue(route.continued)
        self.assertFalse(route.aborted)

    def test_a_sub_frame_is_not_a_top_level_navigation(self) -> None:
        route = self.route(
            _FakeRequest("https://elsewhere.example/page", navigation=True, top=False),
        )
        self.assertTrue(route.continued)
        self.assertFalse(route.aborted)


class TestTheToolsAreTheDocumentedTools(_Base):
    """The catalogue, the descriptions and the schemas."""

    def tools(self) -> dict:
        import mcp_server  # noqa: PLC0415

        return {tool.name: tool for tool in mcp_server.mcp._tool_manager.list_tools()}  # noqa: SLF001

    def test_the_three_tools_are_in_the_catalogue(self) -> None:
        from mcp_server import tool_names  # noqa: PLC0415

        names = tool_names()
        for name in ("browser_open", "browser_screenshot", "browser_close"):
            self.assertIn(name, names)

    def test_the_description_states_the_rules(self) -> None:
        description = self.tools()["browser_open"].description or ""
        self.assertIn("http://", description)
        self.assertIn("https://", description)
        self.assertIn("allows", description)
        self.assertIn("redirect", description)

    def test_the_screenshot_description_states_the_fence(self) -> None:
        description = self.tools()["browser_screenshot"].description or ""
        self.assertIn("workspace", description)
        self.assertIn("refused", description)

    def test_the_open_schema_takes_a_url_and_a_session(self) -> None:
        properties = self.tools()["browser_open"].parameters["properties"]
        self.assertEqual(set(properties), {"url", "session_id"})

    def test_the_screenshot_schema_takes_a_path(self) -> None:
        properties = self.tools()["browser_screenshot"].parameters["properties"]
        self.assertEqual(set(properties), {"path", "session_id", "full_page"})

    def test_the_close_schema_takes_a_session(self) -> None:
        properties = self.tools()["browser_close"].parameters["properties"]
        self.assertEqual(set(properties), {"session_id"})


class TestTheDoorLeavesATrace(_Base):
    """The counting is the audit trail this door exists to keep."""

    def test_the_request_is_counted(self) -> None:
        self.use_driver(_FakeDriver())
        from agent_api import METRICS  # noqa: PLC0415

        before = METRICS.render()
        _call(self.browser_open("https://allowed.example/page"))
        after = METRICS.render()
        self.assertIn("sandbox_agent_browser_requests_total 0", before)
        self.assertIn("sandbox_agent_browser_requests_total 1", after)

    def test_a_refusal_is_counted(self) -> None:
        self.use_driver(_FakeDriver())
        from agent_api import METRICS  # noqa: PLC0415

        reason = _refusal(self.browser_open("https://elsewhere.example/page"))
        self.assertIn("cannot open the page", reason)
        self.assertIn("sandbox_agent_browser_refused_total 1", METRICS.render())

    def test_a_screenshot_is_counted(self) -> None:
        self.use_driver(_FakeDriver())
        from agent_api import METRICS  # noqa: PLC0415

        before = METRICS.render()
        _call(self.browser_open("https://allowed.example/page"))
        _call(self.browser_screenshot("shot.png"))
        after = METRICS.render()
        self.assertIn("sandbox_agent_browser_screenshots_total 0", before)
        self.assertIn("sandbox_agent_browser_screenshots_total 1", after)


class TestTheDoorOverTheWire(_Base):
    """The door as the model sees it."""

    def test_the_tool_answers_with_the_text(self) -> None:
        self.use_driver(_FakeDriver(text="the rendered page", title="A page"))
        result = _call(self.browser_open("https://allowed.example/page"))
        self.assertEqual(result["text"], "the rendered page")
        self.assertEqual(result["title"], "A page")
        self.assertEqual(result["session_id"], "mcp")
        self.assertEqual(result["sandbox"], "on")

    def test_the_tool_refuses_a_host_that_is_not_on_the_list(self) -> None:
        self.use_driver(_FakeDriver())
        reason = _refusal(self.browser_open("https://elsewhere.example/page"))
        self.assertIn("cannot open the page", reason)
        self.assertIn("not on the list", reason)

    def test_the_closed_door_says_so(self) -> None:
        self.app, self.tmpdir = _fresh_app()
        from mcp_server import browser_open  # noqa: PLC0415

        reason = _refusal(browser_open("https://allowed.example/page"))
        self.assertIn("closed", reason)
        self.assertIn("AGENT_BROWSER_ALLOWED_HOSTS", reason)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
