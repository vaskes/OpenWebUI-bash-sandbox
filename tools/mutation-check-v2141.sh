#!/usr/bin/env bash
# Mutation check for the v2.14.1 tripwires (the review's code claims).
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties:
#
#   * the file door serves a file inside the workspace even when a
#     directory of the path is swapped for a symlink in between;
#   * the size in the answer describes the bytes that were read;
#   * the token is compared in constant time;
#   * a timeout outside the range is refused in words the model can use;
#   * the MCP tool resolves the agent at call time;
#   * a test workspace does not outlive the test process;
#   * every mutation pattern in every mutation script still applies.
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
# same second leave the second one invisible. The check must see the code
# that is on disk, not the code that was compiled a minute ago.
export PYTHONDONTWRITEBYTECODE=1

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
    find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
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

mutate_py() {
    # mutate_py <file> <selector> <label> — the edit is a python block on stdin
    local file="$1" selector="$2" label="$3"
    local backup
    backup="$(mktemp -p /tmp mut.XXXXXX)"
    cp "$file" "$backup"
    python3 - "$file" <<'PY'
import sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
old = """    from agent_api import (
        _open_for_download,  # noqa: PLC0415
        _safe_file,  # noqa: PLC0415
    )"""
new = """    from agent_api import (
        _open_for_download,  # noqa: PLC0415
    )
    _safe_file = _MODULE_LEVEL_SAFE_FILE"""
if old not in text:
    sys.exit("PATTERN NOT FOUND: the lazy import")
text = text.replace(old, new, 1)
anchor = "from mcp.server.mcpserver.exceptions import ToolError"
if anchor not in text:
    sys.exit("PATTERN NOT FOUND: the import anchor")
text = text.replace(
    anchor,
    anchor + "\n\nfrom agent_api import _safe_file as _MODULE_LEVEL_SAFE_FILE",
    1,
)
open(path, "w", encoding="utf-8").write(text)
PY
    if [ $? -ne 0 ]; then
        echo "SKIP  $label (pattern not found — mutation did not apply)"
        cp "$backup" "$file"
        rm -f "$backup"
        FAIL=$((FAIL+1))
        return
    fi
    find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
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

DOOR="tests/test_file_door_containment_v2141.py"

echo "== the file is opened by path again =="
mutate agent_api.py \
    '    dir_fd = os.open(WORKSPACE_REAL, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = next_fd
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)' \
    '    return os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)' \
    "$DOOR::TestASwappedDirectoryIsNotFollowed::test_the_file_outside_the_workspace_is_not_served" \
    "a swapped directory is not served"

echo "== a directory of the path is followed =="
mutate agent_api.py \
    '            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)' \
    '            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY, dir_fd=dir_fd)' \
    "$DOOR::TestASwappedDirectoryIsNotFollowed::test_the_file_outside_the_workspace_is_not_served" \
    "a symlinked directory is not followed"

echo "== the last component is followed =="
mutate agent_api.py \
    '        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)' \
    '        return os.open(parts[-1], os.O_RDONLY, dir_fd=dir_fd)' \
    "$DOOR::TestASwappedDirectoryIsNotFollowed::test_a_swapped_file_is_not_followed_either" \
    "a symlinked file is not followed"

echo "== the workspace root is followed =="
mutate agent_api.py \
    '    dir_fd = os.open(WORKSPACE_REAL, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)' \
    '    dir_fd = os.open(WORKSPACE_REAL, os.O_RDONLY | os.O_DIRECTORY)' \
    "$DOOR::TestASwappedDirectoryIsNotFollowed::test_a_swapped_workspace_root_is_not_followed" \
    "a symlinked workspace root is not followed"

echo "== the size comes from the path again =="
mutate mcp_server.py \
    '        size = os.fstat(handle.fileno()).st_size' \
    '        size = size' \
    "$DOOR::TestTheSizeDescribesTheBytes::test_the_size_comes_from_the_descriptor" \
    "the size in the answer describes the bytes"

echo "== the token is compared with an ordinary operator =="
mutate agent_api.py \
    '    return _hmac.compare_digest(credentials, AGENT_TOKEN)' \
    '    return credentials == AGENT_TOKEN' \
    "tests/test_review_kwen_v2141.py::TestTheTokenIsComparedInConstantTime" \
    "the token is compared in constant time"

echo "== the timeout range is not checked =="
mutate mcp_server.py \
    '        if not 1 <= timeout <= MAX_TIMEOUT:' \
    '        if False:' \
    "tests/test_review_kwen_v2141.py::TestTheTimeoutIsRefusedLegibly" \
    "a timeout outside the range is refused"

echo "== the refusal does not name the range =="
mutate mcp_server.py \
    '            raise ToolError(
                f"the timeout must be between 1 and {MAX_TIMEOUT} seconds, got {timeout}"
            )' \
    '            raise ToolError(f"the timeout is wrong: {timeout}")' \
    "tests/test_review_kwen_v2141.py::TestTheTimeoutIsRefusedLegibly::test_a_timeout_above_the_range_is_refused" \
    "the refusal names the range"

echo "== the tool keeps a reference to the agent =="
mutate_py mcp_server.py \
    "tests/test_review_kwen_v2141.py::TestTheToolResolvesTheAgentAtCallTime::test_the_tool_uses_the_current_agent_module" \
    "the MCP tool resolves the agent at call time"

echo "== the test workspace survives the process =="
mutate tests/test_agent_api_endpoints.py \
    '    atexit.register(lambda: shutil.rmtree(tmpdir, ignore_errors=True))' \
    '    pass' \
    "tests/test_review_kwen_v2141.py::TestTheTestsDoNotLeaveTempDirs::test_the_workspace_does_not_survive_the_process" \
    "the test workspace does not survive the process"

echo "== the MCP harness leaves its workspace behind =="
mutate tests/test_mcp_server.py \
    '    atexit.register(lambda: shutil.rmtree(tmpdir, ignore_errors=True))' \
    '    pass' \
    "tests/test_review_kwen_v2141.py::TestTheTestsDoNotLeaveTempDirs::test_the_mcp_harness_cleans_up_too" \
    "the MCP harness cleans up too"

echo "== a mutation pattern goes stale =="
mutate tools/mutation-check-v2121.sh \
    '        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)' \
    '        return os.open(parts[-1], os.O_RDONLY, dir_fd=dir_fd)' \
    "tests/test_mutation_scripts_v2141.py::TestTheMutationScriptsStillApply::test_every_mutation_pattern_is_in_the_code" \
    "a stale mutation pattern is caught"

echo
echo "mutations: $PASS went red as expected, $FAIL did not"
[ "$FAIL" -eq 0 ] || exit 1
