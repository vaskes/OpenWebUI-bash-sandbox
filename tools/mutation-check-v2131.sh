#!/usr/bin/env bash
# Mutation check for the v2.13.1 tripwires (the busy-session leak).
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The bug: a command that never started — the shell was busy with an
# earlier command — answered with the pane, and the pane held the
# PREVIOUS command's output. The caller read somebody else's output as
# its own, and a model quoted it into the chat.
#
# The fix: the begin marker is the proof that the command ran. When it
# is missing and the scrollback did not overflow, the command never
# started: the stdout stays empty, the reason goes to stderr, and the
# pane is left alone.
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

BUSY="tests/test_busy_session_v2131.py::TestTheBusySessionLeak"

echo "== the busy session is not detected at all =="
mutate agent_api.py \
    '    limit = await _pane_history_limit(name)
    if limit <= 0:
        return False
    return await _history_size(name) >= limit' \
    '    return True' \
    "$BUSY::test_the_stdout_is_empty" \
    "a command that never started answers with an empty stdout"

echo "== the tiebreaker is inverted =="
mutate agent_api.py \
    'return await _history_size(name) >= limit' \
    'return await _history_size(name) < limit' \
    "$BUSY::test_the_stdout_is_empty" \
    "a scrollback that is not full is read as full"

echo "== the pane is handed over anyway =="
mutate agent_api.py \
    '                return {
                    "stdout": "",
                    "stderr": _BUSY_NOTICE,' \
    '                return {
                    "stdout": output,
                    "stderr": _BUSY_NOTICE,' \
    "$BUSY::test_the_previous_commands_output_is_not_the_stdout" \
    "the previous command's output stays out of the stdout"

echo "== the reason is dropped =="
mutate agent_api.py \
    '"stderr": _BUSY_NOTICE,' \
    '"stderr": "",' \
    "$BUSY::test_the_reason_is_named_in_stderr" \
    "the reason is named in stderr"

echo "== the reason says something else =="
mutate agent_api.py \
    '"the command did not start: the session is busy running another "' \
    '"the command failed: the session is busy running another "' \
    "$BUSY::test_the_reason_is_named_in_stderr" \
    "the reason says that the command did not start"

echo "== the whole scrollback is not consulted =="
mutate agent_api.py \
    '    _, out, _ = await _tmux("capture-pane", "-t", name, "-p", "-S", "-")
    return out' \
    '    _, out, _ = await _tmux("capture-pane", "-t", name, "-p", "-S", "-1")
    return out' \
    "tests/test_busy_session_v2131.py::TestTheMarkerInTheFullScrollback::test_the_output_is_taken_from_the_full_scrollback" \
    "the output is taken from the whole scrollback"

echo "== the output found in the scrollback is dropped =="
mutate agent_api.py \
    '                output = full_output
                head_survived = True' \
    '                output = ""
                head_survived = True' \
    "tests/test_busy_session_v2131.py::TestTheMarkerInTheFullScrollback::test_the_output_is_taken_from_the_full_scrollback" \
    "the output found in the scrollback is returned"

echo "== the echo of the queued command counts as a printed marker =="
mutate agent_api.py \
    'begin_hit = re.search(rf"(?m)^{re.escape(begin_marker)}\r?$", region)' \
    'begin_hit = re.search(rf"{re.escape(begin_marker)}", region)' \
    "tests/test_busy_session_v2131.py::TestTheEchoedMarkerIsNotAPrintedMarker::test_a_marker_inside_a_line_does_not_count_as_started" \
    "a marker inside a line is not a printed marker"

echo "== the lost-beginning notice is dropped =="
mutate agent_api.py \
    '        if not head_survived and output.strip():
            # The pane overflowed and the beginning of the output is' \
    '        if False:
            # The pane overflowed and the beginning of the output is' \
    "tests/test_busy_session_v2131.py::TestTheOverflowedScrollback::test_the_lost_beginning_notice_is_kept" \
    "an overflowed scrollback still says that the beginning was lost"

echo
echo "mutations: $PASS went red as expected, $FAIL did not"
[ "$FAIL" -eq 0 ] || exit 1
