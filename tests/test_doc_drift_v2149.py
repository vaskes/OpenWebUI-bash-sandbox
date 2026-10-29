"""Tripwire — the documentation must not drift away from the code.

The operator's complaint about this repository was not a bug report: it
was *"the documentation keeps going stale and getting neglected"*. The
review waves before this one found code defects; this wave is about the
**other** half — the claims the docs make about the code.

A doc is a claim. A claim that is false is worse than a missing one,
because a reader acts on it. So this tripwire does not check that a
phrase is present; it **compares the docs with the code** and fails when
they disagree:

1. every knob the operator template (``.env.example``) offers must be a
   knob the compose file actually interpolates — a template entry that
   compose hardcodes, or never passes, is a promise the stack cannot
   keep;
2. every variable a compose file *requires* (``:?``) must be documented
   in prose somewhere in ``docs/`` — an undocumented required variable
   is a deployment that stops with an error nobody can look up;
3. the timeout the configuration table advertises must be the timeout the
   proxy ships;
4. the status codes the handoff contract enumerates must include every
   code the file door can actually answer;
5. the metrics table must name every counter and gauge the services
   declare;
6. the docs must not claim that the compose file does not contain a
   value that the compose file does contain.

Every one of these is measured against the **code**, not against a
checked-in expectation, so the tripwire cannot drift into being a
tautology. And a check that cannot be made red is a decoration:
``tools/mutation-check-v2149.sh`` breaks each property on purpose and
expects the named test to go red.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"
COMPOSE = REPO_ROOT / "docker-compose.yml"
COMPOSE_PRODUCTION = REPO_ROOT / "docker-compose.production.yml"
CONFIGURATION = REPO_ROOT / "docs" / "CONFIGURATION.md"
HANDOFF_BINARY = REPO_ROOT / "docs" / "HANDOFF-binary-transport.md"
README = REPO_ROOT / "README.md"

#: The compose files a deployment can start. Both are shipped, so both
#: are held to the same standard.
COMPOSE_FILES = (COMPOSE, COMPOSE_PRODUCTION)

#: Knobs whose value is a path inside the container, or a build argument:
#: they are not deployment knobs and the template must not offer them as
#: if they were.
NOT_OPERATOR_KNOBS = {
    "AGENT_WORKSPACE",
    "AGENT_SESSIONS_FILE",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _template_knobs() -> set[str]:
    """The knobs ``.env.example`` offers, commented or not.

    A commented-out entry is an offer: the operator uncomments it and
    expects it to work.
    """
    knobs = set()
    for line in _read(ENV_EXAMPLE).splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", stripped)
        if match:
            knobs.add(match.group(1))
    return knobs


def _compose_environment() -> dict[str, str]:
    """Every environment entry the dev compose file passes, as source text.

    The value is kept verbatim (``:?`` guards and all) because the whole
    point is to tell an interpolated variable from a literal one.
    """
    source = _read(COMPOSE)
    entries: dict[str, str] = {}
    for match in re.finditer(r"-\s+([A-Z][A-Z0-9_]*)=(.*)$", source, re.MULTILINE):
        entries[match.group(1)] = match.group(2).strip()
    return entries


def _interpolated_knobs(path: Path) -> set[str]:
    """Every variable the compose file interpolates, wherever it lives.

    A knob does not have to sit in an ``environment:`` block to be a
    knob: ``pids_limit``, ``cap_drop``, ``security_opt``, ``read_only``
    and the image's ``build.args`` are all deployment knobs, and all of
    them are read from the operator's env file. Looking only at
    ``environment:`` would call them "not passed" — a false accusation
    against a file that does the right thing.
    """
    return set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)[:}]", _read(path)))


def _pinned_knobs(path: Path) -> set[str]:
    """The variables a compose file pins to a literal value.

    ``- AGENT_LOG_LEVEL=INFO`` names the variable but ignores the
    operator's env file: the container always gets ``INFO``.
    """
    pinned = set()
    for match in re.finditer(r"-\s+([A-Z][A-Z0-9_]*)=(.*)$", _read(path), re.MULTILINE):
        if "$" not in match.group(2):
            pinned.add(match.group(1))
    return pinned


def _required_by_compose(path: Path) -> set[str]:
    """The variables a compose file refuses to start without (``:?``)."""
    required = set()
    for match in re.finditer(r"\$\{([A-Z][A-Z0-9_]*):\?", _read(path)):
        required.add(match.group(1))
    return required


def _declared_metrics() -> dict[str, set[str]]:
    """The metrics each service declares, read from the AST.

    ``Metrics("sandbox_agent")`` sets the prefix; the names come from the
    ``counter()`` and ``gauge()`` calls. Reading the AST rather than
    grepping keeps a mention in a docstring from counting as a metric.
    """
    declared: dict[str, set[str]] = {}
    for module, prefix in (
        ("agent_api.py", "sandbox_agent"),
        ("openapi_proxy.py", "sandbox_proxy"),
    ):
        source = (REPO_ROOT / module).read_text(encoding="utf-8")
        tree = ast.parse(source)
        names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("counter", "gauge"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            value = node.args[0].value
            if isinstance(value, str):
                names.add(f"{prefix}_{value}")
        declared[prefix] = names
    # A parser that found nothing would make every check that uses it
    # pass vacuously — it would "find no drift" because it looked at
    # nothing. It must find the metrics it is looking for.
    assert declared.get("sandbox_agent"), "the metrics parser found no agent metric"
    assert declared.get("sandbox_proxy"), "the metrics parser found no proxy metric"
    return declared


def _shipped_exec_timeout() -> float:
    """The exec timeout the proxy ships, read from the code.

    ``proxy_core.py`` holds it as a module constant; the compose file
    passes the same number as a default. Both are read, and the test
    asserts they agree — a doc that copies one of them is only right if
    the two cannot drift apart.
    """
    tree = ast.parse((REPO_ROOT / "proxy_core.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "EXEC_TIMEOUT_SECONDS":
                    return float(node.value.value)
    raise AssertionError("proxy_core.py does not define EXEC_TIMEOUT_SECONDS")


class TestTheTemplateOffersOnlyKnobsThatWork(unittest.TestCase):
    """A knob the template offers must be a knob the stack honours."""

    def test_every_offered_knob_is_passed_to_a_container(self) -> None:
        """A knob in .env.example must reach a container as an env var.

        The template is the **development** stack's template: the
        production stack is a different file with a different set of
        variables (no Open WebUI at all, and its own token name), and it
        is documented separately. So the check is against the compose
        file the template belongs to.

        The knob does not have to sit in an ``environment:`` block:
        ``pids_limit``, ``cap_drop``, ``security_opt``, ``read_only``
        and the image's ``build.args`` are deployment knobs too, and all
        of them read the operator's env file.
        """
        interpolated = _interpolated_knobs(COMPOSE)
        # Variables the Open WebUI service reads for itself, or that the
        # compose file passes under a different name.
        aliases = {"WHISPER_API_KEY": "AUDIO_STT_OPENAI_API_KEY"}
        for knob in sorted(_template_knobs()):
            if knob in NOT_OPERATOR_KNOBS:
                continue
            with self.subTest(knob=knob):
                self.assertTrue(
                    knob in interpolated or aliases.get(knob) in interpolated,
                    f"{knob} is offered in .env.example but the compose file does not "
                    "interpolate it — setting it in .env cannot change anything",
                )

    def test_the_production_stack_has_its_own_token_and_no_open_webui(self) -> None:
        """The production stack is a different stack, and says so in code.

        It has no Open WebUI service, and the shared token has its own
        name there: ``AGENT_STACK_TOKEN``. The container still receives
        it as ``OPENAPI_PROXY_TOKEN`` — that is the name the code reads
        — but the *value* comes from the production stack's own
        variable, not from the development stack's.
        """
        production = _read(COMPOSE_PRODUCTION)
        self.assertNotIn("open-webui", production, "the production stack has an Open WebUI")
        self.assertIn(
            "OPENAPI_PROXY_TOKEN=${AGENT_STACK_TOKEN:?AGENT_STACK_TOKEN must be set}",
            production,
            "the production stack does not take its shared token from AGENT_STACK_TOKEN — "
            "the development stack's variable name is not the production stack's",
        )
        self.assertNotIn(
            "${OPENAPI_PROXY_TOKEN",
            production,
            "the production stack reads the shared token from OPENAPI_PROXY_TOKEN — its own "
            "name for it is AGENT_STACK_TOKEN",
        )

    def test_an_offered_knob_is_interpolated_and_not_hardcoded(self) -> None:
        """A knob the operator may change must not be a literal in compose.

        ``- AGENT_LOG_LEVEL=INFO`` means the value in .env is ignored: the
        container always gets ``INFO``. The template must not offer a knob
        the compose file pins.
        """
        pinned = _pinned_knobs(COMPOSE) | _pinned_knobs(COMPOSE_PRODUCTION)
        for knob in sorted(_template_knobs()):
            if knob in NOT_OPERATOR_KNOBS:
                continue
            with self.subTest(knob=knob):
                self.assertNotIn(
                    knob,
                    pinned,
                    f"{knob} is offered in .env.example but compose pins it to a literal — "
                    "the value in .env is ignored",
                )

    def test_a_knob_the_compose_pins_is_not_offered_in_the_template(self) -> None:
        """The reverse direction: no dead knob in the template.

        If compose pins a value, the template must not list it as a knob
        — either the compose interpolates it, or the template stops
        offering it.
        """
        pinned = _pinned_knobs(COMPOSE) | _pinned_knobs(COMPOSE_PRODUCTION)
        for knob in sorted(pinned):
            if knob in NOT_OPERATOR_KNOBS:
                continue
            with self.subTest(knob=knob):
                self.assertNotIn(
                    knob,
                    _template_knobs(),
                    f"compose pins {knob} to a literal — the template must not offer it as a "
                    "knob the operator can turn",
                )

    def test_a_knob_the_compose_interpolates_is_offered_in_the_template(self) -> None:
        """A knob a deployment may set must be discoverable in the template.

        The compose file interpolates it, so it *is* a knob — and an
        operator who wants to set it has to find it somewhere. The
        template is where a knob is offered; a knob that lives only in
        the compose file is a knob nobody knows about.
        """
        interpolated = _interpolated_knobs(COMPOSE)
        # The knobs the compose file passes for the service's own sake,
        # not for the operator's.
        internal = {
            "AGENT_TOKEN",
            "OPENAPI_PROXY_TOKEN",
            "TOOL_SERVER_CONNECTIONS",
            "SANDBOX_URL",
        }
        offered = _template_knobs()
        for knob in sorted(interpolated - internal):
            if knob in NOT_OPERATOR_KNOBS:
                continue
            with self.subTest(knob=knob):
                self.assertIn(
                    knob,
                    offered,
                    f"the compose file interpolates {knob}, so it is a knob — but the "
                    "template does not offer it, so nobody can find it",
                )


class TestTheRequiredVariablesAreDocumented(unittest.TestCase):
    """A variable the stack refuses to start without must be documented."""

    def _docs_prose(self) -> str:
        """The documents' prose, without their tables and code blocks.

        A table row and a code sample are not prose: an operator who
        needs to know *what* a variable is reads the sentence that
        explains it. The check is on the prose, so that dropping a
        variable from a table row is caught rather than hidden behind a
        mention in an example.
        """
        prose = []
        for path in sorted((REPO_ROOT / "docs").glob("*.md")):
            in_code = False
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("```"):
                    in_code = not in_code
                    continue
                if in_code or line.lstrip().startswith("|"):
                    continue
                prose.append(line)
        return "\n".join(prose)

    def test_every_required_variable_is_named_in_the_prose(self) -> None:
        prose = self._docs_prose()
        for compose in COMPOSE_FILES:
            for knob in sorted(_required_by_compose(compose)):
                with self.subTest(compose=compose.name, knob=knob):
                    self.assertIn(
                        knob,
                        prose,
                        f"{compose.name} refuses to start without {knob}, but no document "
                        "in docs/ explains it in prose — an operator cannot look it up",
                    )

    def test_the_required_variable_of_the_production_stack_is_documented(self) -> None:
        """The production stack's token is the one an operator must set."""
        self.assertIn("AGENT_STACK_TOKEN", _required_by_compose(COMPOSE_PRODUCTION))
        configuration = _read(CONFIGURATION)
        self.assertIn(
            "AGENT_STACK_TOKEN",
            configuration,
            "the production stack's required token is not documented in CONFIGURATION.md",
        )


class TestTheDocumentedTimeoutIsTheShippedTimeout(unittest.TestCase):
    """The table cell must carry the number the code ships."""

    def test_the_configuration_table_agrees_with_the_code(self) -> None:
        shipped = _shipped_exec_timeout()
        configuration = _read(CONFIGURATION)
        rows = [
            line
            for line in configuration.splitlines()
            if "AGENT_EXEC_TIMEOUT" in line and line.startswith("|")
        ]
        self.assertTrue(rows, "CONFIGURATION.md has no table row for AGENT_EXEC_TIMEOUT")
        row = rows[0]
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        self.assertGreaterEqual(
            len(cells),
            3,
            f"the AGENT_EXEC_TIMEOUT row has no default column: {row!r}",
        )
        documented = cells[1].strip("`")
        with self.subTest(documented=documented, shipped=shipped):
            self.assertEqual(
                documented,
                str(int(shipped)),
                "the CONFIGURATION.md table advertises a timeout the proxy does not ship",
            )

    def test_the_compose_default_agrees_with_the_code(self) -> None:
        """The compose default and the code constant must not drift apart."""
        shipped = int(_shipped_exec_timeout())
        value = _compose_environment().get("AGENT_EXEC_TIMEOUT", "")
        match = re.search(r"AGENT_EXEC_TIMEOUT:-\s*(\d+)", value)
        self.assertIsNotNone(
            match, f"AGENT_EXEC_TIMEOUT has no numeric default in compose: {value!r}"
        )
        self.assertEqual(
            int(match.group(1)),
            shipped,
            "compose and the code disagree about the shipped exec timeout",
        )

    def test_the_prose_agrees_with_the_code(self) -> None:
        """The prose under the table must not contradict it."""
        shipped = int(_shipped_exec_timeout())
        configuration = _read(CONFIGURATION)
        self.assertIn(
            f"default is {shipped} seconds",
            configuration,
            "the prose under the AGENT_EXEC_TIMEOUT table does not state the shipped default",
        )


class TestTheHandoffContractNamesEveryStatusTheDoorAnswers(unittest.TestCase):
    """The contract with the bot must name the codes the door can answer."""

    #: The marker that starts the canonical status enumeration. The codes
    #: the door can answer are listed in the prose that follows it, and
    #: that prose is the contract — a mention of "409" in a table further
    #: down the document is an aside, not the enumeration.
    ENUMERATION_MARKER = "**All four endpoints answer the same way.**"

    def _status_codes_the_door_can_raise(self) -> set[int]:
        """The HTTP status codes the file door raises, read from the AST.

        ``HTTPException`` is a *name*, not an attribute — a parser that
        only looks for ``node.func.attr`` finds nothing here and the
        check that uses it becomes vacuous: it passes because it never
        looked at a single code. Both shapes are therefore accepted, and
        the parser proves it found something.
        """
        source = (REPO_ROOT / "agent_api.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        codes: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "HTTPException":
                continue
            for keyword in node.keywords:
                if keyword.arg == "status_code" and isinstance(keyword.value, ast.Constant):
                    codes.add(int(keyword.value.value))
        self.assertTrue(
            codes,
            "the parser found no status code in agent_api.py — it is broken, and every "
            "check that uses it passes vacuously",
        )
        return codes

    def _the_status_contract(self) -> str:
        """The prose that enumerates the codes the door answers."""
        contract = _read(HANDOFF_BINARY)
        self.assertIn(
            self.ENUMERATION_MARKER,
            contract,
            "the handoff contract has no status enumeration — the canonical list of "
            "statuses is the contract",
        )
        tail = contract[contract.index(self.ENUMERATION_MARKER) :]
        # The enumeration ends where the next section starts.
        for heading in ("\n### ", "\n## "):
            if heading in tail:
                tail = tail[: tail.index(heading)]
                break
        return tail

    def test_every_status_the_door_raises_is_in_the_enumeration(self) -> None:
        """The canonical list must name every code the door can answer.

        The check looks at the enumeration and not at the whole document:
        the contract mentions "409" in two other places, and a check over
        the whole file would stay green when the enumeration loses it.
        """
        enumeration = self._the_status_contract()
        door_codes = self._status_codes_the_door_can_raise()
        for code in sorted(door_codes):
            if code in (400, 403, 404, 409, 413, 416, 502):
                with self.subTest(code=code):
                    self.assertIn(
                        str(code),
                        enumeration,
                        f"the file door can answer {code}, but the contract's status "
                        "enumeration does not name it — the bot's transport would fall "
                        "through to a generic error",
                    )

    def test_the_enumeration_names_a_code_once(self) -> None:
        """The list is a list: a code named twice is a fact in two places.

        The mutation check for this contract breaks the enumeration's
        **409** and expects the contract to stop naming it. That only
        works while the enumeration names it *once*: a second mention
        somewhere else in the list would keep the code named, the
        mutation would destroy nothing, and the tripwire would report a
        verdict about a property that never broke.
        """
        enumeration = self._the_status_contract()
        for code in (409,):
            with self.subTest(code=code):
                self.assertEqual(
                    enumeration.count(str(code)),
                    1,
                    f"the enumeration names {code} {enumeration.count(str(code))} times - "
                    "a code named twice is a fact in two places, and the mutation that "
                    "breaks one mention would break nothing",
                )

    def test_the_contract_pins_a_tag_that_exists_in_the_tree(self) -> None:
        """The contract names the sandbox version it was written against.

        A contract pinned to a tag that is seven releases old is a
        contract the reader cannot check — and one whose status-code
        table has already gone stale.
        """
        contract = _read(HANDOFF_BINARY)
        version = re.search(r"\*\*Code against:\*\* sandbox tag \*\*`(v2\.\d+\.\d+)`\*\*", contract)
        self.assertIsNotNone(version, "the handoff contract does not pin a version in its header")
        pinned = version.group(1)
        shipped = _shipped_version()
        self.assertEqual(
            pinned.removeprefix("v"),
            shipped,
            f"the handoff contract is pinned to {pinned}, but this tree ships {shipped} — "
            "the contract describes a version that is not deployed",
        )


def _shipped_version() -> str:
    """The version this tree ships, read from the code."""
    tree = ast.parse((REPO_ROOT / "agent_core.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    return str(node.value.value)
    raise AssertionError("agent_core.py does not define __version__")


class TestTheMetricsTableNamesEveryMetric(unittest.TestCase):
    """A metric the services export but the table does not name is invisible."""

    def test_every_declared_metric_is_in_the_configuration_table(self) -> None:
        configuration = _read(CONFIGURATION)
        declared = _declared_metrics()
        for _prefix, names in sorted(declared.items()):
            for name in sorted(names):
                with self.subTest(metric=name):
                    self.assertRegex(
                        configuration,
                        rf"`{re.escape(name)}`",
                        f"{name} is exported by the service but the metrics table does not "
                        "name it — an operator watching a dashboard has no idea it exists",
                    )

    def test_the_metrics_table_names_no_metric_the_code_does_not_export(self) -> None:
        """The other direction: a table row for a metric that does not exist."""
        configuration = _read(CONFIGURATION)
        declared = set().union(*_declared_metrics().values())
        listed = set(re.findall(r"`(sandbox_(?:agent|proxy)_[a-z_]+)`", configuration))
        for name in sorted(listed):
            with self.subTest(metric=name):
                self.assertIn(
                    name,
                    declared,
                    f"the metrics table lists {name}, but no service exports it — an alert on "
                    "it would never fire",
                )


class TestTheDocsDoNotContradictTheComposeFile(unittest.TestCase):
    """A sentence about what the compose file contains must be true."""

    def test_the_docs_do_not_say_the_compose_file_lacks_the_bind_host(self) -> None:
        """``SANDBOX_BIND_HOST`` is interpolated by the compose file.

        The docs used to say the address "is not written into the compose
        file" — false since the variable was introduced, and the same
        sentence contradicted itself two paragraphs later.
        """
        for path in (README, CONFIGURATION):
            text = _read(path)
            with self.subTest(document=path.name):
                self.assertNotIn(
                    "not written into the compose file",
                    text,
                    f"{path.name} claims the compose file does not contain the bind address, "
                    "but the compose file interpolates SANDBOX_BIND_HOST",
                )

    def test_the_bind_host_is_really_interpolated(self) -> None:
        """The claim above only holds while the compose file says so."""
        self.assertIn(
            "SANDBOX_BIND_HOST",
            _read(COMPOSE),
            "the compose file no longer interpolates SANDBOX_BIND_HOST — the docs are right "
            "and the code is wrong",
        )


if __name__ == "__main__":  # pragma: no cover - the runner imports this
    unittest.main()
