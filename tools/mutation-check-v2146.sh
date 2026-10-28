#!/usr/bin/env bash
# Mutation check for the v2.14.6 tripwires.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties:
#
#   * the CI job that runs the suite without tmux really runs it
#     without tmux — the PATH is replaced, not prepended;
#   * the protocol's marker does not reach the caller's stdout;
#   * the file door checks the limit before it opens the file, and
#     closes the descriptor on every path that does not hand it to the
#     response.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

PASS=0
FAIL=0

RULE_CI="tests/test_ci_without_tmux_v2146.py"
RULE_MARKER="tests/test_stdout_marker_v2146.py"
RULE_ORDER="tests/test_file_door_order_v2146.py"

echo "== the job without tmux runs the suite with tmux again =="
mutate .github/workflows/ci.yml \
    '        run: PATH=/tmp/no-tmux pytest -q tests/' \
    '        run: PATH=/tmp/no-tmux:$PATH pytest -q tests/' \
    "$RULE_CI::TestTheJobReallyRunsWithoutTmux::test_tmux_is_not_findable_in_the_path_the_job_runs_with" \
    "the job runs the suite without tmux"

echo "== the job does not use the farm it built =="
mutate .github/workflows/ci.yml \
    '        run: PATH=/tmp/no-tmux pytest -q tests/' \
    '        run: pytest -q tests/' \
    "$RULE_CI::TestTheJobReallyRunsWithoutTmux::test_tmux_is_not_findable_in_the_path_the_job_runs_with" \
    "the job runs the suite with the PATH it built"

echo "== the marker reaches the caller again =="
mutate agent_api.py \
    '        if line.startswith(CONTINUATION_PROMPT):' \
    '        if False:' \
    "$RULE_MARKER::TestTheMarkerDoesNotReachTheCaller::test_a_multi_line_command_answers_with_its_output_only" \
    "the protocol's marker does not reach the caller"

echo "== the limit is checked after the file is opened again =="
mutate agent_api.py \
    '    policy_size = _path_size(target)' \
    '    policy_size = None' \
    "$RULE_ORDER::TestTheFileDoorChecksTheLimitFirst::test_the_limit_is_checked_before_the_file_is_opened" \
    "the limit is checked before the file is opened"

echo "== the descriptor is left open on the 416 path again =="
mutate agent_api.py \
    '    except Exception:
        # An unsatisfiable range is a 416, and the descriptor opened' \
    '    except ZeroDivisionError:
        # An unsatisfiable range is a 416, and the descriptor opened' \
    "$RULE_ORDER::TestTheFileDoorChecksTheLimitFirst::test_the_descriptor_is_closed_on_the_416_path" \
    "the descriptor is closed on the 416 path"

echo
echo "mutations: $PASS went red as expected, $FAIL did not"
[ "$FAIL" -eq 0 ] || exit 1
