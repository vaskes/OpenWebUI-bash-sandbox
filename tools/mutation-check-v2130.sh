#!/usr/bin/env bash
# Mutation check for the v2.13.0 tripwires.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The mutations come in two families: the read budget (the new knob) and
# the search that was removed. A removal that cannot be seen coming back
# is not a removal.
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

mutate() {
    # mutate <file> <python-expr-old> <python-expr-new> <test-selector> <label>
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

echo "== the read budget stops counting =="
mutate mcp_server.py \
    'budget = MCP_READ_LIMIT_BYTES' \
    'budget = 0' \
    'tests/test_mcp_read_limit_v2130.py::TestTheChain::test_the_budget_wins_when_the_policy_is_larger' \
    "the budget is one of the links in the chain"

echo "== the budget's default changes =="
mutate agent_config.py \
    'MCP_READ_LIMIT_DEFAULT_BYTES = 1 * _MIB' \
    'MCP_READ_LIMIT_DEFAULT_BYTES = 8 * _MIB' \
    'tests/test_mcp_read_limit_v2130.py::TestTheBudgetIsAStartupFact::test_the_default_is_one_mebibyte' \
    "the default budget is one mebibyte"

echo "== an over-the-ceiling value is clamped instead of refused =="
mutate agent_config.py \
    'maximum=MCP_READ_CEILING_BYTES,' \
    'maximum=None,' \
    'tests/test_mcp_read_limit_v2130.py::TestTheBudgetIsAStartupFact::test_a_value_over_the_ceiling_is_a_startup_failure' \
    "a value over the ceiling is a startup failure"

echo "== the refusal stops naming the file's size =="
mutate mcp_server.py \
    'f"the file is {size} bytes, which is over the limit of {limit} bytes; "' \
    'f"the file is over the limit of {limit} bytes; "' \
    'tests/test_mcp_read_limit_v2130.py::TestTheBudgetInTheWire::test_a_file_over_the_budget_is_refused' \
    "the refusal names the size and the limit"

echo "== file_info reports a limit that is not enforced =="
mutate mcp_server.py \
    'limit=_read_limit(),' \
    'limit=_binary_limit(),' \
    'tests/test_mcp_read_limit_v2130.py::TestTheBudgetInTheWire::test_file_info_reports_the_limit_that_applies' \
    "file_info reports the limit that applies"

echo "== the HTTP door inherits the model's budget =="
mutate agent_config.py \
    'os.environ.get("AGENT_BINARY_LIMIT", "2048k")' \
    'os.environ.get("AGENT_BINARY_LIMIT", "512k")' \
    'tests/test_mcp_read_limit_v2130.py::TestTheBudgetInTheWire::test_the_http_door_is_not_affected' \
    "the HTTP door keeps its own limit"

echo "== the ceiling gets a second home =="
mutate mcp_server.py \
    'SERVER_NAME = "openwebui-bash-sandbox"' \
    'SERVER_NAME = "openwebui-bash-sandbox"
MCP_MAX_READ_BYTES = 64 * 1024 * 1024' \
    'tests/test_mcp_read_limit_v2130.py::TestTheCeilingHasOneHome::test_the_mcp_server_does_not_define_its_own' \
    "the ceiling is defined once"

echo "== a search engine creeps back into the deployment =="
mutate docker-compose.yml \
    '      - SANDBOX_URL=http://sandbox-agent:8082' \
    '      - SANDBOX_URL=http://sandbox-agent:8082
      - SEARXNG_URL=http://search.invalid:8888' \
    'tests/test_no_search_v2130.py::TestTheSearchIsGone::test_no_file_names_a_search_engine' \
    "the deployment names no search engine"

echo "== the limits table drifts away from the code =="
mutate docs/ARCHITECTURE.md \
    'the smallest of `AGENT_MCP_MAX_READ_BYTES` (1 MiB), `AGENT_BINARY_LIMIT` and 64 MiB' \
    'the smallest of `AGENT_MCP_MAX_READ_BYTES` (1 MiB), `AGENT_BINARY_LIMIT` and 32 MiB' \
    'tests/test_drift_architecture_docs.py::TestTheLimitsAreTheCodeLimits::test_the_read_ceiling_is_the_code_ceiling' \
    "the limits table carries the code's ceiling"

echo "== the new knob is not documented =="
mutate docs/CONFIGURATION.md \
    '| `AGENT_MCP_MAX_READ_BYTES` | `1m`                                  | How many bytes one MCP `read_file` may put into a model'"'"'s context. `0` = no budget of its own. 0..64m. A file over the budget is refused, and the refusal names the file'"'"'s size and the limit. The HTTP `/files` door keeps `AGENT_BINARY_LIMIT` — there the bytes land in a file, not in a prompt. |' \
    '' \
    'tests/test_drift_release_claims.py::TestAllEnvVarsDocumented::test_every_runtime_env_var_appears_in_configuration' \
    "every env var the code reads is documented"

echo
echo "mutations: $PASS went red as expected, $FAIL did not"
[ "$FAIL" -eq 0 ] || exit 1
