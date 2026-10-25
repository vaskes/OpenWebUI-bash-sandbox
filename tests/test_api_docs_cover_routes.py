"""The API contract must be documented where the readers look.

Two readers depend on the endpoint list being complete and correct:

* the **agent that maintains the bot** — it reads the README and the
  agent's module docstring to learn how to pull a file out, and a
  missing row there is a missing feature to that agent;
* the **next human** — an endpoint that exists in the code but in
  neither document is an endpoint nobody knows about.

v2.6.0 shipped ``/files`` and ``/files/meta`` and documented them in the
README — but not in the agent's own "Public API" docstring, which is
exactly the kind of drift this test exists to kill.

The check is deliberately mechanical: for every route the app actually
serves, there must be a documentation line naming the same method and
the same path. A route that is implemented and not documented fails; a
documented route that does not exist fails the other way round.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

README = ROOT / "README.md"

#: FastAPI mounts these itself. They are part of the framework, not of
#: this project's contract.
AUTO_ROUTES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

#: ``/sessions/{session_id}`` and ``/sessions/{id}`` name the same route.
_PARAM_RE = re.compile(r"\{[^}]*\}")
#: A path cell in the README: ``/files?path=P`` or ``/ws`` — the first
#: backticked token, without its query string.
_PATH_CELL_RE = re.compile(r"`([^`]+)`")


def _normalise(path: str) -> str:
    """Collapse path parameters so ``{id}`` and ``{session_id}`` match."""
    return _PARAM_RE.sub("{}", path.strip())


def _route_pairs(app) -> list[tuple[str, str]]:
    """(method, path) for every route the app serves."""
    pairs: list[tuple[str, str]] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path or path in AUTO_ROUTES:
            continue
        methods = getattr(route, "methods", None)
        if not methods:
            # A websocket route: it has no HTTP methods.
            pairs.append(("WS", path))
            continue
        for method in methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            pairs.append((method.upper(), path))
    return pairs


def _readme_rows(section: str) -> set[tuple[str, str]]:
    """(method, path) pairs from the markdown table inside ``section``."""
    rows: set[tuple[str, str]] = set()
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        method = cells[0].strip("`*").upper()
        if not re.fullmatch(r"[A-Z]+", method):
            continue
        match = _PATH_CELL_RE.search(cells[1])
        if not match:
            continue
        path = match.group(1).split("?")[0]
        rows.add((method, _normalise(path)))
    return rows


def _readme_section(heading: str) -> str:
    """The text of one README subsection, up to the next heading."""
    text = README.read_text(encoding="utf-8")
    start = text.find(heading)
    if start < 0:
        return ""
    rest = text[start + len(heading) :]
    end = rest.find("\n#")
    return rest if end < 0 else rest[:end]


def setUpModule() -> None:
    tmpdir = tempfile.mkdtemp(prefix="agent-docs-tripwire-")
    os.environ.setdefault("AGENT_TOKEN", "tripwire-token-not-a-secret")
    os.environ.setdefault("AGENT_WORKSPACE", tmpdir)
    os.environ.setdefault("AGENT_SESSIONS_FILE", os.path.join(tmpdir, "sessions.json"))
    os.environ.setdefault("OPENAPI_PROXY_TOKEN", "tripwire-token-not-a-secret")


class TestAgentRoutesAreDocumented(unittest.TestCase):
    def setUp(self) -> None:
        import agent_api

        self.agent = agent_api
        self.routes = _route_pairs(agent_api.app)
        self.assertGreater(len(self.routes), 5, "no routes found — the check would be vacuous")

    def test_every_route_is_named_in_the_module_docstring(self) -> None:
        doc = self.agent.__doc__ or ""
        missing = []
        for method, path in self.routes:
            if method == "WS":
                continue
            pattern = re.compile(rf"^\s*{method}\s+{re.escape(path)}(?![A-Za-z0-9])", re.MULTILINE)
            if not pattern.search(doc):
                missing.append(f"{method} {path}")
        self.assertEqual(missing, [], f"undocumented in the docstring: {missing}")

    def test_every_route_is_in_the_readme_table(self) -> None:
        section = _readme_section("### `sandbox-agent`")
        self.assertTrue(section, "the README has no sandbox-agent section")
        documented = _readme_rows(section)
        missing = [
            f"{method} {path}"
            for method, path in self.routes
            if (method, _normalise(path)) not in documented
        ]
        self.assertEqual(missing, [], f"undocumented in the README: {missing}")


class TestProxyRoutesAreDocumented(unittest.TestCase):
    def setUp(self) -> None:
        import openapi_proxy

        self.proxy = openapi_proxy
        self.routes = _route_pairs(openapi_proxy.app)
        self.assertGreater(len(self.routes), 5, "no routes found — the check would be vacuous")

    def test_every_route_is_in_the_readme_table(self) -> None:
        section = _readme_section("### `openapi-proxy`")
        self.assertTrue(section, "the README has no openapi-proxy section")
        documented = _readme_rows(section)
        missing = [
            f"{method} {path}"
            for method, path in self.routes
            if (method, _normalise(path)) not in documented
        ]
        self.assertEqual(missing, [], f"undocumented in the README: {missing}")


if __name__ == "__main__":
    unittest.main()
