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

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

PASS=0
FAIL=0

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
# The pattern points at the message, not at the way the string is
# wrapped: v2.14.4 reworded the notice (it now names the way out), and
# a tripwire whose pattern rots is a tripwire that stops watching.
mutate agent_api.py \
    '"the command did not start: the session is busy running another command, "' \
    '"the command failed: the session is busy running another command, "' \
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
