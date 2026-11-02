"""The browser door: a browser in the sandbox's own container.

Wave F, and the shape of it is the operator's decision of 2026-11-02: the
sandbox is **one atomic service of one Docker**, so the browser is a
process inside the sandbox's own container, driven by our own MCP tools.
There is no second container, no browser sidecar, and no code from any
other sandbox.

What this module is:

* a **policy** — which hosts the tool may fetch, how large an answer may
  be, how long a navigation may take — written as pure functions so it can
  be tested without a browser;
* a **driver** — Chromium, through Playwright, warm between calls;
* the **door** itself, which the MCP tools call.

What it is **not**: a fence against the model. The jail's egress is
unrestricted and measured (``docs/SECURITY.md``), so the model's own shell
reaches any host it likes with ``curl``; and a same-uid sibling can
attach to the browser's driver and speak CDP to it (measured 2026-11-02).
What the allowlist buys is narrower and real: **our tool** does not become
a second, unaudited door to the open web. The document says so rather
than implying otherwise.

The Chromium posture is measured, not assumed:

* Playwright launches Chromium with ``--remote-debugging-pipe`` — a
  socketpair, not a TCP port. There is no devtools endpoint on localhost
  to curl (measured: the browser process opens **zero** listening TCP
  sockets);
* Chromium has two sandbox backends and the jail allows neither: the
  setuid helper is inert under ``no-new-privileges``, and the namespace
  backend needs unprivileged user namespaces, which the jail's seccomp
  filter refuses (``unshare -Ur`` is ``EPERM`` there). So in the jail
  Chromium runs with ``--no-sandbox``, and the consequence is stated in
  ``docs/ROADMAP.md``: a renderer compromise there is code execution as
  uid 1000 — the same uid the model's own shell runs as.

The credentials rules are the reason the profile rules are not
negotiable: a browser profile is a set of files the browser *trusts*, and
the workspace is attacker-controlled. So this door keeps no profile of its
own — every session gets a fresh, in-memory context with no stored
cookie, no token and no saved password, and nothing is ever written into
the workspace except the PNG the caller asked for.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Any
from urllib.parse import urlsplit

from agent_config import (
    BROWSER_ALLOWED_HOSTS,
    BROWSER_MAX_SCREENSHOT_BYTES,
    BROWSER_MAX_TEXT_BYTES,
    BROWSER_SETTLE_MS,
    BROWSER_TIMEOUT_SECONDS,
)

logger = logging.getLogger("agent.browser")

#: Where the browser's own temporary files live. A subdirectory of the
#: tmpfs that is mounted at /tmp, so it is outside the workspace and it
#: dies with the container. It is created here rather than in the image
#: because the tmpfs mount shadows whatever the image put under /tmp.
BROWSER_TMP_DIR = "/tmp/browser"

#: The schemes the door speaks. A browser that could open ``file://``
#: would read the workspace's files through a path the allowlist never
#: sees; a browser that could open ``data:`` or ``chrome:`` would be a
#: different tool wearing the same name.
BROWSER_SCHEMES = ("http", "https")

#: The flags every launch carries. ``--disable-dev-shm-usage`` keeps
#: Chromium off the container's small ``/dev/shm``, which is the usual
#: reason a browser dies in Docker.
BROWSER_ARGS = ["--disable-dev-shm-usage"]

#: How the browser is launched — and this is not a detail.
#:
#: Playwright's ``headless=True`` **without a channel** launches the
#: *headless shell*: a second Chromium, which is a separate download
#: (273 MB) and a separate binary. The image installs one Chromium (the
#: full build, 403 MB) and skips the shell with ``--no-shell``, so the
#: driver has to say which binary it wants: ``channel="chromium"`` is
#: what makes the driver use the full build in headless mode.
#:
#: Measured in the driver's own source: the executable name is
#: ``options.headless ? "chromium-headless-shell" : "chromium"`` when no
#: channel is given. Without the channel the browser in this image does
#: not exist, and the tool answers "Executable doesn't exist".
BROWSER_LAUNCH = {"channel": "chromium"}

#: The environment the browser gets — **and only this one**.
#:
#: Two reasons, and the second is the important one.
#:
#: 1. Chromium takes its home directory from the passwd entry
#:    (``getpwuid()``) and **ignores ``$HOME``**. In this container that
#:    home is ``/home/agent`` on a read-only rootfs, so the crashpad
#:    handler cannot create its database and Chromium dies at launch
#:    with ``SIGTRAP`` and "chrome_crashpad_handler: --database is
#:    required" (measured, in the CI job that builds the image). The
#:    fix is the one Chromium documents: writable, pre-created
#:    ``XDG_CONFIG_HOME`` and ``XDG_CACHE_HOME`` on the tmpfs.
#: 2. Playwright's ``env`` **replaces** the environment rather than
#:    adding to it, so the browser is given a minimal environment on
#:    purpose: the agent's own token is in this process's environment,
#:    and a same-uid sibling can read ``/proc/<pid>/environ`` — so a
#:    browser that inherited the agent's environment would carry the
#:    agent's credential in a file the model can read. The browser gets
#:    the paths and nothing else.
BROWSER_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": BROWSER_TMP_DIR,
    "TMPDIR": "/tmp",
    "LANG": "C.UTF-8",
    "XDG_CONFIG_HOME": os.path.join(BROWSER_TMP_DIR, "config"),
    "XDG_CACHE_HOME": os.path.join(BROWSER_TMP_DIR, "cache"),
}


class BrowserRefusedError(Exception):
    """The browser door refused before anything was fetched."""


# ---------------------------------------------------------------------------
# The policy — pure functions, no browser, no I/O
# ---------------------------------------------------------------------------
def url_host(url: str) -> str:
    """The host of a URL, lowercased. Empty when there is none."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def host_matches(host: str, pattern: str) -> bool:
    """Does ``host`` match one entry of the allowlist?

    Three forms, and only three:

    * ``*`` — any host. The operator's explicit choice.
    * ``example.com`` — that host and nothing else.
    * ``*.example.com`` — that host and everything under it.
    """
    if not host or not pattern:
        return False
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        suffix = pattern[1:]  # ".example.com"
        return host == suffix[1:] or host.endswith(suffix)
    return host == pattern


def host_allowed(url: str, hosts: tuple[str, ...]) -> bool:
    """May the door fetch this URL, according to the operator's list?"""
    host = url_host(url)
    if not host:
        return False
    return any(host_matches(host, pattern) for pattern in hosts)


def check_url(url: str, hosts: tuple[str, ...]) -> str:
    """Check a URL against the door's rules, and return it unchanged.

    The rules, in the order they are asked:

    * the scheme is http or https;
    * the URL is absolute — a bare ``example.com`` is refused, because a
      URL whose meaning depends on a base is not a URL;
    * the host is on the operator's list.
    """
    raw = str(url or "").strip()
    if not raw:
        raise BrowserRefusedError("the URL is required")
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise BrowserRefusedError(f"the URL is not a URL: {exc}") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in BROWSER_SCHEMES:
        if not scheme:
            raise BrowserRefusedError(
                f"the URL must be absolute and start with http:// or https:// — got {raw!r}"
            )
        raise BrowserRefusedError(
            f"the browser speaks http and https, and nothing else — got {scheme!r}"
        )
    if not parts.netloc:
        raise BrowserRefusedError(f"the URL has no host: {raw!r}")
    if not hosts:
        raise BrowserRefusedError(
            "the browser door is closed: AGENT_BROWSER_ALLOWED_HOSTS is not set, "
            "so there is nowhere the tool may fetch"
        )
    if not host_allowed(raw, hosts):
        raise BrowserRefusedError(
            f"{url_host(raw)} is not on the list of hosts this tool may fetch ({', '.join(hosts)})"
        )
    return raw


def bound_text(text: str, limit: int) -> tuple[str, bool]:
    """Cut the text to the budget, and say whether it was cut."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", "ignore"), True


# ---------------------------------------------------------------------------
# The driver — Chromium, through Playwright
# ---------------------------------------------------------------------------
class PlaywrightDriver:
    """Chromium in this container, warm between calls.

    One browser for the whole service, one **context** per session: the
    context is the credential store, and a fresh one carries no cookie,
    no token and no saved password. The browser's own temporary directory
    lives under the tmpfs at /tmp, never in the workspace.
    """

    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...] | None = None,
        timeout_ms: int | None = None,
        settle_ms: int | None = None,
    ) -> None:
        self._allowed_hosts = tuple(
            allowed_hosts if allowed_hosts is not None else BROWSER_ALLOWED_HOSTS
        )
        self._timeout_ms = timeout_ms if timeout_ms is not None else BROWSER_TIMEOUT_SECONDS * 1000
        self._settle_ms = settle_ms if settle_ms is not None else BROWSER_SETTLE_MS
        self._playwright: Any = None
        self._browser: Any = None
        self._sandbox = "off"
        self._contexts: dict[str, Any] = {}
        self._pages: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        """Launch the browser, once, and keep it."""
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright  # noqa: PLC0415

        # The dirs Chromium insists on having, on the tmpfs. They are
        # created here, by the agent's own user, before the launch —
        # Chromium's crashpad handler aborts the browser when it cannot
        # create its database.
        for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
            os.makedirs(BROWSER_ENV[name], mode=0o700, exist_ok=True)
        os.makedirs(BROWSER_TMP_DIR, mode=0o700, exist_ok=True)
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
                **BROWSER_LAUNCH,
                headless=True,
                env=dict(BROWSER_ENV),
            )
            self._sandbox = "on"
        except Exception as exc:  # noqa: BLE001 - the fallback is deliberate
            if "sandbox" not in str(exc).lower():
                await self._stop_playwright()
                raise
            # Neither sandbox backend is available here: the setuid helper
            # is inert under no-new-privileges, and the namespace backend
            # needs user namespaces the jail's seccomp filter refuses.
            logger.warning(
                "chromium could not start with its own sandbox (%s); "
                "running it without — see docs/ROADMAP.md, wave F",
                str(exc)[:200],
            )
            self._browser = await self._playwright.chromium.launch(
                **BROWSER_LAUNCH,
                headless=True,
                env=dict(BROWSER_ENV),
                args=[*BROWSER_ARGS, "--no-sandbox"],
            )
            self._sandbox = "off"

    async def stop(self) -> None:
        """Close everything the driver opened."""
        for session_id in list(self._contexts):
            try:
                await self._close_session(session_id)
            except Exception:  # noqa: BLE001 - a shutdown must not raise
                logger.exception("browser cannot close the session %s", session_id)
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                logger.exception("browser cannot close the browser")
            self._browser = None
        await self._stop_playwright()
        shutil.rmtree(BROWSER_TMP_DIR, ignore_errors=True)

    async def _stop_playwright(self) -> None:
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                logger.exception("browser cannot stop the playwright driver")
            self._playwright = None

    # -- the route handler -------------------------------------------------
    async def _route(self, route: Any, request: Any) -> None:
        """Refuse a navigation to a host the operator did not list.

        Subresources are the page's own business: the operator's list is
        about sites, and a page that loads its stylesheet from a CDN is
        not an escape. A **navigation** is different — it is a page the
        tool would hand to the model — so it is checked.

        The limit of this check is measured, not guessed: Chromium follows
        an HTTP redirect inside its own network stack, and the browser
        does not hand that follow-up to the route handler. So the final
        URL is checked again after the navigation, and a page that ended
        up somewhere else is refused.
        """
        try:
            is_navigation = request.is_navigation_request()
            is_top = request.frame.parent_frame is None
        except Exception:  # noqa: BLE001 - an odd request is not a reason to crash
            is_navigation, is_top = False, False
        if is_navigation and is_top and not host_allowed(request.url, self._allowed_hosts):
            logger.info("browser refused a navigation to %s", url_host(request.url))
            await route.abort()
            return
        await route.continue_()

    # -- the session -------------------------------------------------------
    async def _page_for(self, session_id: str) -> Any:
        page = self._pages.get(session_id)
        if page is not None:
            return page
        context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
        )
        await context.route("**/*", self._route)
        page = await context.new_page()
        self._contexts[session_id] = context
        self._pages[session_id] = page
        return page

    async def _close_session(self, session_id: str) -> None:
        context = self._contexts.pop(session_id, None)
        self._pages.pop(session_id, None)
        if context is None:
            return
        try:
            await context.close()
        except Exception:  # noqa: BLE001
            logger.exception("browser cannot close the context of %s", session_id)

    # -- the operations ----------------------------------------------------
    async def open(self, session_id: str, url: str) -> dict[str, Any]:
        """Navigate, and hand back what the page says.

        No policy here: the driver drives the browser, and the rules live
        in :func:`browser_open`, where they can be tested without a
        browser at all. What the driver does know is the one thing only
        the browser can tell — **where the page ended up**, because
        Chromium follows an HTTP redirect inside its own network stack
        and never hands that follow-up to the route handler.
        """
        async with self._lock:
            await self.start()
            page = await self._page_for(session_id)
            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self._timeout_ms,
                )
            except Exception as exc:  # noqa: BLE001 - the detail reaches the model
                raise BrowserRefusedError(f"the page did not load: {str(exc)[:300]}") from exc
            if self._settle_ms:
                try:
                    await page.wait_for_timeout(self._settle_ms)
                except Exception:  # noqa: BLE001
                    logger.exception("browser cannot settle the page")
            final = str(page.url)
            try:
                title = await page.title()
            except Exception:  # noqa: BLE001
                title = ""
            try:
                text = await page.inner_text("body")
            except Exception as exc:  # noqa: BLE001
                raise BrowserRefusedError(
                    f"the page has no readable text: {str(exc)[:200]}"
                ) from exc
        return {
            "url": final,
            "title": title,
            "text": text,
            "truncated": False,
            "sandbox": self._sandbox,
        }

    async def screenshot(self, session_id: str, *, full_page: bool = False) -> bytes:
        """A PNG of the page as it stands."""
        async with self._lock:
            await self.start()
            page = self._pages.get(session_id)
            if page is None:
                raise BrowserRefusedError(
                    "there is no page open in this session — open one before asking "
                    "for a screenshot"
                )
            return await page.screenshot(type="png", full_page=full_page)

    async def close(self, session_id: str) -> bool:
        """Drop the context and the profile of one session."""
        async with self._lock:
            had = session_id in self._contexts
            await self._close_session(session_id)
        return had


# ---------------------------------------------------------------------------
# The door
# ---------------------------------------------------------------------------
_DRIVER: PlaywrightDriver | None = None


def driver() -> PlaywrightDriver:
    """The one driver of this service, made on first use."""
    global _DRIVER
    if _DRIVER is None:
        _DRIVER = PlaywrightDriver()
    return _DRIVER


async def shutdown() -> None:
    """Close the browser with the agent."""
    global _DRIVER
    if _DRIVER is None:
        return
    driver_, _DRIVER = _DRIVER, None
    await driver_.stop()


async def browser_open(
    session_id: str,
    url: str,
    *,
    allowed_hosts: tuple[str, ...] | None = None,
    driver_: PlaywrightDriver | None = None,
) -> dict[str, Any]:
    """Open a URL in the sandbox's own browser, and return its text.

    The rules, in the order they are asked:

    * the door is configured — an empty allowlist is a closed door;
    * the URL is absolute, http or https, and its host is on the list;
    * after the navigation, the page is **still** on the list. Chromium
      follows an HTTP redirect inside its own network stack and does not
      hand that follow-up to the route handler (measured 2026-11-02), so
      this second check is what stops a redirect from walking the browser
      off the list. A page that ended up elsewhere is refused and its
      content is discarded;
    * the text is bounded, and a truncated text says that it was cut.
    """
    hosts = tuple(allowed_hosts if allowed_hosts is not None else BROWSER_ALLOWED_HOSTS)
    checked = check_url(url, hosts)
    result = await (driver_ or driver()).open(session_id, checked)
    final = str(result.get("url") or checked)
    if not host_allowed(final, hosts):
        raise BrowserRefusedError(
            f"the page ended up at {url_host(final)}, which is not on the list of "
            f"hosts this tool may fetch ({', '.join(hosts)})"
        )
    text, truncated = bound_text(str(result.get("text") or ""), BROWSER_MAX_TEXT_BYTES)
    _count("browser_requests_total")
    return {
        "url": final,
        "title": str(result.get("title") or ""),
        "text": text,
        "truncated": truncated,
        "sandbox": str(result.get("sandbox") or "unknown"),
    }


async def browser_screenshot(
    session_id: str,
    path: str,
    *,
    full_page: bool = False,
    driver_: PlaywrightDriver | None = None,
) -> dict[str, Any]:
    """Take a PNG of the page and write it into the workspace."""
    from agent_api import write_workspace_bytes  # noqa: PLC0415

    shot = await (driver_ or driver()).screenshot(session_id, full_page=full_page)
    if len(shot) > BROWSER_MAX_SCREENSHOT_BYTES:
        raise BrowserRefusedError(
            f"the screenshot is {len(shot)} bytes, over the limit of "
            f"{BROWSER_MAX_SCREENSHOT_BYTES} bytes"
        )
    written = write_workspace_bytes(path, shot)
    _count("browser_screenshots_total")
    return written


async def browser_close(
    session_id: str,
    *,
    driver_: PlaywrightDriver | None = None,
) -> bool:
    """Drop the browser context of a session."""
    closed = await (driver_ or driver()).close(session_id)
    return closed


def _count(name: str) -> None:
    try:
        from agent_api import METRICS  # noqa: PLC0415

        METRICS.inc(name)
    except Exception:  # noqa: BLE001 - a counter must never break an operation
        logger.exception("browser cannot count %s", name)
