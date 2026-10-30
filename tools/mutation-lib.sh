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

# The tripwire has to be GREEN before anything is broken.
#
# A mutation script that runs against a red tree learns nothing: every
# mutation "goes red", and the script reports catches for a tree that
# was already broken. That is not a rare accident — a stage parser that
# does not recognise the runtime stage of a Dockerfile is enough, and
# the verdicts look perfectly fine while measuring nothing.
#
# So the script says it out loud and stops, instead of printing a green
# line for a mutation that proved nothing.
mutation_require_green() {
    # mutation_require_green <selector>
    local selector="$1" out rc=0
    out="$(mktemp -p /tmp mut-green.XXXXXX)"
    "$PY" -m pytest "$selector" -q >"$out" 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "INVALID — the tripwire is not green on the clean tree (pytest exited $rc)."
        echo "         Every mutation below would 'go red' for a reason that has"
        echo "         nothing to do with the mutation. Fix the tree first."
        tail -30 "$out"
        rm -f "$out"
        exit 2
    fi
    rm -f "$out"
    echo "preflight: the tripwire is green on the clean tree"
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

# Replace **every** occurrence of $2 with $3 in $1. Use it when the fact
# under test lives in more than one place: breaking one copy leaves the
# other intact, the property survives, and the mutation reports a verdict
# about a tree that nobody broke. Fails when the pattern is gone.
mutation_apply_all() {
    "$PY" - "$1" "$2" "$3" <<'PY'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8").read()
count = text.count(old)
if not count:
    sys.exit(f"PATTERN NOT FOUND in {path}: {old!r}")
open(path, "w", encoding="utf-8").write(text.replace(old, new))
print(count)
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
#
# pytest's exit code says this exactly, and the output text does not:
#
#   0  the tests ran and passed
#   1  the tests ran and at least one failed  <- a catch
#   2  interrupted (a collection error)
#   3  an internal error
#   4  usage error (the selector names no file)
#   5  no tests were collected
#
# The old check grepped the whole output for "no tests ran" and friends.
# That is wrong in a way the output itself demonstrates: a failing test
# prints the assertion's message, and that message may quote a file that
# contains the phrase — the library quotes itself, and the mutation that
# renamed a function made the test print the library's own source, with
# "ERROR: not found" inside it. The grep then said "nothing was
# collected" about a run that had three real failures, and the script
# called a caught mutation INVALID.
#
# The exit code is not quoted by anybody. Use it.
mutation_run_was_invalid() {
    # mutation_run_was_invalid <exit-code>
    # Only 0 (everything passed) and 1 (something failed) mean that a
    # test ran. Every other code means it did not, and a mutation whose
    # test never ran is not a catch.
    [ "$1" -ne 0 ] && [ "$1" -ne 1 ]
}

# Run the test under the mutation, then say what the run means:
#   0 -> the test stayed green (the mutation escaped)
#   1 -> the test went red (the mutation was caught)
#   2 -> the test did not run (INVALID — this is not a catch)
mutation_run_pytest() {
    # mutation_run_pytest <selector> <out-file> <label>
    local selector="$1" out="$2" label="$3" rc=0
    "$PY" -m pytest "$selector" -q >"$out" 2>&1 || rc=$?
    if mutation_run_was_invalid "$rc"; then
        echo "INVALID $label — the test did not run (pytest exited $rc)"
        FAIL=$((FAIL+1))
        return 2
    fi
    if [ "$rc" -eq 1 ]; then
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

# The same shape, but every occurrence of the pattern is replaced. Use it
# when the fact under test lives in more than one place and the mutation
# has to destroy all of them to mean anything.
mutate_all() {
    # mutate_all <file> <old> <new> <test-selector> <label>
    local file="$1" old="$2" new="$3" selector="$4" label="$5"
    local backup
    backup="$(mktemp -p /tmp mut.XXXXXX)"
    cp "$file" "$backup"
    MUT_BACKUP="$backup"
    MUT_TARGET="$file"
    if ! mutation_apply_all "$file" "$old" "$new" >/dev/null; then
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
