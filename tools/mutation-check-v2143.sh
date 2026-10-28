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

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

PASS=0
FAIL=0

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

CI="tests/test_ci_runs_the_mutation_checks.py"

echo "== the mutation checks stop running in CI =="
mutate .github/workflows/ci.yml \
    'for script in tools/mutation-check-*.sh; do' \
    'for script in tools/mutation-check-none.sh; do' \
    "$CI::TestTheMutationChecksRunInCI::test_the_workflow_runs_the_whole_family" \
    "the mutation checks run in CI"

echo "== the privilege warning is no longer checked in the log =="
mutate .github/workflows/ci.yml \
    "grep -q 'the privilege mode is ON'" \
    "grep -q 'nothing at all'" \
    "$CI::TestThePrivilegeWarningIsCheckedLive::test_the_workflow_looks_for_the_warning_in_the_log" \
    "the privilege warning is checked in the log"

echo "== the make target is gone =="
mutate Makefile \
    'mutation: ## Run the mutation checks' \
    'mutations: ## Run the mutation checks' \
    "$CI::TestTheMutationChecksRunInCI::test_the_makefile_runs_them_too" \
    "there is a make target for the mutation checks"

echo "== the contribution guide stops mentioning them =="
mutate CONTRIBUTING.md \
    '## Mutation checks' \
    '## Something else entirely' \
    "$CI::TestTheMutationChecksRunInCI::test_contributing_says_how_to_run_them" \
    "the contribution guide says how to run them"

echo "== the contribution guide stops saying how =="
mutate CONTRIBUTING.md \
    'make mutation     # or:  for s in tools/mutation-check-*.sh; do bash "$s"; done' \
    'make something-else' \
    "$CI::TestTheMutationChecksRunInCI::test_contributing_says_how_to_run_them" \
    "the contribution guide says how to run them"

echo
echo "mutations: $PASS went red as expected, $FAIL did not"
[ "$FAIL" -eq 0 ] || exit 1
