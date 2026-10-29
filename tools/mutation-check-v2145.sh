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

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

PASS=0
FAIL=0

RULE="tests/test_suite_hygiene_v2145.py"

echo "== the suite stops using a tmux server of its own =="
# The rule lives in tests/_tmux_isolation.py, and it is set in two
# places there (the first call and the re-assert), so BOTH have to go:
# breaking one leaves the other, the suite keeps its private server, and
# the mutation would report a verdict about a tree nobody broke.
mutate_all tests/_tmux_isolation.py \
    'os.environ["TMUX_TMPDIR"] = private' \
    'pass  # the suite uses the machine'"'"'s default server' \
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

echo "== the CI forgets to walk the stdlib path =="
mutate .github/workflows/ci.yml \
    '        run: python -m unittest discover -s tests' \
    '        run: echo "the stdlib path is not walked"' \
    "tests/test_tree_hygiene_v2145.py::TestTheStdlibPathIsWalked::test_the_ci_runs_the_suite_on_the_stdlib" \
    "the CI walks the stdlib path"

echo "== the MCP tool asks tmux on every call again =="
mutate mcp_server.py \
    '    if cwd is not None or sid not in SESSIONS:' \
    '    if True:' \
    "$RULE4::TestTheToolAsksTmuxOnlyWhenItMust::test_a_known_session_is_not_opened_again" \
    "the tool opens the session only when it has to"

echo
echo "mutations: $PASS went red as expected, $FAIL did not"
[ "$FAIL" -eq 0 ] || exit 1
