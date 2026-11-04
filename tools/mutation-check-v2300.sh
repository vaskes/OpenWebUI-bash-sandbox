#!/usr/bin/env bash
# Mutation check for the wave that closes the review's leftovers.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties this wave pins:
#
#   * the newest section of the release notes names the size of the
#     sweep, and the number it names is the number of scripts on disk;
#   * the product profile names ONE ceiling (the template, the Makefile,
#     the setup script and the documentation agree), the CI profile
#     names its own, and the product compose files declare no ceiling
#     of their own;
#   * the rule that the declared ceiling must equal the filesystem that
#     was made is written down;
#   * the sweep runner lives in the repository and keeps its promises:
#     a dirty tree is refused, a leftover mutation fails the sweep, the
#     tree is not restored behind the operator's back, the scripts run
#     one at a time, and a second runner is refused.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

TESTS=tests/test_release_hygiene_v2300.py

# The tree has to be green before anything is broken: a mutation sweep
# against a red tree reports catches for a tree nobody broke.
mutation_require_green "$TESTS"

echo "== the release notes name a sweep that is not the sweep =="
mutate CHANGELOG.md \
  'The sweep is 34' \
  'The sweep is 26' \
  "$TESTS::TestTheReleaseNotesCountTheSweep::test_the_number_it_names_is_the_number_on_disk" \
  "the release notes name a number of scripts that is not the real one"

echo "== the template declares a ceiling of its own =="
mutate .env.example \
  'AGENT_WORKSPACE_SIZE=4g' \
  'AGENT_WORKSPACE_SIZE=1g' \
  "$TESTS::TestTheCeilingIsOneProfile::test_the_product_profile_names_one_size" \
  "the template declares a ceiling that the rest of the profile does not"

echo "== the Makefile declares a ceiling of its own =="
mutate Makefile \
  'WORKSPACE_SIZE ?= 4g' \
  'WORKSPACE_SIZE ?= 3g' \
  "$TESTS::TestTheCeilingIsOneProfile::test_the_product_profile_names_one_size" \
  "the Makefile declares a ceiling that the rest of the profile does not"

echo "== the deploy rule is not written down =="
mutate docs/CONFIGURATION.md \
  'The declared ceiling must equal the filesystem that was made.' \
  'The declared ceiling may differ from the filesystem that was made.' \
  "$TESTS::TestTheCeilingIsOneProfile::test_the_deploy_rule_is_written_down" \
  "the documentation does not say that the declared ceiling must equal the filesystem"

echo "== the product compose carries a literal ceiling =="
mutate docker-compose.yml \
  'AGENT_WORKSPACE_SIZE=${AGENT_WORKSPACE_SIZE:-}' \
  'AGENT_WORKSPACE_SIZE=${AGENT_WORKSPACE_SIZE:-4g}' \
  "$TESTS::TestTheCeilingIsOneProfile::test_the_product_compose_declares_no_ceiling" \
  "the product compose declares a ceiling of its own"

echo "== the CI profile stops being its own =="
mutate docker-compose.ci.yml \
  'AGENT_WORKSPACE_SIZE=${AGENT_WORKSPACE_SIZE:-1g}' \
  'AGENT_WORKSPACE_SIZE=${AGENT_WORKSPACE_SIZE:-4g}' \
  "$TESTS::TestTheCeilingIsOneProfile::test_the_ci_profile_names_its_own_size" \
  "the CI profile names a ceiling that is not the one the CI declares"

echo "== the runner sweeps a dirty tree =="
mutate tools/run-sweep.sh \
  '    exit 3' \
  '    exit 0' \
  "$TESTS::TestTheSweepRunnerLivesInTheRepo::test_a_dirty_tree_is_refused" \
  "the runner sweeps a tree that holds uncommitted work"

echo "== the runner logs into the tree it is checking =="
mutate tools/run-sweep.sh \
  '    "$repo_abs"/*)' \
  '    "$repo_abs"/never)' \
  "$TESTS::TestTheSweepRunnerLivesInTheRepo::test_the_runner_refuses_to_log_into_the_tree" \
  "the runner writes its logs into the repository it is checking"

echo "== the runner ignores a leftover mutation =="
mutate tools/run-sweep.sh \
  '        status=1' \
  '        status=0' \
  "$TESTS::TestTheSweepRunnerLivesInTheRepo::test_a_leftover_mutation_fails_the_sweep" \
  "a script that left the tree dirty does not fail the sweep"

echo "== the runner restores the tree behind the operator's back =="
mutate tools/run-sweep.sh \
  '        echo "$leftover" >&2' \
  '        echo "$leftover" >&2
        git checkout -- .' \
  "$TESTS::TestTheSweepRunnerLivesInTheRepo::test_the_runner_does_not_restore_the_tree" \
  "the runner restores the tree, and restoring it throws away work that was never committed"

echo "== the runner runs the scripts at the same time =="
mutate tools/run-sweep.sh \
  '    bash "$script" 2>&1 | tee "$log"' \
  '    bash "$script" 2>&1 | tee "$log" &' \
  "$TESTS::TestTheSweepRunnerLivesInTheRepo::test_the_scripts_run_one_at_a_time" \
  "the runner runs the mutation scripts at the same time"

echo "== the runner lets a second runner in =="
mutate tools/run-sweep.sh \
  '    exit 4' \
  '    exit 0' \
  "$TESTS::TestTheSweepRunnerLivesInTheRepo::test_a_second_runner_is_refused" \
  "a second runner in the same tree is not refused"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
