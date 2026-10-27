#!/usr/bin/env bash
# Mutation check for the v2.12.1 tripwires.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one fix on purpose; the named test must go red. If it stays
# green, the test is decoration and the fix is unpinned.
set -u
cd "$(dirname "$0")/.."

PY=/workspace/.venv-owbs/bin/python3
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

echo "== the body limiter stops counting =="
mutate body_limit.py \
  'total += len(message.get("body", b""))' \
  'total += 0' \
  'tests/test_hardening_v2121.py' \
  "chunked bodies are counted"

echo "== the body limiter stops refusing =="
mutate body_limit.py \
  'if total > self.limit:' \
  'if False:' \
  'tests/test_hardening_v2121.py' \
  "over-limit bodies are refused"

echo "== the open follows symlinks again =="
mutate agent_api.py \
  '        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)' \
  '        return os.open(parts[-1], os.O_RDONLY, dir_fd=dir_fd)' \
  'tests/test_hardening_v2121.py' \
  "the open does not follow a symlink"

echo "== the refusal loses its reason =="
mutate agent_api.py \
  'if exc.errno in (errno.ELOOP, errno.ENOTDIR):' \
  'if False:' \
  'tests/test_hardening_v2121.py' \
  "the symlink refusal names the reason"

echo "== the read loses its ceiling =="
mutate mcp_server.py \
  'def _read_limit() -> int:' \
  'def _read_limit() -> int:
    return 0' \
  'tests/test_review_v2120.py' \
  "the read ceiling is gone"

echo "== the read stops noticing a file that grew =="
mutate mcp_server.py \
  'payload = handle.read(limit + 1)' \
  'payload = handle.read()' \
  'tests/test_hardening_v2121.py' \
  "the read is bounded by the limit"

echo "== the read stops noticing a file that grew (the refusal) =="
mutate mcp_server.py \
  'if len(payload) > limit:' \
  'if False:' \
  'tests/test_hardening_v2121.py' \
  "a file that grew while it was read is refused"

echo "== the failure stops being logged =="
mutate mcp_server.py \
  'logger.exception("mcp.read_file refused %s", path)' \
  'logger.info("mcp.read_file refused %s", path)' \
  'tests/test_review_v2120.py' \
  "a failing tool leaves a trace"

echo "== the failure stops being logged (the other handler) =="
mutate mcp_server.py \
  'logger.exception("mcp.bash failed in the session %s", sid)' \
  'logger.info("mcp.bash failed in the session %s", sid)' \
  'tests/test_review_v2120.py' \
  "every failure path leaves a trace"

echo "== the relay stops outlasting the agent =="
mutate openapi_proxy.py \
  'MCP_RELAY_TIMEOUT_SECONDS: float = 3660.0' \
  'MCP_RELAY_TIMEOUT_SECONDS: float = 75.0' \
  'tests/test_review_v2120.py' \
  "the relay waits longer than the agent works"

echo "== the reaper prints everything again =="
mutate agent_api.py \
  'shown = ", ".join(reaped[:10])' \
  'shown = ", ".join(reaped)' \
  'tests/test_review_v2120.py' \
  "the reaper log stays readable"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
