#!/usr/bin/env bash
# Mutation check for the v2.14.5 tripwires.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties:
#
#   * the suite runs a tmux server of its own, and does not touch the
#     sessions of another server;
#   * the HTTP file door's answer describes the bytes it serves;
#   * a dead session comes back in the directory it had;
#   * a bad command reaches the model as words, not as a traceback;
#   * the MCP tool opens the session only when it has to.
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
# source's mtime AND its size, so two mutations of the same length in
# the same second leave the second one invisible.
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

RULE="tests/test_suite_hygiene_v2145.py"

echo "== the suite stops using a tmux server of its own =="
mutate tests/conftest.py \
    '    os.environ["TMUX_TMPDIR"] = str(private)' \
    '    pass  # the suite uses the machine'"'"'s default server' \
    "$RULE::TestTheSuiteHasATmuxServerOfItsOwn::test_a_session_of_another_server_is_not_ours" \
    "another server's session is not the suite's"

RULE2="tests/test_file_door_v2145.py"

echo "== the size is taken from the path again =="
# The old shape: the path is measured *before* the file is opened, and
# the bytes come from the descriptor.  Anything that changes the file in
# between leaves the answer promising N bytes and the body carrying M.
mutate agent_api.py \
    '    handle = _open_for_download(target)
    try:
        size = os.fstat(handle.fileno()).st_size' \
    '    size = os.stat(target).st_size
    handle = _open_for_download(target)
    try:
        pass' \
    "$RULE2::TestTheSizeDescribesTheBytes::test_the_size_is_taken_from_the_descriptor" \
    "the size describes the bytes that are served"

RULE3="tests/test_session_cwd_v2145.py"
RULE4="tests/test_mcp_session_cost_v2145.py"

echo "== a dead session comes back in the root again =="
mutate agent_api.py \
    '    cwd = requested or _persisted_cwd(sid) or WORKSPACE_REAL' \
    '    cwd = requested or WORKSPACE_REAL' \
    "$RULE3::TestTheSessionComesBackWhereItWas::test_a_dead_session_comes_back_in_its_directory" \
    "a known session comes back in the directory it had"

echo "== the bad command reaches the model as a framework error again =="
mutate mcp_server.py \
    '    if not command:' \
    '    if False:' \
    "tests/test_review_kwen_v2141.py::TestTheCommandIsRefusedLegibly::test_the_refusal_does_not_mention_an_internal_class" \
    "a bad command reaches the model as words"

echo "== a dead operator is back in the tree =="
mutate agent_api.py \
    '        raise HTTPException(status_code=404, detail=f"file not found: {candidate}") from exc' \
    '        raise HTTPException(status_code=404, detail=f"file not found: {candidate}") from exc
        raise HTTPException(status_code=400, detail="unresolvable path") from exc' \
    "tests/test_tree_hygiene_v2145.py::TestTheTreeHasNoUnreachableOperators::test_no_operator_follows_a_return_or_a_raise" \
    "the tree has no unreachable operators"

echo "== the CI forgets to run the suite without tmux =="
mutate .github/workflows/ci.yml \
    '  suite-without-tmux:' \
    '  suite-tmux-removed:' \
    "tests/test_tree_hygiene_v2145.py::TestTheSuiteIsCheckedWithoutTmux::test_the_ci_runs_the_suite_without_tmux" \
    "the CI runs the suite without tmux"

echo "== the MCP tool asks tmux on every call again =="
mutate mcp_server.py \
    '    if cwd is not None or sid not in SESSIONS:' \
    '    if True:' \
    "$RULE4::TestTheToolAsksTmuxOnlyWhenItMust::test_a_known_session_is_not_opened_again" \
    "the tool opens the session only when it has to"

echo
echo "mutations: $PASS went red as expected, $FAIL did not"
[ "$FAIL" -eq 0 ] || exit 1
