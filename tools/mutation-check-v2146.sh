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
