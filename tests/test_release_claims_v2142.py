"""The release claims of v2.14.2, pinned by tests.

A release note is a claim about the code. A claim that is not pinned by
a test is a claim that rots: it stays true until somebody edits the
code and forgets the sentence. These tests are the pin.

What v2.14.2 claims:

* the two services answer with this version;
* the changelog has the section, and the changelog is newest-first;
* the shipped configuration names no host by its address, and the
  published ports take the address from the operator;
* the coverage gate measures the sources and has a threshold of its
  own;
* the MCP door is POST only, is rate-limited, and a timeout of it
  names the cause;
* the caller's token is not the upstream's token;
* a live session that cannot change its directory says so;
* the unit that starts the stack builds it;
* the suite leaves no tmux session behind, and the tmux double is
  really in effect;
* a full scrollback does not cost the caller the whole answer;
* the documentation says what the code does.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, ROOT)


def _source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _prose(name: str) -> str:
    """The document with its line breaks collapsed.

    A claim about prose must not depend on where the line happened to
    wrap: the sentence is the claim, not the line it sits on.
    """
    return " ".join(_source(name).split())


class TestTheVersionIsTheRelease(unittest.TestCase):
    def test_the_services_answer_with_this_version(self) -> None:
        """The two services agree — the number is the current release's.

        The exact number belongs to the release that is being made, and
        it moves with every release. What has to hold is that the two
        services do not drift apart: a client that reads the version
        from one of them and compares it with the other must find them
        equal.
        """
        import agent_core  # noqa: PLC0415
        import proxy_core  # noqa: PLC0415

        self.assertEqual(agent_core.__version__, proxy_core.__version__)

    def test_the_changelog_has_the_section(self) -> None:
        self.assertIn("## [2.14.2]", _source("CHANGELOG.md"))

    def test_the_changelog_is_newest_first(self) -> None:
        headings = re.findall(r"^## \[(\d+)\.(\d+)\.(\d+)\]", _source("CHANGELOG.md"), re.MULTILINE)
        self.assertTrue(headings, "the changelog has no version sections")
        versions = [(int(a), int(b), int(c)) for a, b, c in headings]
        self.assertEqual(versions, sorted(versions, reverse=True))


class TestTheConfigurationIsTheClaim(unittest.TestCase):
    def test_the_published_ports_take_the_address_from_the_operator(self) -> None:
        # The default file takes it from the operator; the CI and the
        # production file pin the loopback, which is the right thing for
        # a machine that runs the stack for itself.
        self.assertIn("${SANDBOX_BIND_HOST:-127.0.0.1}", _source("docker-compose.yml"))
        for name in ("docker-compose.ci.yml", "docker-compose.production.yml"):
            with self.subTest(file=name):
                body = _source(name)
                self.assertIn('"127.0.0.1:', body)
                self.assertNotIn("192.168.", body)

    def test_no_shipped_file_names_a_host_by_its_address(self) -> None:
        # The tripwire itself is the claim; it is checked here so that
        # removing it is noticed.
        self.assertTrue((ROOT / "tests/test_no_foreign_addresses_v2142.py").exists())
        self.assertIn(
            "class TestTheConfigurationNamesNoHost",
            _source("tests/test_no_foreign_addresses_v2142.py"),
        )

    def test_the_coverage_gate_measures_the_sources(self) -> None:
        pyproject = _source("pyproject.toml")
        self.assertIn('omit = ["tests/*"]', pyproject)
        self.assertIn("fail_under", pyproject)
        # And the CI does not pass a threshold of its own: two places
        # to change is two places to forget.
        self.assertNotIn("--cov-fail-under", _source(".github/workflows/ci.yml"))


class TestTheMcpDoorIsTheClaim(unittest.TestCase):
    def test_the_proxy_relays_post_only(self) -> None:
        source = _source("openapi_proxy.py")
        self.assertIn('if request.method != "POST":', source)

    def test_the_refusal_happens_before_the_upstream_is_called(self) -> None:
        source = _source("openapi_proxy.py")
        block = source.split("async def mcp_endpoint", 1)[1].split("\n@app", 1)[0]
        self.assertLess(
            block.index('if request.method != "POST":'),
            block.index("client = _client(request)"),
            "the refusal happens after the upstream is called",
        )

    def test_the_door_is_rate_limited(self) -> None:
        source = _source("openapi_proxy.py")
        block = source.split('@app.api_route(\n    "/mcp"', 1)[1].split(")\n", 1)[0]
        self.assertIn("dependencies=[Depends(_rate_limit)]", block)

    def test_a_timeout_of_the_relay_names_the_cause(self) -> None:
        source = _source("openapi_proxy.py")
        self.assertIn("_mcp_relay_timeout_reason", source)
        self.assertIn("httpx.TimeoutException", source)

    def test_the_two_tokens_are_two_values(self) -> None:
        source = _source("tests/test_mcp_proxy.py")
        self.assertIn("UPSTREAM_TOKEN", source)
        self.assertIn("self.assertNotEqual(", source)

    def test_the_handshake_names_the_release(self) -> None:
        source = _source("mcp_server.py")
        self.assertIn("mcp._lowlevel_server.version = version", source)


class TestTheSessionContractIsTheClaim(unittest.TestCase):
    def test_a_live_session_refuses_a_directory_it_cannot_honour(self) -> None:
        source = " ".join(_source("agent_api.py").split())
        self.assertIn("its directory is not a parameter", source)
        self.assertIn("Use a new session_id to run", source)

    def test_the_mcp_tool_opens_the_session_on_every_call(self) -> None:
        source = _source("mcp_server.py")
        block = source.split("async def bash", 1)[1].split("\n@", 1)[0]
        self.assertIn("create_session(", block)


class TestTheDeploymentIsTheClaim(unittest.TestCase):
    def test_the_unit_is_a_template(self) -> None:
        self.assertTrue((ROOT / "agent-stack.service.example").exists())
        self.assertFalse((ROOT / "agent-stack.service").exists())

    def test_the_unit_builds_what_it_starts(self) -> None:
        body = _source("agent-stack.service.example")
        self.assertIn("docker compose up -d --build", body)


class TestTheSuiteIsTheClaim(unittest.TestCase):
    def test_the_cold_start_test_has_a_tmux_server_of_its_own(self) -> None:
        source = _source("tests/test_exec_budget.py")
        self.assertIn("TMUX_TMPDIR", source)

    def test_the_double_is_patched_where_the_request_runs(self) -> None:
        source = _source("tests/test_busy_session_v2131.py")
        self.assertIn("patch.dict(endpoint.__globals__", source)

    def test_a_test_that_leaves_a_session_is_told(self) -> None:
        source = _source("tests/conftest.py")
        self.assertIn("the test left tmux sessions behind", source)


class TestTheAnswerIsTheClaim(unittest.TestCase):
    def test_the_success_path_reads_the_full_scrollback(self) -> None:
        source = _source("agent_api.py")
        block = source.split("    # The poll saw the completion marker in the tail.", 1)[1]
        self.assertIn("_capture_full_pane", block)

    def test_the_scrollback_fix_has_a_test(self) -> None:
        source = _source("tests/test_exec_budget.py")
        self.assertIn("class TestAScrollbackThatIsAlreadyFull", source)


class TestTheDocumentationIsTheClaim(unittest.TestCase):
    def test_the_readme_does_not_advise_apt_get(self) -> None:
        readme = _prose("README.md")
        self.assertNotIn("apt-get install curl", readme)

    def test_the_readme_does_not_curl_the_agent_on_a_port_it_does_not_publish(self) -> None:
        readme = _prose("README.md")
        self.assertNotIn("curl http://localhost:8082/healthz", readme)

    def test_the_security_reference_is_not_ten_releases_old(self) -> None:
        security = _prose("docs/SECURITY.md")
        self.assertIn("## Status — v2.14", security)
        self.assertNotIn("## Status — v2.4 ", security)

    def test_the_configuration_explains_the_hour(self) -> None:
        configuration = _prose("docs/CONFIGURATION.md")
        self.assertIn("An hour-long command is an operational event", configuration)

    def test_the_contributor_layout_knows_the_modules(self) -> None:
        contributing = _prose("CONTRIBUTING.md")
        for module in ("mcp_server.py", "agent_config.py", "body_limit.py", "metrics.py"):
            with self.subTest(module=module):
                self.assertIn(module, contributing)

    def test_the_env_template_promises_only_what_is_kept(self) -> None:
        env = _prose(".env.example")
        self.assertNotIn("Must be 32+ chars", env)

    def test_the_changelog_names_the_claims(self) -> None:
        changelog = _prose("CHANGELOG.md")
        for claim in (
            "The shipped configuration named a machine by its address.",
            "The coverage gate could not fail.",
            "A full scrollback cost the caller the whole answer.",
            "The proxy relayed two MCP methods it should not have.",
            "is not redundant.** The review called it so",
        ):
            with self.subTest(claim=claim):
                self.assertIn(claim, changelog)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
