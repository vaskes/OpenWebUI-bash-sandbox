"""Tripwire — the architecture docs must describe the code that exists.

The v2.12.1 cycle ended with a README and a ``docs/ARCHITECTURE.md`` that
still drew a two-service, HTTP-only world: no MCP door, no files door, no
``/metrics``, and a module layout that had quietly lost half of the tree.
Nothing there was a code defect — it was a *documentation* defect, and it
survived because no test compared the docs with the routes and the modules
the code actually has. A reader planning a deployment would have read a
picture that had not been true for a release.

This tripwire closes that gap by comparing three things:

1. the routes the code declares (read from the **AST**, not from grep) with
   the tables in ``docs/ARCHITECTURE.md`` and ``README.md``;
2. the modules in the repository root with the module layout;
3. the MCP tools the server registers with the tools the docs name.

And it pins the numbers a reader acts on: the body limit, the read ceiling,
the relay timeout and the agent's own cap must be the ones in the code, and
every service in the dev compose must be named in the architecture.

A check that cannot be made red is a decoration: ``tools/mutation-check-docs.sh``
breaks each of these on purpose and expects the named test to go red.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHITECTURE = REPO_ROOT / "docs" / "ARCHITECTURE.md"
README = REPO_ROOT / "README.md"

#: The decorators that register a route on the FastAPI/Starlette app.
ROUTE_DECORATORS = {"get", "post", "put", "patch", "delete", "api_route", "websocket"}

#: FastAPI serves this one itself; no source line declares it.
BUILTIN_ROUTES = {"/openapi.json"}

#: The phrase of the world that is gone. The docs must not drift back into it.
STALE_CLAIM = "three loosely-coupled"


def _normalise(path: str) -> str:
    """``/sessions/{session_id}`` and ``/sessions/{id}`` are the same path.

    The query string is not part of the route: ``/files/meta?path=P`` is the
    route ``/files/meta`` with a parameter.
    """
    path = path.split("?", 1)[0]
    return re.sub(r"\{[^}]*\}", "{}", path)


def _declared_routes(source: Path) -> set[str]:
    """The paths the app registers, read from the AST.

    Both shapes count: the decorators (``@app.get("/healthz")``) and the
    explicit route append (``app.router.routes.append(Route("/mcp", ...))``)
    that registers the MCP endpoint as an exact route.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    routes: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        candidate = None
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "app"
            and func.attr in ROUTE_DECORATORS
        ):
            candidate = node.args[0] if node.args else None
        elif isinstance(func, ast.Name) and func.id in {"Route", "StarletteRoute"}:
            candidate = node.args[0] if node.args else None
        if candidate is None:
            continue
        if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
            routes.add(_normalise(candidate.value))

    return routes


def _section(text: str, start: str, end: str) -> str:
    """The text between two markers."""
    begin = text.index(start)
    stop = text.index(end, begin)
    return text[begin:stop]


def _table_paths(section: str) -> set[str]:
    """The paths in the **path column** of the markdown tables of a section.

    Only that column counts. The purpose column of a row may well mention
    another path ("Alias of ``/capture``"), and a path mentioned in prose is
    not a route the table declares.
    """
    paths: set[str] = set()
    for line in section.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 3:
            continue
        path_cell = cells[2]
        for cell in re.findall(r"`(/[^`]*)`", path_cell):
            paths.add(_normalise(cell))
    return paths


def _architecture_agent_section(text: str) -> str:
    return _section(text, "### 1. `sandbox-agent`", "### 2. `openapi-proxy`")


def _architecture_proxy_section(text: str) -> str:
    return _section(text, "### 2. `openapi-proxy`", "### 3. `open-webui`")


def _readme_agent_section(text: str) -> str:
    return _section(text, "### `sandbox-agent`", "### `openapi-proxy`")


def _readme_proxy_section(text: str) -> str:
    return _section(text, "### `openapi-proxy`", "\n## ")


def _module_layout(text: str) -> str:
    """The fenced block under ``## Module layout``."""
    tail = text[text.index("## Module layout") :]
    body = tail[tail.index("```") + 3 :]
    return body[: body.index("```")]


def _mcp_tools(source: Path) -> set[str]:
    """The tools the MCP server registers, read from the AST."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    tools: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            if isinstance(func, ast.Attribute) and func.attr == "tool":
                tools.add(node.name)
    return tools


def _documented_mcp_tools(text: str) -> set[str]:
    """The tools the architecture doc names in its canonical sentence.

    The sentence, not any line that happens to talk about the door: a table
    row may mention the tools in passing, and the canonical list is the one
    place the doc keeps them. Two copies of a list are two facts, and the
    second one is the one that rots.
    """
    for line in text.splitlines():
        if line.strip().startswith("The MCP door exposes"):
            return set(re.findall(r"`(\w+)`", line))
    return set()


def _int_constant(source: Path, name: str) -> int:
    """The value of a module-level integer constant, evaluated from the AST."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        value = None
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    value = node.value
                    break
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == name:
                value = node.value
        if value is None:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, (int, float)):
            return int(value.value)
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Mult):
            return _int_constant_from(value.left) * _int_constant_from(value.right)
    raise AssertionError(f"{name} is not defined in {source.name}")


def _int_constant_from(node: ast.AST) -> int:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return int(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        return _int_constant_from(node.left) * _int_constant_from(node.right)
    raise AssertionError(f"not an integer constant: {ast.dump(node)}")


def _compose_services(path: Path) -> set[str]:
    """The service names of a compose file, read from the text."""
    text = path.read_text(encoding="utf-8")
    services: set[str] = set()
    inside = False
    for line in text.splitlines():
        if line.rstrip() == "services:":
            inside = True
            continue
        if inside:
            if line and not line.startswith(" "):
                break
            match = re.match(r"^  ([a-z][a-z0-9-]*):\s*$", line)
            if match:
                services.add(match.group(1))
    return services


def _limits_rows(text: str) -> list[str]:
    """The rows of the limits table."""
    tail = text[text.index("## Limits") :]
    return [line for line in tail.splitlines() if line.lstrip().startswith("|")]


class TestRoutesAreDocumented(unittest.TestCase):
    """Every route in the code is in the tables — and nothing else is."""

    def setUp(self) -> None:
        self.architecture = ARCHITECTURE.read_text(encoding="utf-8")
        self.readme = README.read_text(encoding="utf-8")

    def _assert_routes_match(self, code: set[str], doc: set[str], where: str) -> None:
        self.assertTrue(code, f"{where}: no routes found in the code — the parser is broken")
        self.assertTrue(doc, f"{where}: no routes found in the table — the parser is broken")
        self.assertEqual(
            code - doc,
            set(),
            f"{where}: routes the code registers but the docs do not list: {sorted(code - doc)}",
        )
        self.assertEqual(
            doc - code - BUILTIN_ROUTES,
            set(),
            f"{where}: routes the docs list but the code does not register: "
            f"{sorted(doc - code - BUILTIN_ROUTES)}",
        )

    def test_agent_routes_are_in_the_architecture_tables(self) -> None:
        code = _declared_routes(REPO_ROOT / "agent_api.py")
        doc = _table_paths(_architecture_agent_section(self.architecture))
        self._assert_routes_match(code, doc, "docs/ARCHITECTURE.md, agent table")

    def test_proxy_routes_are_in_the_architecture_tables(self) -> None:
        code = _declared_routes(REPO_ROOT / "openapi_proxy.py")
        doc = _table_paths(_architecture_proxy_section(self.architecture))
        self._assert_routes_match(code, doc, "docs/ARCHITECTURE.md, proxy table")

    def test_agent_routes_are_in_the_readme_tables(self) -> None:
        code = _declared_routes(REPO_ROOT / "agent_api.py")
        doc = _table_paths(_readme_agent_section(self.readme))
        self._assert_routes_match(code, doc, "README.md, agent table")

    def test_proxy_routes_are_in_the_readme_tables(self) -> None:
        code = _declared_routes(REPO_ROOT / "openapi_proxy.py")
        doc = _table_paths(_readme_proxy_section(self.readme))
        self._assert_routes_match(code, doc, "README.md, proxy table")


class TestTheModuleLayout(unittest.TestCase):
    """The module layout is the tree, not a memory of the tree."""

    def setUp(self) -> None:
        self.layout = _module_layout(ARCHITECTURE.read_text(encoding="utf-8"))

    def test_every_python_module_is_listed(self) -> None:
        for path in sorted(REPO_ROOT.glob("*.py")):
            with self.subTest(module=path.name):
                self.assertIn(path.name, self.layout)

    def test_every_listed_module_exists(self) -> None:
        for token in sorted(set(re.findall(r"[\w./-]+\.py\b", self.layout))):
            with self.subTest(module=token):
                self.assertTrue((REPO_ROOT / token).exists())


class TestTheMcpToolsAreDocumented(unittest.TestCase):
    """The MCP door's tool list is the server's tool list."""

    def test_the_documented_tools_are_the_code_tools(self) -> None:
        code = _mcp_tools(REPO_ROOT / "mcp_server.py")
        self.assertTrue(code, "no MCP tools found in mcp_server.py — the parser is broken")
        doc = _documented_mcp_tools(ARCHITECTURE.read_text(encoding="utf-8"))
        self.assertTrue(doc, "docs/ARCHITECTURE.md does not name the MCP tools")
        self.assertEqual(doc, code)


class TestTheComponentsAreTheComposeServices(unittest.TestCase):
    """The architecture draws the stack that is actually shipped."""

    def setUp(self) -> None:
        self.architecture = ARCHITECTURE.read_text(encoding="utf-8")
        self.services = _compose_services(REPO_ROOT / "docker-compose.yml")

    def test_every_service_is_named_in_the_architecture(self) -> None:
        for service in sorted(self.services):
            with self.subTest(service=service):
                self.assertIn(service, self.architecture)

    def test_every_component_section_is_a_service(self) -> None:
        headings = re.findall(r"^### \d+\. `([^`]+)`", self.architecture, re.MULTILINE)
        self.assertTrue(headings, "the architecture has no component sections")
        self.assertEqual(set(headings), self.services)
        self.assertEqual(len(headings), len(self.services))


class TestTheLimitsAreTheCodeLimits(unittest.TestCase):
    """The numbers in the limits table are the numbers in the code."""

    def setUp(self) -> None:
        self.rows = _limits_rows(ARCHITECTURE.read_text(encoding="utf-8"))

    def _row(self, needle: str) -> str:
        for row in self.rows:
            if needle in row:
                return row
        self.fail(f"the limits table has no row about {needle!r}")

    def test_the_body_limit_is_the_code_limit(self) -> None:
        limit = _int_constant(REPO_ROOT / "agent_api.py", "MAX_BODY_BYTES")
        self.assertIn(f"{limit // (1024 * 1024)} MiB", self._row("request body"))

    def test_both_services_share_the_same_body_limit(self) -> None:
        agent = _int_constant(REPO_ROOT / "agent_api.py", "MAX_BODY_BYTES")
        proxy = _int_constant(REPO_ROOT / "openapi_proxy.py", "MAX_BODY_BYTES")
        self.assertEqual(agent, proxy)

    def test_the_read_ceiling_is_the_code_ceiling(self) -> None:
        limit = _int_constant(REPO_ROOT / "mcp_server.py", "MCP_MAX_READ_BYTES")
        self.assertIn(f"{limit // (1024 * 1024)} MiB", self._row("read_file"))

    def test_the_relay_timeout_is_the_code_timeout(self) -> None:
        seconds = _int_constant(REPO_ROOT / "openapi_proxy.py", "MCP_RELAY_TIMEOUT_SECONDS")
        self.assertIn(f"{seconds} s", self._row("relay"))

    def test_the_agent_cap_is_the_code_cap(self) -> None:
        seconds = _int_constant(REPO_ROOT / "agent_api.py", "MAX_TIMEOUT")
        self.assertIn(f"{seconds} s", self._row("runtime"))


class TestTheStaleWorldIsGone(unittest.TestCase):
    """The world the docs used to draw must not creep back in."""

    def test_the_docs_do_not_claim_three_services(self) -> None:
        for path in (ARCHITECTURE, README):
            with self.subTest(document=path.name):
                self.assertNotIn(STALE_CLAIM, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
