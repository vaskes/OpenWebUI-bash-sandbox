#!/usr/bin/env bash
# Mutation check for the landing path — how a release reaches `main`.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The property:
#
#   * the CI gate runs on a branch push, and not only on a pull
#     request. The workflow's `push:` trigger used to listen to `main`
#     alone, which meant a branch push produced no check runs at all:
#     the required checks could only be collected by a pull request,
#     and opening one needs the GitHub API. The release was pushed,
#     tagged, deployed and verified — and could not be landed, because
#     the commits carrying it had no green runs to show. The gate is
#     the checks, not the pull request.
#
# This script is not version-scoped on purpose: the landing path is not
# a property of any one release. It is the door every release comes
# through, and it is the door that is easiest to close by accident.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

RULE="tests/test_branch_protection.py"

echo "== the gate stops running on a branch push =="
mutate .github/workflows/ci.yml \
    "    branches: ['**']" \
    "    branches: [main]" \
    "$RULE::TestTheGateCanRunOnABranch::test_the_gate_runs_on_every_branch_push" \
    "the gate runs on every branch push"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
