#!/usr/bin/env bash
# Mutation check for the architecture-doc tripwire.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one documented property on purpose; the named test must go red.
# If it stays green, the test is decoration and the property is unpinned.
#
# The docs are the thing that drifted in the first place: the v2.12.1
# release shipped a README and an ARCHITECTURE.md that drew a world that
# had been gone for a release. These mutations are the proof that the new
# tripwire would have caught it.
set -u
cd "$(dirname "$0")/.."

PY=/workspace/.venv-owbs/bin/python3
TESTS=tests/test_drift_architecture_docs.py
PASS=0
FAIL=0

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
    if $PY -m pytest "$selector" -q >/tmp/mut-docs.out 2>&1; then
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

echo "== a route vanishes from the architecture table =="
mutate docs/ARCHITECTURE.md \
  '| GET    | `/files/meta?path=P` | Size/mtime/name of a workspace file |' \
  '| GET    | `/files/gone?path=P` | Size/mtime/name of a workspace file |' \
  "$TESTS::TestRoutesAreDocumented::test_agent_routes_are_in_the_architecture_tables" \
  "the agent table lists every route"

echo "== a route vanishes from the README table =="
mutate README.md \
  '| POST   | `/mcp`     | Relay of the agent'"'"'s MCP endpoint (same token)   |' \
  '| POST   | `/mcp-x`   | Relay of the agent'"'"'s MCP endpoint (same token)   |' \
  "$TESTS::TestRoutesAreDocumented::test_proxy_routes_are_in_the_readme_tables" \
  "the README lists every route"

echo "== a module vanishes from the layout =="
mutate docs/ARCHITECTURE.md \
  'body_limit.py         # Pure-ASGI body-size limiter, shared by both services
' \
  '' \
  "$TESTS::TestTheModuleLayout::test_every_python_module_is_listed" \
  "the module layout lists every module"

echo "== a tool is renamed in the docs =="
mutate docs/ARCHITECTURE.md \
  'The MCP door exposes four tools: `bash`, `read_file`, `file_info`, `delete_file`.' \
  'The MCP door exposes four tools: `bash`, `read_file`, `fileinfo`, `delete_file`.' \
  "$TESTS::TestTheMcpToolsAreDocumented::test_the_documented_tools_are_the_code_tools" \
  "the documented tools are the code tools"

echo "== a component section disappears =="
mutate docs/ARCHITECTURE.md \
  '### 3. `open-webui` (upstream image, optional)' \
  '### 3. A component that is not a service' \
  "$TESTS::TestTheComponentsAreTheComposeServices::test_every_component_section_is_a_service" \
  "every component section is a compose service"

echo "== the read ceiling in the docs is wrong =="
mutate docs/ARCHITECTURE.md \
  'the smaller of `AGENT_BINARY_LIMIT` and 64 MiB' \
  'the smaller of `AGENT_BINARY_LIMIT` and 32 MiB' \
  "$TESTS::TestTheLimitsAreTheCodeLimits::test_the_read_ceiling_is_the_code_ceiling" \
  "the read ceiling is the code ceiling"

echo "== the relay timeout in the docs is wrong =="
mutate docs/ARCHITECTURE.md \
  '| the MCP relay'"'"'s patience | 900 s' \
  '| the MCP relay'"'"'s patience | 75 s' \
  "$TESTS::TestTheLimitsAreTheCodeLimits::test_the_relay_timeout_is_the_code_timeout" \
  "the relay timeout is the code timeout"

echo "== the body limit in the docs is wrong =="
mutate docs/ARCHITECTURE.md \
  '| request body | 2 MiB' \
  '| request body | 4 MiB' \
  "$TESTS::TestTheLimitsAreTheCodeLimits::test_the_body_limit_is_the_code_limit" \
  "the body limit is the code limit"

echo "== the two services stop sharing the body limit =="
mutate openapi_proxy.py \
  'MAX_BODY_BYTES = 2 * 1024 * 1024' \
  'MAX_BODY_BYTES = 4 * 1024 * 1024' \
  "$TESTS::TestTheLimitsAreTheCodeLimits::test_both_services_share_the_same_body_limit" \
  "both services share the body limit"

echo "== the stale world creeps back in =="
mutate docs/ARCHITECTURE.md \
  '## Component responsibilities' \
  'The sandbox is three loosely-coupled HTTP services.

## Component responsibilities' \
  "$TESTS::TestTheStaleWorldIsGone::test_the_docs_do_not_claim_three_services" \
  "the docs do not claim the world that is gone"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
