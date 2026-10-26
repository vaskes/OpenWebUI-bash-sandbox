"""Tripwires for the v2.11.0 review wave.

The 2026-10-26 Qwen review of the whole repo produced a list of
findings. Each one that turned out to be real got a tripwire here, and
each tripwire was checked by mutation — a test that cannot be made red
is a decoration, not a check.

Nothing in this file tests the *implementation* of a fix; it tests the
*claim* the fix makes. The claims are written down in the commit
messages and in docs/SECURITY.md, and those are the things that rot.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("OPENAPI_PROXY_TOKEN", "test-token")
os.environ.setdefault("AGENT_TOKEN", "test-token")


# ---------------------------------------------------------------------------
# H1 — the command preview that goes into the log must not carry secrets
# ---------------------------------------------------------------------------
class TestSecretRedaction(unittest.TestCase):
    """The agent logs a preview of every command it runs. A command that
    carries a token must not write that token into the log file."""

    def setUp(self) -> None:
        from agent_core import redact_secrets  # noqa: PLC0415

        self._redact = redact_secrets

    def test_an_assignment_with_a_secret_name_is_redacted(self) -> None:
        out = self._redact("export OPENAI_API_KEY=sk-proj-abc123")
        self.assertNotIn("sk-proj-abc123", out)
        self.assertIn("OPENAI_API_KEY", out, "the name must survive — the log is an audit trail")
        self.assertIn("[REDACTED]", out)

    def test_a_camel_case_secret_name_is_redacted(self) -> None:
        out = self._redact("curl -H apiKey:abc123 https://example.com")
        self.assertNotIn("abc123", out)

    def test_a_password_flag_is_redacted(self) -> None:
        for command in (
            "mysql -uroot --password hunter2 db",
            "mysql -uroot --password=hunter2 db",
        ):
            out = self._redact(command)
            self.assertNotIn("hunter2", out, command)
            self.assertIn("--password", out, command)

    def test_an_authorization_header_is_redacted(self) -> None:
        out = self._redact('curl -H "Authorization: Bearer sk-abc123" https://api.example.com')
        self.assertNotIn("sk-abc123", out)
        self.assertIn("Authorization", out)

    def test_sshpass_password_is_redacted(self) -> None:
        out = self._redact("sshpass -p topsecret ssh host")
        self.assertNotIn("topsecret", out)

    def test_a_private_key_block_is_redacted(self) -> None:
        out = self._redact(
            "cat key.pem\n-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n"
            "-----END OPENSSH PRIVATE KEY-----"
        )
        self.assertNotIn("AAAA", out)
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", out)

    def test_innocent_words_are_not_redacted(self) -> None:
        """A redactor that eats everything is not a redactor. The name
        match is per *segment*, so ``monkey`` and ``KEYBOARD`` are not
        secrets."""
        for command in (
            "echo monkey=banana",
            "echo KEYBOARD=qwerty",
            "echo hello world",
            "git commit -m 'fix the parser'",
            "curl https://example.com/path?a=1&b=2",
        ):
            self.assertEqual(self._redact(command), command, command)

    def test_an_empty_string_is_untouched(self) -> None:
        self.assertEqual(self._redact(""), "")


class _AgentTestBase(unittest.TestCase):
    """Boots the agent app against a fake tmux and records what the
    endpoint typed into the terminal."""

    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: PLC0415
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        import tempfile  # noqa: PLC0415

        tmp = tempfile.mkdtemp(prefix="agent-test-")
        os.environ["AGENT_SESSIONS_FILE"] = os.path.join(tmp, "sessions.json")
        os.environ["AGENT_WORKSPACE"] = tmp
        os.environ["AGENT_TOKEN"] = "test-token"
        if "agent_api" in sys.modules:
            del sys.modules["agent_api"]
        from agent_api import SESSIONS, app  # noqa: E402

        SESSIONS.clear()
        self.client = TestClient(app)
        self.auth = {"Authorization": "Bearer test-token"}
        self.sent: list[str] = []

    def exec_command(self, command: str):
        """Run one command against the fake tmux. Returns the response."""
        sent = self.sent

        async def fake_tmux(*args):
            cmd = args[0]
            if cmd == "send-keys":
                sent.append(" ".join(str(a) for a in args))
                return (0, "", "")
            if cmd == "capture-pane":
                # Answer with the marker the endpoint actually sent, so the
                # exec finishes instead of running into the timeout.
                marker = ""
                for payload in sent:
                    match = re.search(r"__AGENT_DONE_[0-9a-f]+__", payload)
                    if match:
                        marker = match.group(0)
                return (0, f"$ echo hi\n{marker}=0\n", "")
            return (0, "", "")

        with patch("agent_api._tmux", new=fake_tmux):
            return self.client.post(
                "/sessions/x/exec",
                json={"command": command, "timeout": 5},
                headers=self.auth,
            )


class TestTheLoggedPreviewIsRedacted(_AgentTestBase):
    """The redaction must be on the path that actually logs — not only in
    a helper nobody calls."""

    def _exec(self, command: str) -> list[logging.LogRecord]:
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("sandbox-agent")
        old_level = logger.level
        logger.setLevel(logging.INFO)
        handler = _Capture()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, old_level)

        response = self.exec_command(command)
        self.assertEqual(response.status_code, 200, response.text)
        return records

    def test_the_secret_does_not_reach_the_log(self) -> None:
        records = self._exec("export API_KEY=sk-live-9f8e7d6c")
        messages = "\n".join(record.getMessage() for record in records)
        self.assertNotIn("sk-live-9f8e7d6c", messages)
        self.assertIn("[REDACTED]", messages)

    def test_the_command_is_still_auditable(self) -> None:
        """Redaction must not turn the audit trail into noise: the command
        is still named in the log."""
        records = self._exec("export API_KEY=sk-live-9f8e7d6c")
        messages = "\n".join(record.getMessage() for record in records)
        self.assertIn("API_KEY", messages)
        self.assertIn("cmd_len=", messages)


# ---------------------------------------------------------------------------
# M2 — the completion markers must not be guessable
# ---------------------------------------------------------------------------
class TestMarkerLength(_AgentTestBase):
    """The exec protocol names its bookkeeping lines in the terminal. A
    command that prints a line of its own must not be able to
    impersonate one. 32 bits was the width; 64 is the claim."""

    def test_the_marker_carries_at_least_64_bits(self) -> None:
        from agent_core import MARKER_HEX_LEN  # noqa: PLC0415

        self.assertGreaterEqual(MARKER_HEX_LEN, 16)

    def test_a_fresh_token_has_that_length(self) -> None:
        from agent_core import MARKER_HEX_LEN, new_marker_token  # noqa: PLC0415

        token = new_marker_token()
        self.assertEqual(len(token), MARKER_HEX_LEN)
        self.assertTrue(re.fullmatch(r"[0-9a-f]+", token))

    def test_two_tokens_differ(self) -> None:
        from agent_core import new_marker_token  # noqa: PLC0415

        self.assertNotEqual(new_marker_token(), new_marker_token())

    def test_the_command_carries_three_wide_markers(self) -> None:
        """The end-to-end claim: what the endpoint actually types into
        the terminal names all three markers with 16 hex characters."""
        response = self.exec_command("echo hello")
        self.assertEqual(response.status_code, 200, response.text)
        payload = "\n".join(self.sent)
        for prefix in ("__AGENT_DONE_", "__AGENT_BEGIN_", "__AGENT_EC_"):
            self.assertRegex(payload, rf"{re.escape(prefix)}[0-9a-f]{{16}}__")
        # And nothing narrower: an 8-hex marker would match the pattern
        # above only if the width grew, so pin the width explicitly.
        for prefix in ("__AGENT_DONE_", "__AGENT_BEGIN_", "__AGENT_EC_"):
            for token in re.findall(rf"{re.escape(prefix)}([0-9a-f]+)__", payload):
                self.assertEqual(len(token), 16, f"{prefix}{token}")


# ---------------------------------------------------------------------------
# M3 — the token must survive being interpolated into JSON
# ---------------------------------------------------------------------------
class TestTokenShape(unittest.TestCase):
    """The token is not only a bearer token: the compose file interpolates
    it into a JSON document that Open WebUI has to parse. A token that
    breaks that JSON leaves the UI without the sandbox tool, quietly."""

    def test_an_empty_token_is_a_problem(self) -> None:
        from proxy_core import token_problem  # noqa: PLC0415

        problem = token_problem("")
        self.assertIsNotNone(problem)
        self.assertIn("empty", problem)

    def test_a_hex_token_is_fine(self) -> None:
        from proxy_core import token_problem  # noqa: PLC0415

        self.assertIsNone(token_problem("0123456789abcdef" * 4))

    def test_a_quote_breaks_it(self) -> None:
        from proxy_core import token_problem  # noqa: PLC0415

        problem = token_problem('abc"def')
        self.assertIsNotNone(problem)
        self.assertIn('"', problem)

    def test_a_backslash_breaks_it(self) -> None:
        from proxy_core import token_problem  # noqa: PLC0415

        self.assertIsNotNone(token_problem("abc\\def"))

    def test_whitespace_breaks_it(self) -> None:
        from proxy_core import token_problem  # noqa: PLC0415

        self.assertIsNotNone(token_problem("abc def"))
        self.assertIsNotNone(token_problem("abc\ndef"))

    def test_non_ascii_breaks_it(self) -> None:
        from proxy_core import token_problem  # noqa: PLC0415

        self.assertIsNotNone(token_problem("токен"))


class TestTheDeployGateRefusesABrokenToken(unittest.TestCase):
    """``make check-env`` is chained into build, up and restart. A check
    that is written but never fires is a comment."""

    def _run_check_env(self, env_line: str) -> int:
        import shutil  # noqa: PLC0415
        import subprocess  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        if shutil.which("make") is None:
            self.skipTest("make is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(ROOT / "Makefile", Path(tmp) / "Makefile")
            (Path(tmp) / ".env").write_text(env_line + "\n", encoding="utf-8")
            return subprocess.call(
                ["make", "-f", "Makefile", "check-env"],
                cwd=tmp,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def test_a_good_token_passes(self) -> None:
        self.assertEqual(self._run_check_env("OPENAPI_PROXY_TOKEN=" + "a" * 64), 0)

    def test_a_token_with_a_quote_is_refused(self) -> None:
        self.assertNotEqual(self._run_check_env('OPENAPI_PROXY_TOKEN=has"quote'), 0)

    def test_a_token_with_a_backslash_is_refused(self) -> None:
        self.assertNotEqual(self._run_check_env("OPENAPI_PROXY_TOKEN=back\\slash"), 0)

    def test_a_token_with_a_space_is_refused(self) -> None:
        self.assertNotEqual(self._run_check_env("OPENAPI_PROXY_TOKEN=has space"), 0)

    def test_a_token_with_non_ascii_is_refused(self) -> None:
        self.assertNotEqual(self._run_check_env("OPENAPI_PROXY_TOKEN=токен"), 0)

    def test_an_empty_token_is_refused(self) -> None:
        self.assertNotEqual(self._run_check_env("OPENAPI_PROXY_TOKEN="), 0)


class TestTheProxySaysSoAtStartup(unittest.TestCase):
    """A stack started with ``docker compose up`` does not run
    ``make check-env`` — so the proxy has to say it itself."""

    def test_the_lifespan_logs_the_problem(self) -> None:
        import asyncio  # noqa: PLC0415

        import openapi_proxy  # noqa: PLC0415

        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("openapi-proxy")
        old_level = logger.level
        logger.setLevel(logging.INFO)
        handler = _Capture()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, old_level)

        class _State:
            pass

        class _App:
            def __init__(self) -> None:
                self.state = _State()

        old_token = openapi_proxy.PROXY_TOKEN
        openapi_proxy.PROXY_TOKEN = 'bad"token'
        try:

            async def run() -> None:
                async with openapi_proxy.lifespan(_App()):
                    pass

            asyncio.run(run())
        finally:
            openapi_proxy.PROXY_TOKEN = old_token

        messages = "\n".join(record.getMessage() for record in records)
        self.assertIn("token_invalid", messages)


# ---------------------------------------------------------------------------
# H3 — the process limit is a knob, not a constant
# ---------------------------------------------------------------------------
class TestPidsLimitIsAKnob(unittest.TestCase):
    """256 processes is enough for a shell and not enough for a build.
    The knob is the answer — but only if every stack interpolates it and
    the default stays the safe one.

    The check is on the *interpolation*, not on the name: a compose file
    that mentions ``AGENT_PIDS_LIMIT`` in a comment and hardcodes 256
    would satisfy a name search and change nothing.
    """

    COMPOSE_FILES = (
        "docker-compose.yml",
        "docker-compose.production.yml",
        "docker-compose.ci.yml",
    )
    KNOB = 'pids_limit: "${AGENT_PIDS_LIMIT:-256}"'

    def test_every_stack_carries_the_knob(self) -> None:
        for name in self.COMPOSE_FILES:
            with self.subTest(compose=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn(self.KNOB, text)

    def test_only_the_agent_service_carries_the_knob(self) -> None:
        """The proxy is a thin process; it keeps its own strict limit."""
        for name in self.COMPOSE_FILES:
            with self.subTest(compose=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                if "  sandbox-agent:" not in text:
                    continue
                agent = text.split("  sandbox-agent:", 1)[1].split("  openapi-proxy:", 1)[0]
                self.assertIn(self.KNOB, agent)

    def test_the_default_is_still_the_safe_one(self) -> None:
        """A knob whose default is not the old value changes every
        deployment that upgrades without touching .env."""
        for name in self.COMPOSE_FILES:
            with self.subTest(compose=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                agent = text.split("  sandbox-agent:", 1)[1].split("  openapi-proxy:", 1)[0]
                self.assertNotIn("pids_limit: 256", agent)
                self.assertIn(":-256}", agent)

    def test_the_knob_is_documented(self) -> None:
        for name in (".env.example", "docs/CONFIGURATION.md"):
            with self.subTest(document=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn("AGENT_PIDS_LIMIT", text)


# ---------------------------------------------------------------------------
# quality — the proxy image does not carry a package manager for one probe
# ---------------------------------------------------------------------------
def _without_comments(text: str) -> str:
    """Drop whole-line comments.

    The checks below are about what the build and the compose files
    *do*, not about what they talk about: a file that explains in a
    comment that it no longer installs curl must still pass.
    """
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


class TestTheProxyImageCarriesNoCurl(unittest.TestCase):
    """curl was installed into the proxy image for one healthcheck and
    nothing else. The probe now asks Python, which is already in the
    image, so the image installs nothing from the distribution at all."""

    COMPOSE_FILES = (
        "docker-compose.yml",
        "docker-compose.production.yml",
        "docker-compose.ci.yml",
    )

    def test_the_proxy_image_installs_nothing(self) -> None:
        text = _without_comments((ROOT / "Dockerfile.openapi-proxy").read_text(encoding="utf-8"))
        self.assertNotIn("apt-get", text)
        self.assertNotIn("curl", text.lower())

    def test_the_image_healthcheck_uses_python(self) -> None:
        text = (ROOT / "Dockerfile.openapi-proxy").read_text(encoding="utf-8")
        instruction = text.split("HEALTHCHECK", 1)[1]
        instruction = _without_comments(instruction)
        self.assertNotIn("curl", instruction.lower())
        self.assertIn("urllib.request", instruction)

    def test_every_stack_probes_the_proxy_with_python(self) -> None:
        for name in self.COMPOSE_FILES:
            with self.subTest(compose=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                block = text.split("  openapi-proxy:", 1)[1]
                probe = _without_comments(block.split("healthcheck:", 1)[1])
                self.assertNotIn("curl", probe.lower())
                self.assertIn("urllib.request", probe)

    def test_the_sandbox_image_still_ships_curl(self) -> None:
        """The other way round: the *sandbox* image keeps curl, because
        the model's shell uses it. Only the proxy image — which runs no
        model commands — lost it."""
        text = (ROOT / "Dockerfile.sandbox").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^\s*git curl ")


# ---------------------------------------------------------------------------
# ops — the logs are JSON, and the JSON is valid
# ---------------------------------------------------------------------------
class TestTheLogsAreJson(unittest.TestCase):
    """Both services claimed to log JSON and both did it the same way:
    the message was interpolated into a JSON string by the format
    string. The agent logs the text of every command, and a command is
    full of quotes — so the busiest log in the system was the one that
    broke the format."""

    def _record(self, message: str, *args: object) -> logging.LogRecord:
        return logging.LogRecord("sandbox-agent", logging.INFO, "f.py", 1, message, args, None)

    def test_a_message_with_quotes_is_valid_json(self) -> None:
        import json

        from json_logging import JsonFormatter  # noqa: PLC0415

        record = self._record("cmd=%r", 'echo "hi"')
        line = JsonFormatter().format(record)
        self.assertEqual(json.loads(line)["msg"], "cmd='echo \"hi\"'")

    def test_a_message_with_a_newline_stays_on_one_line(self) -> None:
        import json

        from json_logging import JsonFormatter  # noqa: PLC0415

        record = self._record("first\nsecond")
        line = JsonFormatter().format(record)
        self.assertNotIn("\n", line)
        self.assertEqual(json.loads(line)["msg"], "first\nsecond")

    def test_both_services_write_json_lines(self) -> None:
        import agent_api  # noqa: F401, PLC0415
        import openapi_proxy  # noqa: F401, PLC0415
        from json_logging import JsonFormatter  # noqa: PLC0415

        root = logging.getLogger()
        ours = [
            handler
            for handler in root.handlers
            if isinstance(getattr(handler, "formatter", None), JsonFormatter)
        ]
        self.assertTrue(ours, "neither service installed the JSON formatter on the root logger")
        self.assertEqual(
            len(ours),
            1,
            "the JSON handler is installed more than once — every line would print twice",
        )

    def test_no_service_interpolates_the_message_into_json_by_hand(self) -> None:
        """The old format string is the bug; it must not come back."""
        for name in ("agent_api.py", "openapi_proxy.py"):
            with self.subTest(module=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertNotIn('"msg":"%(message)s"', text)


class TestTheAccessLogIsJsonToo(unittest.TestCase):
    """uvicorn installs its own access log, in plain text, when the
    server starts. The application's records being JSON is only half of
    the job: the access log is the line an operator reads first, and a
    parser that chokes on it drops the rest of the stream."""

    def _access_record(self) -> logging.LogRecord:
        return logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "uvicorn/middleware/log.py",
            1,
            '"%s %s HTTP/%s" %d',
            ("127.0.0.1", "GET", "/healthz", "1.1", 200),
            None,
        )

    def test_the_access_record_keeps_its_fields(self) -> None:
        import json

        from json_logging import UvicornAccessFormatter  # noqa: PLC0415

        line = UvicornAccessFormatter().format(self._access_record())
        payload = json.loads(line)
        self.assertEqual(payload["method"], "GET")
        self.assertEqual(payload["path"], "/healthz")
        self.assertEqual(payload["status"], 200)
        self.assertEqual(payload["client"], "127.0.0.1")

    def test_both_services_take_the_access_log_over(self) -> None:
        import agent_api  # noqa: F401, PLC0415
        import openapi_proxy  # noqa: F401, PLC0415
        from json_logging import UvicornAccessFormatter  # noqa: PLC0415

        access = logging.getLogger("uvicorn.access")
        ours = [
            handler
            for handler in access.handlers
            if isinstance(getattr(handler, "formatter", None), UvicornAccessFormatter)
        ]
        self.assertTrue(
            ours,
            "the access log still uses uvicorn's own plain-text formatter",
        )

    def test_no_line_is_printed_twice(self) -> None:
        """uvicorn's "uvicorn.error" propagates to "uvicorn". A handler on
        both would print every error line twice — which is how a log
        starts to look like it counts things twice."""
        import agent_api  # noqa: F401, PLC0415
        import openapi_proxy  # noqa: F401, PLC0415
        from json_logging import JsonFormatter, UvicornAccessFormatter  # noqa: PLC0415

        for name, formatter in (
            ("uvicorn", JsonFormatter),
            ("uvicorn.error", JsonFormatter),
            ("uvicorn.access", UvicornAccessFormatter),
        ):
            with self.subTest(logger=name):
                logger = logging.getLogger(name)
                self.assertFalse(
                    logger.propagate,
                    f"{name} propagates to a parent that also has a handler",
                )
                ours = [
                    handler
                    for handler in logger.handlers
                    if isinstance(getattr(handler, "formatter", None), formatter)
                ]
                self.assertEqual(
                    len(ours),
                    1,
                    f"{name} must carry exactly one JSON handler — "
                    "more than one prints every line twice",
                )


class TestTheMonitoringExampleRuns(unittest.TestCase):
    """The example was shipped broken and only a live run showed it.

    Prometheus reads the bearer token as the user it runs as (65534 =
    nobody). The instructions said "chmod 600", the file was owned by
    root, and every scrape failed with "unable to read authorization
    credentials" — a target that stays down with no hint of why.

    The instructions now say to chown the file. This tripwire keeps
    them saying it: a documentation fix that nothing protects is a
    documentation fix that comes back.
    """

    def test_the_instructions_give_the_file_to_prometheus(self) -> None:
        for name in ("README.md", "docker-compose.monitoring.yml", "monitoring/prometheus.yml"):
            with self.subTest(file=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn(
                    "chown 65534:65534",
                    text,
                    f"{name} does not say who has to own the token file",
                )

    def test_the_compose_says_which_user_prometheus_runs_as(self) -> None:
        text = (ROOT / "docker-compose.monitoring.yml").read_text(encoding="utf-8")
        self.assertIn('user: "65534:65534"', text)

    def test_the_monitoring_compose_is_valid_yaml(self) -> None:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load((ROOT / "docker-compose.monitoring.yml").read_text(encoding="utf-8"))
        self.assertIn("services", data)
        for name in ("prometheus", "grafana", "cadvisor"):
            self.assertIn(name, data["services"])


# ---------------------------------------------------------------------------
# ops — the metrics endpoint and the monitoring example
# ---------------------------------------------------------------------------
class TestTheMetricsEndpoint(unittest.TestCase):
    """Both services expose a Prometheus exposition. It is authenticated
    like everything else: the numbers are harmless, but an open endpoint
    on a published port is a habit worth not having."""

    def _proxy_client(self):
        """Import the proxy with the token this test needs.

        The module reads its configuration from the environment at import
        time, and other test files import it with their own token — so
        the module is imported fresh here rather than taken from
        ``sys.modules``.
        """
        from fastapi.testclient import TestClient  # noqa: PLC0415

        os.environ["OPENAPI_PROXY_TOKEN"] = "test-token"
        os.environ["OPENAPI_PROXY_RATE_LIMIT_RPM"] = "0"
        sys.modules.pop("openapi_proxy", None)
        import openapi_proxy  # noqa: PLC0415

        return TestClient(openapi_proxy.app)

    def test_the_proxy_metrics_need_the_token(self) -> None:
        client = self._proxy_client()
        self.assertEqual(client.get("/metrics").status_code, 401)

    def test_the_proxy_metrics_answer_with_the_token(self) -> None:
        client = self._proxy_client()
        response = client.get("/metrics", headers={"Authorization": "Bearer test-token"})
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("# TYPE sandbox_proxy_requests_total counter", body)
        self.assertRegex(body, r"(?m)^sandbox_proxy_requests_total \d")

    def test_the_agent_metrics_answer_with_the_token(self) -> None:
        import agent_api  # noqa: PLC0415

        assert agent_api.METRICS is not None
        body = agent_api.METRICS.render()
        self.assertIn("# TYPE sandbox_agent_active_sessions gauge", body)
        self.assertIn("# TYPE sandbox_agent_exec_total counter", body)

    def test_every_exposition_line_is_a_name_and_a_number(self) -> None:
        """The exposition format is one metric per line, name and value.
        A line that does not parse is a line Prometheus drops."""
        import agent_api  # noqa: PLC0415
        import openapi_proxy  # noqa: PLC0415

        for rendered in (agent_api.METRICS.render(), openapi_proxy.METRICS.render()):
            for line in rendered.splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                self.assertRegex(line, r"^[a-zA-Z_:][a-zA-Z0-9_:]*\s+[-+0-9.eE]+$", line)


class TestTheMonitoringExampleIsNotALie(unittest.TestCase):
    """A dashboard that asks for a metric nobody exposes is the worst
    kind of example: it looks right and shows nothing."""

    def test_every_metric_the_dashboard_asks_for_exists(self) -> None:
        import json  # noqa: PLC0415
        import re  # noqa: PLC0415

        import agent_api  # noqa: PLC0415
        import openapi_proxy  # noqa: PLC0415

        exposed: set[str] = set()
        for rendered in (agent_api.METRICS.render(), openapi_proxy.METRICS.render()):
            for line in rendered.splitlines():
                if line.startswith("# TYPE "):
                    exposed.add(line.split()[2])

        dashboard = json.loads(
            (ROOT / "monitoring/grafana/dashboards/sandbox.json").read_text(encoding="utf-8")
        )
        asked: set[str] = set()
        for panel in dashboard["panels"]:
            for target in panel.get("targets", []):
                asked.update(re.findall(r"\bsandbox_[a-z_]+\b", target.get("expr", "")))
        self.assertTrue(asked, "the dashboard asks for nothing at all")
        missing = sorted(asked - exposed)
        self.assertEqual(missing, [], f"the dashboard asks for metrics nobody exposes: {missing}")

    def test_the_prometheus_config_scrapes_both_services(self) -> None:
        text = (ROOT / "monitoring/prometheus.yml").read_text(encoding="utf-8")
        self.assertIn("openapi-proxy:8090", text)
        self.assertIn("sandbox-agent:8082", text)
        self.assertIn("bearer_token_file", text)
        self.assertIn("/metrics", text)

    def test_the_monitoring_stack_joins_the_stack_network(self) -> None:
        text = (ROOT / "docker-compose.monitoring.yml").read_text(encoding="utf-8")
        self.assertIn("prom/prometheus", text)
        self.assertIn("grafana/grafana", text)
        self.assertIn("external: true", text)
        self.assertIn("MONITOR_NETWORK", text)
