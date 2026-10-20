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
from pathlib import Path

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
        """``ubuntu:24.04`` already owns uid/gid 1000 as ``ubuntu``.

        ``groupadd -g 1000 agent`` therefore exits 4 and takes the whole
        build with it. The fix references ids numerically.
        """
        body = _strip_comments(SANDBOX_DOCKERFILE.read_text(encoding="utf-8"))
        self.assertNotIn(
            "groupadd",
            body,
            "Dockerfile.sandbox calls groupadd. ubuntu:24.04 already ships "
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


class TestProxyDockerfile(unittest.TestCase):
    """BUG-1b + BUG-4: ``proxy`` group name taken, and the authed HEALTHCHECK."""

    def test_no_named_user_creation(self) -> None:
        """``python:3.11-slim`` ships a ``proxy`` group at GID 13.

        uid/gid 1000 are free; only the *name* collides. Reference ids
        numerically.
        """
        body = _strip_comments(PROXY_DOCKERFILE.read_text(encoding="utf-8"))
        self.assertNotIn(
            "groupadd",
            body,
            "Dockerfile.openapi-proxy calls groupadd. python:3.11-slim ships "
            "a `proxy` group at GID 13, so `groupadd -g 1000 proxy` exits 4. "
            "Use numeric ids instead.",
        )
        self.assertNotIn("useradd", body)
        self.assertNotIn("--chown=proxy:proxy", body)

    def test_uses_numeric_user(self) -> None:
        body = _strip_comments(PROXY_DOCKERFILE.read_text(encoding="utf-8"))
        self.assertRegex(body, r"(?m)^USER\s+1000:1000\s*$")

    def test_pip_prefix_not_unwrapped(self) -> None:
        """python:3.11-slim pip is platform-scheme: no ``local`` component.

        This is the mirror image of the sandbox test. The two Dockerfiles
        genuinely differ here; copying the sandbox fix across "for
        consistency" breaks the proxy build.
        """
        body = _strip_comments(PROXY_DOCKERFILE.read_text(encoding="utf-8"))
        self.assertRegex(
            body,
            r"(?m)^COPY --from=builder /install /usr/local\s*$",
            "Dockerfile.openapi-proxy must keep the bare /install copy: "
            "python:3.11-slim pip uses the platform scheme and does NOT "
            "create /install/local/.",
        )

    def test_healthcheck_uses_unauthenticated_endpoint(self) -> None:
        """Every proxy endpoint needs a bearer token since v2.4.3.

        Probing ``/healthz`` here marks the container permanently
        unhealthy, and ``depends_on: service_healthy`` then stops every
        dependent service from starting.
        """
        body = _strip_comments(PROXY_DOCKERFILE.read_text(encoding="utf-8"))
        match = re.search(r"(?ms)^HEALTHCHECK.*?^\s+CMD\s+curl\s+-fsS\s+(\S+)", body)
        self.assertIsNotNone(match, "Dockerfile.openapi-proxy has no HEALTHCHECK curl probe")
        probe_url = match.group(1)
        self.assertNotIn(
            "/healthz",
            probe_url,
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

    def test_no_volume_mounted_at_home_agent(self) -> None:
        """A volume at /home/agent freezes the code at whatever the first deploy had.

        The application code lives in /home/agent (Dockerfile.sandbox sets
        WORKDIR there so the import path resolves). Docker seeds a fresh named
        volume from the image on first use and never re-syncs it, so mounting
        one at that path makes every later image rebuild a no-op and the agent
        keeps running its first-deploy code forever.
        """
        block = self._agent_block(self._compose_text())
        offenders = [
            line.strip()
            for line in block.splitlines()
            if line.strip().startswith("-")
            and line.rstrip().endswith("/home/agent")
            and "rw" in line
        ]
        self.assertEqual(
            offenders,
            [],
            "docker-compose.production.yml must not mount a writable volume at "
            "/home/agent: it shadows agent_api.py/agent_core.py with the copy "
            "seeded on the first deploy, so later image rebuilds are silently "
            f"ignored. Offending lines: {offenders}",
        )

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
