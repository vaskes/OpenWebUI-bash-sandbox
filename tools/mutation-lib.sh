#!/usr/bin/env bash
# Shared helpers for the mutation checks.
#
# Why this file exists: a mutation check that cannot be made red is
# decoration, and one that lies is worse than no check at all. Two ways to
# lie were found before v2.14.8:
#
#   * No interpreter. The scripts ran pytest through a hardcoded path that
#     does not exist in CI. The shell answered 127 ("command not found"),
#     the `if pytest; then FAIL; else PASS' shape read that as "the test
#     went red", and the script printed a catch for a test that never
#     started. Twenty-one of 113 mutations never ran a test.
#   * A selector that matches no test. pytest exits 5 ("no tests ran"),
#     which is not a failure either, and the script counted it as a catch.
#
# The rules this file enforces:
#
#   * No interpreter with pytest => exit 2, loudly, before anything runs.
#   * A run that collected nothing => INVALID, never a catch.
#   * A catch is a test that ran and went red. Nothing else counts.

# The interpreter the scripts use. PY is honoured only when it can import
# pytest: a PY that cannot is a mistake worth shouting about, not a reason
# to fall back silently to some other interpreter.
mutation_require_python() {
    if [ -n "${PY:-}" ]; then
        if "$PY" -c "import pytest" >/dev/null 2>&1; then
            return 0
        fi
        echo "PY=$PY cannot import pytest — point PY at an interpreter that has pytest" >&2
        exit 2
    fi
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 \
            && "$candidate" -c "import pytest" >/dev/null 2>&1; then
            PY="$(command -v "$candidate")"
            return 0
        fi
    done
    echo "no python with pytest found — set PY=/path/to/python" >&2
    exit 2
}

# Replace the first occurrence of $2 with $3 in $1. Fails when the pattern
# is gone: a mutation that does not apply proves nothing.
mutation_apply() {
    "$PY" - "$1" "$2" "$3" <<'PY'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8").read()
if old not in text:
    sys.exit(f"PATTERN NOT FOUND in {path}: {old!r}")
open(path, "w", encoding="utf-8").write(text.replace(old, new, 1))
PY
}

# A stale bytecode cache can hide a mutation: Python keys a .pyc by the
# source's mtime AND its size, so two mutations of the same length in the
# same second leave the second one invisible. The check must see the code
# that is on disk, not the code that was compiled a minute ago.
export PYTHONDONTWRITEBYTECODE=1

mutation_purge_pycache() {
    find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
}

# Did the run collect anything at all?
mutation_collected_nothing() {
    grep -q "no tests ran\|no tests collected\|ERROR: not found\|ERROR collecting" "$1"
}

# Run the test under the mutation, then say what the run means:
#   0 -> the test stayed green (the mutation escaped)
#   1 -> the test went red (the mutation was caught)
#   2 -> the test did not run (INVALID — this is not a catch)
mutation_run_pytest() {
    # mutation_run_pytest <selector> <out-file> <label>
    local selector="$1" out="$2" label="$3" rc=0
    "$PY" -m pytest "$selector" -q >"$out" 2>&1 || rc=$?
    if [ "$rc" -eq 1 ] && ! mutation_collected_nothing "$out"; then
        echo "OK    $label — the test went red"
        PASS=$((PASS+1))
        return 0
    fi
    if [ "$rc" -eq 0 ]; then
        echo "FAIL  $label — the test stayed GREEN under the mutation"
        FAIL=$((FAIL+1))
        return 1
    fi
    echo "INVALID $label — the test did not run (pytest exited $rc)"
    FAIL=$((FAIL+1))
    return 2
}

# One output file per script: two scripts running at the same time must not
# share the same scratch file.
MUT_OUT="${MUT_OUT:-$(mktemp -p /tmp mut-out.XXXXXX)}"

# The counters the verdict updates. A script may set them before it
# sources this file; the defaults are here so that a script that does
# not is not a script that dies with "unbound variable".
PASS="${PASS:-0}"
FAIL="${FAIL:-0}"

# A script that is killed between the mutation and the restore would
# leave the mutation in the tree — and the next run would measure a tree
# that nobody wrote. The trap puts it back.
MUT_BACKUP=""
MUT_TARGET=""
trap 'if [ -n "${MUT_BACKUP:-}" ] && [ -n "${MUT_TARGET:-}" ]; then
    cp "$MUT_BACKUP" "$MUT_TARGET" 2>/dev/null
    rm -f "$MUT_BACKUP" 2>/dev/null
fi' INT TERM HUP

# The mutation itself: back the file up, break it, run the test, put the
# file back. Every script uses this one shape.
mutate() {
    # mutate <file> <old> <new> <test-selector> <label>
    local file="$1" old="$2" new="$3" selector="$4" label="$5"
    local backup
    backup="$(mktemp -p /tmp mut.XXXXXX)"
    cp "$file" "$backup"
    MUT_BACKUP="$backup"
    MUT_TARGET="$file"
    if ! mutation_apply "$file" "$old" "$new"; then
        echo "SKIP  $label (pattern not found — mutation did not apply)"
        cp "$backup" "$file"
        rm -f "$backup"
        FAIL=$((FAIL+1))
        return
    fi
    mutation_purge_pycache
    mutation_run_pytest "$selector" "$MUT_OUT" "$label"
    cp "$backup" "$file"
    rm -f "$backup"
    MUT_BACKUP=""
    MUT_TARGET=""
}
