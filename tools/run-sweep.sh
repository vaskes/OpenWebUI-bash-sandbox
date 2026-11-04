#!/usr/bin/env bash
# The mutation sweep runner: one implementation, used by CI and by hand.
#
# A mutation script breaks one property of the code on purpose and puts
# it back. Run against a tree that is not clean, the sweep can overwrite
# work that was never committed -- and the only sign of it is a diff
# that turns up later and belongs to nobody. So the sweep:
#
#   * starts only on a clean tree (a dirty tree is refused, not fixed);
#   * runs one script at a time, in the order it was given;
#   * checks the tree after every script: a leftover mutation is a
#     finding, and the runner does NOT clean it up -- it says what is
#     left and stops;
#   * refuses to start while another runner is live in the same tree;
#   * writes its logs outside the repository, so that logging can never
#     be the thing that makes the tree dirty.
#
# It never restores the tree by itself. `git checkout -- .` on a tree
# that may hold somebody's uncommitted work throws that work away, and a
# runner that does it silently is worse than no runner at all.
#
# Usage:
#   bash tools/run-sweep.sh tools/mutation-check-*.sh
#
# Exit codes:
#   0  every script caught its mutation, and the tree was clean after it
#   1  a script did not hold, or it left the tree dirty
#   2  usage error (not a repository, no scripts, no flock)
#   3  the tree was dirty before the sweep started
#   4  another runner is live in this tree
set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ]; then
    echo "not a git repository -- refusing to sweep" >&2
    exit 2
fi
cd "$REPO_ROOT"

command -v flock >/dev/null 2>&1 || {
    echo "flock is required -- refusing to sweep without a lock" >&2
    exit 2
}

# The lock lives in the git directory, not in the worktree: a lock file
# in the tree would be the runner dirtying the tree it is checking.
GIT_DIR="$(git rev-parse --git-dir)"
exec 9>"$GIT_DIR/owbs-sweep.lock"
if ! flock -n 9; then
    echo "another sweep runner is live in this tree -- refusing to sweep" >&2
    exit 4
fi

dirty="$(git status --porcelain)"
if [ -n "$dirty" ]; then
    echo "the tree is dirty -- refusing to sweep" >&2
    echo "a mutation sweep breaks the tree on purpose and puts it back," >&2
    echo "so it must run on a tree that holds nothing but committed work." >&2
    echo "Commit the work, or move it out of the tree, and run again." >&2
    echo "$dirty" >&2
    exit 3
fi

LOG_DIR="${OWBS_SWEEP_LOG_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/owbs-sweep-XXXXXX")}"
mkdir -p "$LOG_DIR"

# The arguments are file names or globs; a glob is expanded here, so
# that the caller can hand the runner the family and let the runner
# decide which of them there is.
scripts=()
for pattern in "$@"; do
    [ -n "$pattern" ] || continue
    case "$pattern" in
        *'*'*|*'?'*|*'['*)
            while IFS= read -r match; do
                [ -n "$match" ] && scripts+=("$match")
            done < <(compgen -G "$pattern" || true)
            ;;
        *)
            scripts+=("$pattern")
            ;;
    esac
done

if [ "${#scripts[@]}" -eq 0 ]; then
    echo "no mutation scripts to run" >&2
    exit 2
fi

status=0
for script in "${scripts[@]}"; do
    name="$(basename "$script")"
    log="$LOG_DIR/$name.log"
    if [ -n "${GITHUB_ACTIONS:-}" ]; then
        echo "::group::$script"
    else
        echo "== $script"
    fi
    # The script finds its own interpreter (python3, then python), and
    # PY overrides that when the caller set it. The runner does not
    # choose: a runner that pinned an interpreter would sweep against a
    # python that has no pytest, and the script would say so and stop.
    bash "$script" 2>&1 | tee "$log"
    rc=${PIPESTATUS[0]}
    if [ -n "${GITHUB_ACTIONS:-}" ]; then
        echo "::endgroup::"
    fi
    if [ "$rc" -ne 0 ]; then
        if [ -n "${GITHUB_ACTIONS:-}" ]; then
            echo "::error::$script did not hold -- a tripwire that cannot be made red is decoration"
        fi
        echo "== $script FAILED (exit $rc) -- the sweep stops here" >&2
        status=$rc
        break
    fi
    leftover="$(git status --porcelain)"
    if [ -n "$leftover" ]; then
        if [ -n "${GITHUB_ACTIONS:-}" ]; then
            echo "::error::$script left the tree dirty -- the next script would measure a tree nobody wrote"
        fi
        echo "== $script left the tree dirty -- the next script would measure a tree nobody wrote" >&2
        echo "$leftover" >&2
        git diff --stat >&2 || true
        echo "the runner does not clean this up: a leftover mutation is a finding," >&2
        echo "and restoring the tree here would throw away work that was never" >&2
        echo "committed. Read the diff, then clean it yourself." >&2
        status=1
        break
    fi
done
wait
exit "$status"
