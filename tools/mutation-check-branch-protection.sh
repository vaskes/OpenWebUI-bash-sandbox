#!/usr/bin/env bash
# Mutation check for the branch-protection tripwires.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# What this wave is about: the list of required status checks lives in
# GitHub's settings, not in the repository. Measured on 2026-10-29, on a
# throwaway branch with `main`'s rules, a required context that CI never
# reports left every pull request at `mergeable_state: blocked` with
# HTTP 405 on the merge — and no way out of it from the repository side.
# The same trap waits for `main` the moment a CI job is renamed, and the
# repository would look healthy while every release was stuck.
#
# So the list now lives in the tree (docs/required-status-checks.txt) and
# a tripwire compares it with the job names the workflow really produces.
# These mutations break that comparison in the three ways it can break.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

RULE="tests/test_branch_protection.py"

echo "== the list names a check the workflow never reports =="
mutate docs/required-status-checks.txt \
  'Lint (ruff)
' \
  'Lint (ruff)
A Job That Was Renamed Last Year
' \
  "$RULE" \
  "the list names only checks the workflow reports"

echo "== a CI job is renamed and the list does not follow =="
mutate .github/workflows/ci.yml \
  'name: Lint (ruff)' \
  'name: Lint (ruff, updated)' \
  "$RULE" \
  "a renamed job is caught before it bricks the merge"

echo "== a CI job exists that nobody requires =="
mutate .github/workflows/ci.yml \
  '  lint:
    name: Lint (ruff)' \
  '  lint:
    name: Lint (ruff)
  extra:
    name: A brand new job' \
  "$RULE" \
  "every job the workflow produces is a required check"

echo "== the list stops naming a check the workflow produces =="
mutate docs/required-status-checks.txt \
  'Secret scan (gitleaks)
' \
  '' \
  "$RULE" \
  "a check the workflow produces is required"

echo "== the list is emptied =="
mutate docs/required-status-checks.txt \
  'Lint (ruff)' \
  '# (removed)' \
  "$RULE" \
  "the list is not empty"

echo "== the guide stops pointing at the canonical list =="
mutate_all CONTRIBUTING.md \
  'docs/required-status-checks.txt' \
  'docs/required-checks.txt' \
  "$RULE" \
  "the guide says where the required checks live"

echo "== the guide counts the checks in prose again =="
mutate CONTRIBUTING.md \
  'every check in [`docs/required-status-checks.txt`](docs/required-status-checks.txt) must be green before a merge' \
  'thirteen checks must be green before a merge' \
  "$RULE" \
  "the guide does not keep a copy of the count"

echo "== the canonical list is removed from the tree =="
mutate docs/required-status-checks.txt \
  'Lint (ruff)' \
  '# (removed)' \
  "$RULE" \
  "the list is in the tree"

echo "== the guide stops naming the list =="
mutate_all CONTRIBUTING.md \
  'docs/required-status-checks.txt' \
  'docs/required-checks.txt' \
  "tests/test_release_claims_v21410.py" \
  "the guide names where the required checks live"

echo "== the mutation library loses its all-copies helper =="
mutate tools/mutation-lib.sh \
  'mutation_apply_all() {' \
  'mutation_apply_once() {' \
  "tests/test_release_claims_v21410.py" \
  "the library can break every copy of a fact"

echo "== the shipped version stops being the release =="
mutate agent_core.py \
  '__version__ = "' \
  '__version__ = "0.0.0' \
  "tests/test_release_claims_v21410.py" \
  "the shipped version is the release the changelog describes"

echo "== the verdict goes back to grepping the output =="
mutate tools/mutation-lib.sh \
  'mutation_run_was_invalid() {' \
  'mutation_run_was_invalid() { grep -q "no tests ran" "$2";' \
  "tests/test_mutation_scripts_run_v2148.py" \
  "the verdict comes from the exit code, not from quoted text"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
