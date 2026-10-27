#!/usr/bin/env bash
# Mutation check for the v2.14.2 tripwires (the address of a host in the
# shipped configuration).
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties:
#
#   * the deployable configuration names no host by its IPv4 address;
#   * the published ports take the address from the operator;
#   * the default bind address is the loopback;
#   * the scan still looks at the compose file;
#   * the coverage gate measures the sources and has a threshold of its own;
#   * the MCP door is rate-limited like the exec door;
#   * a timeout of the MCP relay names the cause and both horizons;
#   * the caller's token does not travel upstream;
#   * a directory a running session cannot honour is refused, not ignored;
#   * a live session is adopted, not replaced;
#   * the systemd unit builds the image it starts;
#   * the tmux double is really in effect while the request runs;
#   * a test leaves no tmux session behind;
#   * the whole ssh command reaches the agent;
#   * the handshake names the version of the release;
#   * the proxy's MCP door is POST only, and refuses before the pool;
#   * the image scan sees the MCP module;
#   * the Host knob is read once, at start-up;
#   * the terminal survives the cancellation it causes.
set -u
cd "$(dirname "$0")/.."

PY="${PY:-}"
if [ -z "$PY" ]; then
    for candidate in /workspace/.venv-owbs/bin/python3 python3; do
        if command -v "$candidate" >/dev/null 2>&1 \
            && "$candidate" -c "import pytest" >/dev/null 2>&1; then
            PY="$candidate"
            break
        fi
    done
fi
if [ -z "$PY" ]; then
    echo "no python with pytest found — set PY=/path/to/python" >&2
    exit 2
fi

PASS=0
FAIL=0

# A stale bytecode cache can hide a mutation: Python keys a .pyc by the
# source's mtime AND its size, so two mutations of the same length in the
# same second leave the second one invisible.
export PYTHONDONTWRITEBYTECODE=1

mutate() {
    # mutate <file> <old> <new> <test-selector> <label>
    local file="$1" old="$2" new="$3" selector="$4" label="$5"
    local backup
    backup="$(mktemp -p /tmp mut.XXXXXX)"
    cp "$file" "$backup"
    python3 - "$file" "$old" "$new" <<'PY'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8").read()
if old not in text:
    sys.exit(f"PATTERN NOT FOUND in {path}: {old!r}")
open(path, "w", encoding="utf-8").write(text.replace(old, new, 1))
PY
    if [ $? -ne 0 ]; then
        echo "SKIP  $label (pattern not found — mutation did not apply)"
        cp "$backup" "$file"
        rm -f "$backup"
        FAIL=$((FAIL+1))
        return
    fi
    find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    if grep -q "no tests ran\|ERROR: not found" /tmp/mut.out; then
        # A selector that matches no test is not a caught mutation: the
        # suite "went red" because it never ran. That is coverage of
        # nothing, and it must not be counted as a catch.
        echo "INVALID $label — the selector matched no test"
        cp "$backup" "$file"
        rm -f "$backup"
        FAIL=$((FAIL+1))
        return
    fi
    if $PY -m pytest "$selector" -q >/tmp/mut.out 2>&1; then
        echo "FAIL  $label — the test stayed GREEN under the mutation"
        cp "$backup" "$file"
        rm -f "$backup"
        FAIL=$((FAIL+1))
    else
        echo "OK    $label — the test went red"
        cp "$backup" "$file"
        rm -f "$backup"
        PASS=$((PASS+1))
    fi
}

RULE="tests/test_no_foreign_addresses_v2142.py"

echo "== the address of a host is back in the compose file =="
mutate docker-compose.yml \
    '"${SANDBOX_BIND_HOST:-127.0.0.1}:8090:8090"' \
    '"192.168.10.7:8090:8090"' \
    "$RULE::TestTheConfigurationNamesNoHost::test_no_file_names_a_host_by_its_address" \
    "the compose file names no host"

echo "== the address of a host is back in the documentation =="
mutate docs/CONFIGURATION.md \
    '| `127.0.0.1:3001`   | open-webui     | Web UI                                |' \
    '| `192.168.10.7:3001` | open-webui     | Web UI                                |' \
    "$RULE::TestTheConfigurationNamesNoHost::test_no_file_names_a_host_by_its_address" \
    "the documentation names no host"

echo "== the address stops being the operator's choice =="
mutate docker-compose.yml \
    '"${SANDBOX_BIND_HOST:-127.0.0.1}:8090:8090"' \
    '"127.0.0.1:8090:8090"' \
    "$RULE::TestTheConfigurationNamesNoHost::test_the_published_ports_take_the_address_from_the_operator" \
    "the published ports take the address from the operator"

echo "== the default publishes on every interface =="
mutate docker-compose.yml \
    '${SANDBOX_BIND_HOST:-127.0.0.1}:8090:8090' \
    '${SANDBOX_BIND_HOST:-0.0.0.0}:8090:8090' \
    "$RULE::TestTheConfigurationNamesNoHost::test_the_default_bind_address_is_the_loopback" \
    "the default bind address is the loopback"

echo "== the scan stops looking at the compose file =="
mutate tests/test_no_foreign_addresses_v2142.py \
    '    "docker-compose*.yml",' \
    '    "docker-compose*.yaml",' \
    "$RULE::TestTheConfigurationNamesNoHost::test_the_scan_covers_something" \
    "the scan still looks at the compose file"

GATE="tests/test_coverage_gate_v2142.py"

echo "== the tests are measured again =="
mutate pyproject.toml \
    '[tool.coverage.run]
omit = ["tests/*"]' \
    '[tool.coverage.run]' \
    "$GATE::TestTheCoverageGateMeasuresTheSources::test_the_tests_are_not_part_of_the_measurement" \
    "the tests are not part of the measurement"

echo "== a source module is measured out =="
mutate pyproject.toml \
    'omit = ["tests/*"]' \
    'omit = ["tests/*", "agent_api.py"]' \
    "$GATE::TestTheCoverageGateMeasuresTheSources::test_only_the_tests_are_omitted" \
    "only the tests are omitted"

echo "== the threshold falls back to the meaningless number =="
mutate pyproject.toml \
    'fail_under = 85' \
    'fail_under = 70' \
    "$GATE::TestTheCoverageGateMeasuresTheSources::test_the_threshold_is_a_real_gate" \
    "the threshold is a real gate"

echo "== the CI command takes the threshold back =="
mutate .github/workflows/ci.yml \
    'pytest --cov=. --cov-report=xml --cov-report=term tests/' \
    'pytest --cov=. --cov-report=xml --cov-report=term --cov-fail-under=70 tests/' \
    "$GATE::TestTheCoverageGateMeasuresTheSources::test_the_ci_command_does_not_pass_a_threshold" \
    "the CI command passes no threshold of its own"

DOOR="tests/test_mcp_proxy.py"

echo "== the MCP door loses its limit =="
mutate openapi_proxy.py \
    '    dependencies=[Depends(_rate_limit)],
)' \
    ')' \
    "$DOOR::TestTheMcpDoorIsGuarded::test_the_door_is_rate_limited" \
    "the MCP door is rate-limited"

echo "== the timeout of the relay goes back to an empty reason =="
mutate openapi_proxy.py \
    'raise HTTPException(status_code=502, detail=_mcp_relay_timeout_reason(exc)) from exc' \
    'raise HTTPException(status_code=502, detail=f"sandbox-agent request failed: {exc}") from exc' \
    "$DOOR::TestTheMcpDoorIsGuarded::test_a_timeout_of_the_relay_names_the_cause" \
    "a timeout of the relay names the cause"

echo "== the caller's token travels upstream =="
mutate openapi_proxy.py \
    '    headers.update(_upstream_headers())' \
    '    headers["Authorization"] = request.headers.get("authorization", "")' \
    "$DOOR::TestTheProxyRelaysMcp::test_the_caller_token_is_not_forwarded" \
    "the caller's token does not travel upstream"

CWD="tests/test_cwd_contract_v2142.py"

echo "== the directory of a running session is not checked =="
mutate agent_api.py \
    '        if requested and live and requested != live:' \
    '        if False and requested and live and requested != live:' \
    "$CWD::TestTheDirectoryOfASession::test_a_cwd_the_session_cannot_honour_is_refused" \
    "a cwd the session cannot honour is refused"

echo "== the live directory of the pane is ignored =="
mutate agent_api.py \
    '            adopted = live or requested or WORKSPACE_REAL' \
    '            adopted = requested or WORKSPACE_REAL' \
    "$CWD::TestTheDirectoryOfASession::test_a_live_session_is_adopted_not_replaced" \
    "a live session is adopted, not replaced"

echo "== the MCP tool does not open the session any more =="
mutate mcp_server.py \
    '        await create_session(CreateSession(session_id=sid, cwd=cwd))' \
    '        pass  # the session is not opened' \
    "$CWD::TestTheDirectoryOfASession::test_a_cwd_the_session_cannot_honour_is_refused"     "the MCP tool opens the session on every call"

echo "== the systemd unit stops building what it starts =="
mutate agent-stack.service.example \
    'ExecStart=/usr/bin/docker compose up -d --build' \
    'ExecStart=/usr/bin/docker compose up -d' \
    "tests/test_deploy_artifacts.py::TestTheSystemdUnitAlwaysBuilds::test_the_unit_builds_on_start" \
    "the systemd unit builds what it starts"

echo "== the tmux double is patched into the wrong namespace =="
mutate tests/test_busy_session_v2131.py \
    '    return patch.dict(endpoint.__globals__, {"_tmux": double})' \
    '    return patch.dict({}, {"_tmux": double})' \
    "tests/test_busy_session_v2131.py::TestTheBusySessionLeak::test_the_stdout_is_empty" \
    "the double is in effect while the request runs"

echo "== a test leaves its tmux session behind =="
mutate tests/conftest.py \
    '        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, check=False)' \
    '        pass  # the session is left behind' \
    "tests/test_exec_budget.py::TestLiveExecIsComplete::test_the_pane_really_holds_the_budget" \
    "a test leaves no session behind"

echo "== the ssh command is cut short =="
mutate openapi_proxy.py \
    '    return bare_host, " ".join(shlex.quote(t) for t in tokens[:])' \
    '    return bare_host, " ".join(shlex.quote(t) for t in tokens[:2])' \
    "tests/test_openapi_proxy_endpoints.py::TestSshAllowlist::test_a_flag_after_the_host_survives" \
    "the whole command reaches the agent"

echo "== the handshake reports a placeholder version =="
mutate agent_api.py \
    '_set_mcp_version(__version__)' \
    '_set_mcp_version("0.0.0")' \
    "tests/test_mcp_server.py::TestTheProtocol::test_the_handshake_names_the_version_of_the_release" \
    "the handshake names the version of the release"

echo "== the proxy relays every method =="
mutate openapi_proxy.py \
    '    if request.method != "POST":' \
    '    if request.method not in ("GET", "POST", "DELETE"):' \
    "tests/test_mcp_proxy.py::TestTheMcpDoorIsPostOnly::test_get_is_refused_without_asking_the_agent" \
    "the proxy's MCP door is POST only"

echo "== the image scan stops looking at the MCP module =="
mutate tests/test_deploy_artifacts.py \
    'AGENT_MODULES = ("agent_api.py", "agent_core.py", "agent_config.py", "mcp_server.py")' \
    'AGENT_MODULES = ("agent_api.py", "agent_core.py", "agent_config.py")' \
    "tests/test_deploy_artifacts.py::TestAgentModulesAreInTheImage::test_the_scan_looks_at_every_module_the_agent_ships" \
    "the scan sees the MCP module"

echo "== the Host knob ignores the environment =="
mutate agent_config.py \
    '    for item in os.environ.get("AGENT_MCP_ALLOWED_HOSTS", "").split(",")' \
    '    for item in "".split(",")' \
    "tests/test_mcp_server.py::TestTheHostCheckWhenItIsOn::test_a_foreign_host_is_refused" \
    "the Host knob is read from the environment"

echo "== the terminal dies when the reader is cancelled =="
mutate openapi_proxy.py \
    '        except (asyncio.CancelledError, Exception):  # noqa: BLE001' \
    '        except Exception:  # noqa: BLE001' \
    "tests/test_wave2_fixes.py::TestWebSocketNoTokenInQuery::test_websocket_protocol_header_accepted" \
    "the terminal survives the cancellation it causes"

echo
echo "mutations: $PASS went red as expected, $FAIL did not"
[ "$FAIL" -eq 0 ] || exit 1
