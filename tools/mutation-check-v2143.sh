#!/usr/bin/env bash
# Mutation check for the v2.14.3 tripwires (the privilege warning).
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties:
#
#   * the privilege mode is announced when sudo is present;
#   * the setting that blocks the escalation is named;
#   * the hollow root (a read-only filesystem) is announced too;
#   * nothing is said when there is no sudo to escalate to;
#   * the warning is a plain function, not a context manager in costume;
#   * the start-up really calls the warning.
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
# same second leave the second one invisible.
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

RULE="tests/test_privilege_warning_v2143.py"

echo "== the warning is dressed as a context manager again =="
mutate agent_api.py \
    'def _warn_about_the_privilege_mode(' \
    '@asynccontextmanager
def _warn_about_the_privilege_mode(' \
    "$RULE::TestTheFunctionIsAPlainFunction::test_the_call_returns_nothing" \
    "the warning is a plain function"

echo "== the start-up stops announcing the mode =="
mutate agent_api.py \
    '    _warn_about_the_privilege_mode()' \
    '    pass  # the mode is not announced' \
    "$RULE::TestTheStartupAnnouncesIt::test_the_startup_calls_the_warning" \
    "the start-up announces the mode"

echo "== the warning loses its voice =="
mutate agent_api.py \
    '"the privilege mode is ON: a command in this sandbox can run "' \
    '"the privilege mode is enabled: a command in this sandbox can run "' \
    "$RULE::TestThePrivilegeWarning::test_the_mode_is_announced" \
    "the mode is announced"

echo "== the setting that blocks the escalation is not named =="
mutate agent_api.py \
    '    if verdict != "ok":' \
    '    if False:' \
    "$RULE::TestThePrivilegeWarning::test_the_setting_that_blocks_the_escalation_is_named" \
    "the blocking setting is named"

echo "== the hollow root is not announced =="
mutate agent_api.py \
    '    _warn_about_the_read_only_rootfs(mountinfo)' \
    '    pass  # the hollow root is not announced' \
    "$RULE::TestThePrivilegeWarning::test_the_hollow_root_is_announced_too" \
    "the hollow root is announced"

echo "== the warning is printed without sudo =="
mutate agent_api.py \
    '    if shutil.which("sudo") is None:
        return' \
    '    if False:
        return' \
    "$RULE::TestThePrivilegeWarning::test_nothing_is_said_without_sudo" \
    "nothing is said without sudo"

echo
echo "mutations: $PASS went red as expected, $FAIL did not"
[ "$FAIL" -eq 0 ] || exit 1
