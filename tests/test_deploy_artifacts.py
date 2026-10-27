"""Tripwires for the v2.5.0 fresh-deploy bugs (found 2026-10-20 on llmhost1).

Context. v2.4.5 shipped green CI (131 tests) and did not build at all:
`docker build` failed with ``groupadd: GID '1000' already exists``, and on
llmhost2 — where a hand-rolled single-stage Dockerfile had been running
since September — none of it was visible. The CI job never ran
``docker build``; it tested Python source against the *host's* pip
environment. These tests are the cheap half of the fix: they parse the
artifacts we ship and fail loudly when they regress to something that
cannot build.

The expensive half is the ``deploy`` CI job added in v2.5.0: it builds both
real images, boots the stack and executes a real command through the proxy.
It cannot run in-process because Docker is not available on the unittest
runner. Neither half substitutes for the other.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
SANDBOX_DOCKERFILE = ROOT / "Dockerfile.sandbox"
PROXY_DOCKERFILE = ROOT / "Dockerfile.openapi-proxy"
COMPOSE = ROOT / "docker-compose.yml"
REQUIREMENTS = ROOT / "requirements.txt"
PROXY_SOURCE = ROOT / "openapi_proxy.py"


def _strip_comments(text: str) -> str:
    """Drop whole-line and trailing ``#`` comments.

    The Dockerfiles carry long explanatory comments that *quote* the old
    broken commands (``groupadd -g 1000 agent``). A naive substring scan
    would match those and fail forever, so comments are removed before any
    pattern assertion below.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line.split(" #", 1)[0] if " #" in line else line)
    return "\n".join(out)


class TestSandboxDockerfile(unittest.TestCase):
    """BUG-1a + BUG-2: uid/gid 1000 collision and the ``local/`` pip prefix."""

    def test_no_named_user_creation(self) -> None:
        """``ubuntu:26.04`` already owns uid/gid 1000 as ``ubuntu``.

        ``groupadd -g 1000 agent`` therefore exits 4 and takes the whole
        build with it. The fix references ids numerically.
        """
        body = _strip_comments(SANDBOX_DOCKERFILE.read_text(encoding="utf-8"))
        self.assertNotIn(
            "groupadd",
            body,
            "Dockerfile.sandbox calls groupadd. ubuntu:26.04 already ships "
            "gid 1000 as `ubuntu`, so this exits 4. Use numeric ids instead.",
        )
        self.assertNotIn("useradd", body, "Dockerfile.sandbox calls useradd; use `USER 1000:1000`.")
        self.assertNotIn(
            "--chown=agent:agent",
            body,
            "COPY --chown=agent:agent requires a passwd entry named `agent`, "
            "which no longer exists. Use --chown=1000:1000.",
        )

    def test_uses_numeric_user(self) -> None:
        body = _strip_comments(SANDBOX_DOCKERFILE.read_text(encoding="utf-8"))
        self.assertRegex(body, r"(?m)^USER\s+1000:1000\s*$")

    def test_pip_prefix_unwrapped(self) -> None:
        """Ubuntu pip is distutils-scheme: ``--prefix=/install`` -> ``/install/local``.

        A wholesale copy lands the packages at ``/usr/local/local/...``,
        which the interpreter does not search, and the container dies with
        ``No module named uvicorn``.
        """
        body = _strip_comments(SANDBOX_DOCKERFILE.read_text(encoding="utf-8"))
        self.assertRegex(
            body,
            r"(?m)^COPY --from=builder /install/local/ /usr/local/\s*$",
            "Dockerfile.sandbox must copy /install/local/ (Debian distutils "
            "scheme). A bare `COPY /install /usr/local` yields "
            "/usr/local/local/... and the runtime cannot import uvicorn.",
        )


#: The modules of the agent itself. Every one of them is read by the
#: scan that looks for local imports, because a module the scan does
#: not read is a module whose imports nobody can complain about — and an
#: import that is missing from the image does not fail the build, it
#: fails the container, on the first request.
AGENT_MODULES = ("agent_api.py", "agent_core.py", "agent_config.py", "mcp_server.py")

#: The modules both services share. The agent's own modules import
#: them, so their imports are covered by the scan of those.
SHARED_MODULES = ("body_limit.py", "json_logging.py", "metrics.py")


class TestAgentModulesAreInTheImage(unittest.TestCase):
    """Every module the agent imports has to be IN the image.

    A module that is missing from the image does not fail the build. It
    fails the *container*, on the first request, with
    ``ModuleNotFoundError`` — and the CI job that builds the image would
    still go green if it only checked that the build succeeded.

    That is not hypothetical: ``agent_config.py`` (the validated stdout
    and binary budgets) was added in v2.6.0 and the Dockerfile copied
    only ``agent_core.py`` and ``agent_api.py``.
    """

    def _imported_modules(self, sources: tuple[str, ...]) -> set[str]:
        """The local modules that these sources import.

        The imports are matched at any indentation, not only at the
        top of the file. A module that is imported *inside* a function
        is just as much a part of the image: ``mcp_server.py`` reaches
        for ``agent_api`` that way, on purpose — the two modules must
        not import each other at the same time — and an import the scan
        cannot see is an import the scan cannot complain about.
        """
        found: set[str] = set()
        for source in sources:
            text = (ROOT / source).read_text(encoding="utf-8")
            for match in re.finditer(r"^\s*from (\w+) import|^\s*import (\w+)\s*$", text, re.M):
                module = match.group(1) or match.group(2)
                if (ROOT / f"{module}.py").exists():
                    found.add(f"{module}.py")
        return found

    def test_every_local_module_the_agent_imports_is_copied(self) -> None:
        body = SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
        copied = set(re.findall(r"COPY[^\n]*?\b(\w+\.py)\b", body))
        imported = self._imported_modules(AGENT_MODULES)
        self.assertTrue(imported, "no local module was found to check")
        missing = sorted(imported - copied)
        self.assertEqual(
            missing,
            [],
            f"these modules are imported by the agent but not copied into the image: {missing}",
        )

    def test_the_scan_looks_at_every_module_the_agent_ships(self) -> None:
        """The scan and the image must agree on what the agent is.

        A module that the image ships and the scan does not read is a
        module nobody checks: the next local module it imports goes out
        of the image without a word, and the container dies on the first
        request instead of failing the build.
        """
        shipped = set(
            re.findall(r"COPY[^\n]*?\b(\w+\.py)\b", SANDBOX_DOCKERFILE.read_text(encoding="utf-8"))
        )
        self.assertEqual(
            set(AGENT_MODULES),
            shipped - set(SHARED_MODULES),
            "the agent ships a module the scan does not read",
        )

    def test_every_local_module_the_proxy_imports_is_copied(self) -> None:
        """The same defect, one service over."""
        body = PROXY_DOCKERFILE.read_text(encoding="utf-8")
        copied = set(re.findall(r"COPY[^\n]*?\b(\w+\.py)\b", body))
        imported = self._imported_modules(("openapi_proxy.py", "proxy_core.py"))
        self.assertTrue(imported, "no local module was found to check")
        missing = sorted(imported - copied)
        self.assertEqual(
            missing,
            [],
            f"these modules are imported by the proxy but not copied into the image: {missing}",
        )

    def test_the_agent_modules_are_copied_together(self) -> None:
        """They have to be in the same directory to import each other."""
        body = SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
        for match in re.finditer(r"^COPY[^\n]*\b(\w+\.py)\b[^\n]*$", body, re.M):
            line = match.group(0)
            destination = line.rsplit(None, 1)[-1]
            self.assertIn(
                "/home/agent/",
                destination,
                f"the agent's modules must live in /home/agent: {line!r}",
            )


class TestProxyDockerfile(unittest.TestCase):
    """BUG-1b + BUG-4: ``proxy`` group name taken, and the authed HEALTHCHECK."""

    def test_no_named_user_creation(self) -> None:
        """``python:3.14-slim`` ships a ``proxy`` group at GID 13.

        uid/gid 1000 are free; only the *name* collides. Reference ids
        numerically.
        """
        body = _strip_comments(PROXY_DOCKERFILE.read_text(encoding="utf-8"))
        self.assertNotIn(
            "groupadd",
            body,
            "Dockerfile.openapi-proxy calls groupadd. python:3.14-slim ships "
            "a `proxy` group at GID 13, so `groupadd -g 1000 proxy` exits 4. "
            "Use numeric ids instead.",
        )
        self.assertNotIn("useradd", body)
        self.assertNotIn("--chown=proxy:proxy", body)

    def test_uses_numeric_user(self) -> None:
        body = _strip_comments(PROXY_DOCKERFILE.read_text(encoding="utf-8"))
        self.assertRegex(body, r"(?m)^USER\s+1000:1000\s*$")

    def test_pip_prefix_not_unwrapped(self) -> None:
        """python:3.14-slim pip is platform-scheme: no ``local`` component.

        This is the mirror image of the sandbox test. The two Dockerfiles
        genuinely differ here; copying the sandbox fix across "for
        consistency" breaks the proxy build.
        """
        body = _strip_comments(PROXY_DOCKERFILE.read_text(encoding="utf-8"))
        self.assertRegex(
            body,
            r"(?m)^COPY --from=builder /install /usr/local\s*$",
            "Dockerfile.openapi-proxy must keep the bare /install copy: "
            "python:3.14-slim pip uses the platform scheme and does NOT "
            "create /install/local/.",
        )

    def test_healthcheck_uses_unauthenticated_endpoint(self) -> None:
        """Every proxy endpoint needs a bearer token since v2.4.3.

        Probing ``/healthz`` here marks the container permanently
        unhealthy, and ``depends_on: service_healthy`` then stops every
        dependent service from starting.

        The probe is Python since v2.11.0 (the image installs no curl),
        so the check looks for the URL rather than for a particular
        probe binary.
        """
        body = _strip_comments(PROXY_DOCKERFILE.read_text(encoding="utf-8"))
        self.assertRegex(
            body,
            r"(?ms)^HEALTHCHECK.*?openapi\.json",
            "Dockerfile.openapi-proxy has no HEALTHCHECK probe of /openapi.json",
        )
        instruction = body.split("HEALTHCHECK", 1)[1]
        self.assertNotIn(
            "/healthz",
            instruction,
            "The Dockerfile HEALTHCHECK probes /healthz, which requires a "
            "bearer token. Use the unauthenticated /openapi.json, matching "
            "docker-compose.yml.",
        )


class TestComposeHealthchecks(unittest.TestCase):
    """The compose file overrides the Dockerfiles — keep the two consistent."""

    def test_proxy_healthcheck_is_unauthenticated(self) -> None:
        text = COMPOSE.read_text(encoding="utf-8")
        proxy_block = text.split("openapi-proxy:", 1)[1]
        self.assertIn("/openapi.json", proxy_block, "compose proxy healthcheck lost its endpoint")

    def test_agent_healthcheck_is_public(self) -> None:
        """The agent's /healthz is intentionally unauthenticated.

        Unlike the proxy's, it returns only ``{"status": "ok"}`` — no path,
        no counters — so probing it without a token leaks nothing.
        """
        text = COMPOSE.read_text(encoding="utf-8")
        agent_block = text.split("sandbox-agent:", 1)[1].split("openapi-proxy:", 1)[0]
        self.assertIn("/healthz", agent_block)


class TestProxyAppKwargs(unittest.TestCase):
    """BUG-3: ``openapi_version=`` was never a FastAPI parameter."""

    def test_no_openapi_version_kwarg(self) -> None:
        source = PROXY_SOURCE.read_text(encoding="utf-8")
        code = re.sub(r"#.*", "", source)
        self.assertNotIn(
            "openapi_version",
            code,
            "openapi_proxy.py passes openapi_version= to FastAPI(). That kwarg "
            "was absorbed by **kwargs on FastAPI <0.149 and became a TypeError "
            "on >=0.149, so a fresh install cannot boot the proxy.",
        )

    def test_served_openapi_version_is_not_asserted_wrong(self) -> None:
        """FastAPI serves 3.1.x; the old kwarg claimed 3.0.0 and was a lie."""
        try:
            from fastapi import FastAPI  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi not installed")
        app = FastAPI(title="t")
        served = app.openapi().get("openapi", "")
        self.assertTrue(
            served.startswith("3."),
            f"unexpected OpenAPI spec version: {served!r}",
        )


class TestRequirementsNotOverPinned(unittest.TestCase):
    """A narrow pin hides application bugs and ships stale dependencies."""

    def test_fastapi_range_is_not_narrow(self) -> None:
        """A pin like ``>=0.143,<0.144`` freezes a single patch series.

        The v2.5.0 stop-cran did exactly that to work around an
        application bug. The fix is to repair the application, not to
        freeze the dependency set for every future clean install.
        """
        text = REQUIREMENTS.read_text(encoding="utf-8")
        match = re.search(r"(?m)^fastapi>=([\d.]+),<([\d.]+)\s*$", text)
        self.assertIsNotNone(match, "no fastapi range in requirements.txt")
        low, high = match.group(1), match.group(2)
        high_parts = high.split(".")
        self.assertEqual(
            len(high_parts),
            2,
            f"fastapi upper bound {high!r} must be a <MAJOR.MINOR ceiling "
            "(e.g. <1.0), not a patch pin. A patch ceiling makes a fresh "
            "install ship an old, unaudited dependency set.",
        )
        self.assertEqual(
            high,
            "1.0",
            f"fastapi pinned below {high} — expected the original <1.0 "
            "ceiling; narrower means someone re-introduced a pin",
        )
        self.assertEqual(
            low,
            "0.110",
            f"fastapi floor moved to {low!r} — the only sanctioned floor "
            "change is one made deliberately in a release, not as a "
            "side effect of a build failure",
        )

    def test_no_stop_cran_comment(self) -> None:
        """The old pin carried a 'raise this only with the kwarg' comment."""
        text = REQUIREMENTS.read_text(encoding="utf-8").lower()
        self.assertNotIn(
            "raise this ceiling only together",
            text,
            "stale stop-cran comment left in requirements.txt",
        )


if __name__ == "__main__":
    unittest.main()

# ---------------------------------------------------------------------------
# Production compose (llmhost1 telegram-bot side-car)
# ---------------------------------------------------------------------------
# Imported at module level would need PyYAML, which the CI runner has but a
# bare `python3 -m unittest` may not. Parsed textually instead, with the
# check written so it fails on the shape rather than on a yaml library.


class TestNoStackShadowsTheAgentCode(unittest.TestCase):
    """No stack may mount a volume over the agent's application code.

    The code lives in the directory the Dockerfile copies it into
    (``WORKDIR /home/agent``, so the import path resolves). Docker seeds
    a named volume from the image on first use and never re-syncs it, so
    a volume mounted at that path shadows the code with the copy from the
    first run — and every later image rebuild becomes a no-op. The
    repository moves, the container does not.

    This is not hypothetical. The production stack shipped with
    ``tb-sandbox-home:/home/agent:rw``, and every deploy after the first
    one was a no-op. The same volume was still sitting in the dev and CI
    stacks, where a fresh volume per run hides the problem: the code on
    disk and the code in the container coincide, and the defect only
    shows up on the *second* deploy of the same stack.

    A sub-path is fine — ``/home/agent/.ssh`` and
    ``/home/agent/.cache/pip`` do not cover the code.
    """

    COMPOSE_FILES = (
        "docker-compose.yml",
        "docker-compose.ci.yml",
        "docker-compose.production.yml",
    )

    @staticmethod
    def _code_dir() -> str:
        """The directory the agent's application code is copied into.

        Derived from the Dockerfile rather than hard-coded, so moving the
        code moves the rule with it.
        """
        parents = set()
        for raw in SANDBOX_DOCKERFILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line.startswith("COPY "):
                continue
            dest = line.split()[-1]
            if dest.endswith(".py"):
                parents.add(str(PurePosixPath(dest).parent))
        if len(parents) != 1:
            raise AssertionError(f"cannot tell where the agent's code lives: {sorted(parents)}")
        return parents.pop()

    @staticmethod
    def _volume_mounts(text: str) -> list[tuple[str, str, str, str]]:
        """(service, source, target, mode) for every volume mount in a file.

        The spec is parsed rather than string-matched. An earlier version
        of this rule looked for a line *ending* in ``/home/agent`` and so
        matched nothing at all: the offending line ends in ``:rw``.
        """
        mounts: list[tuple[str, str, str, str]] = []
        service = None
        in_volumes = False
        for raw in text.splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip())
            if indent == 0:
                service = None
                in_volumes = False
                continue
            if indent == 2 and stripped.endswith(":"):
                service = stripped[:-1]
                in_volumes = False
                continue
            if indent >= 4 and stripped.endswith(":") and not stripped.startswith("-"):
                in_volumes = stripped == "volumes:"
                continue
            if in_volumes and stripped.startswith("-"):
                parts = stripped[1:].strip().split(":")
                if len(parts) >= 2:
                    mode = parts[2] if len(parts) > 2 else "rw"
                    mounts.append((service or "?", parts[0], parts[1], mode))
        return mounts

    def test_no_volume_at_the_code_directory(self) -> None:
        code_dir = self._code_dir().rstrip("/")
        offenders = []
        for name in self.COMPOSE_FILES:
            path = ROOT / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for service, source, target, mode in self._volume_mounts(text):
                if target.rstrip("/") != code_dir:
                    continue
                offenders.append(f"{name}: {service}: {source}:{target}:{mode}")
        self.assertEqual(
            offenders,
            [],
            f"a volume mounted at {code_dir} shadows the agent's application "
            "code with the copy seeded on the first run, so every later "
            "image rebuild is silently ignored — the repository moves, the "
            "container does not. Mount a sub-path instead (e.g. "
            f"{code_dir}/.cache/pip). Offending mounts: {offenders}",
        )

    def test_the_rule_catches_the_line_it_was_written_for(self) -> None:
        """A rule that cannot fail is worse than no rule.

        This is the exact line the production stack shipped with, and the
        exact line the CI stack still had. If the parser stops recognising
        it, the tripwire above is decoration.
        """
        mounts = self._volume_mounts(
            "services:\n"
            "  sandbox-agent:\n"
            "    volumes:\n"
            "      - tb-sandbox-home:/home/agent:rw\n"
            "      - /opt/agent-workspace:/workspace\n"
        )
        self.assertIn(
            ("sandbox-agent", "tb-sandbox-home", "/home/agent", "rw"),
            mounts,
        )


class TestProductionCompose(unittest.TestCase):
    """docker-compose.production.yml must not shadow the application code."""

    @staticmethod
    def _compose_text() -> str:
        return (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")

    @staticmethod
    def _agent_block(text: str) -> str:
        start = text.index("  sandbox-agent:")
        end = text.index("  openapi-proxy:")
        return text[start:end]

    def test_deploy_command_documents_the_build_flag(self) -> None:
        """`up -d` alone reuses the existing image; operators must be told."""
        text = self._compose_text()
        self.assertIn(
            "--build",
            text,
            "The header must document `up -d --build`. Neither service pins an "
            "image, so `up -d` reuses the previous build and runs stale code.",
        )

    def test_proxy_is_loopback_only(self) -> None:
        """The bot runs in host network mode; the proxy must not face the LAN."""
        text = self._compose_text()
        self.assertIn('"127.0.0.1:8090:8090"', text)

    def test_agent_publishes_no_host_port(self) -> None:
        block = self._agent_block(self._compose_text())
        self.assertNotIn("ports:", block, "sandbox-agent must stay on the internal bridge")


class TestMakefileAlwaysBuilds(unittest.TestCase):
    """`make up` and `make restart` must pass --build.

    Neither compose service pins an image, so `up -d` reuses whatever tag is
    already present and silently serves the previous code. Documenting that
    in the compose header is not enough - the Makefile is what operators
    actually type, so the flag has to be here too. `restart` is the sharpest
    case: `--force-recreate` rebuilds the containers from the *existing*
    image, so it looks like a fresh restart while serving stale code.
    """

    @staticmethod
    def _makefile() -> str:
        return (ROOT / "Makefile").read_text(encoding="utf-8")

    def _recipe(self, target: str) -> str:
        text = self._makefile()
        start = text.index(f"\n{target}:")
        rest = text[start + 1 :]
        lines = [ln for ln in rest.splitlines() if ln.startswith("\t")]
        return "\n".join(lines)

    def test_up_builds(self) -> None:
        recipe = self._recipe("up")
        self.assertIn("$(COMPOSE) up -d --build", recipe)

    def test_restart_builds(self) -> None:
        recipe = self._recipe("restart")
        self.assertIn(
            "--build", recipe, "make restart must rebuild, not recreate from the old image"
        )
        self.assertIn("--force-recreate", recipe)


class TestTheSystemdUnitAlwaysBuilds(unittest.TestCase):
    """The unit that starts the stack at boot must build it.

    The same story as the Makefile, with one extra edge: the unit runs
    when the machine boots, nobody is watching, and neither compose
    service pins an image. `up -d` reuses whatever tag is present, so
    the unit would serve yesterday's build after every reboot — and the
    symptom shows up as "my change did not take effect", hours later.

    The unit is shipped as a template (`*.service.example`), because it
    names one machine. The glob keeps the tripwire attached to the file
    whatever it is called.
    """

    @staticmethod
    def _unit() -> tuple[Path, str]:
        units = sorted(ROOT.glob("agent-stack.service*"))
        assert units, "no systemd unit in the repository root"
        unit = units[0]
        return unit, unit.read_text(encoding="utf-8")

    def test_the_unit_is_a_template(self) -> None:
        """It names one machine, so it must not pretend to be the unit."""
        unit, text = self._unit()
        self.assertTrue(
            unit.name.endswith(".example"),
            f"{unit.name} is a template — it names one machine's user, group and directory",
        )
        self.assertIn("WorkingDirectory=", text)

    def test_the_unit_builds_on_start(self) -> None:
        _, text = self._unit()
        start = text.split("ExecStart=", 1)[1].splitlines()[0]
        self.assertIn(
            "--build",
            start,
            "the unit starts the stack at boot — without --build it serves the "
            "previous build, and nobody is there to notice",
        )

    def test_the_unit_builds_on_reload(self) -> None:
        _, text = self._unit()
        reload_ = text.split("ExecReload=", 1)[1].splitlines()[0]
        self.assertIn("--build", reload_, "a reload that does not rebuild reloads the old code")
        self.assertIn("--force-recreate", reload_)
