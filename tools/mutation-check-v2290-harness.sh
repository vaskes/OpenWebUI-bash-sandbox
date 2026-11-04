#!/usr/bin/env bash
# Mutation check for the mutation harness itself.
#
# A check that cannot be made red is a decoration. The harness decides
# what a sweep MEANS, so a harness that cannot tell a skipped test from
# a green one turns every environment problem into a false "the test
# stayed green" — the property looks unpinned when in fact the test
# never ran.
#
# The properties this wave pins:
#
#   * the preflight reads pytest's summary: a tripwire that was SKIPPED
#     is INVALID, and the sweep stops instead of counting catches;
#   * the verdict of a mutation says the test was skipped.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

TESTS=tests/test_mutation_scripts_skip_v2290.py

# The tree has to be green before anything is broken: a mutation sweep
# against a red tree reports catches for a tree nobody broke.
mutation_require_green "$TESTS"

echo "== the preflight stops looking at the summary =="
mutate tools/mutation-lib.sh \
  '    if mutation_run_skipped "$out"; then
        echo "INVALID — the tripwire did not RUN' \
  '    if false; then
        echo "INVALID — the tripwire did not RUN' \
  "$TESTS::TestASkippedTestIsNotAGreenTest::test_the_preflight_refuses_a_tripwire_that_skipped" \
  "the preflight calls a skipped tripwire green"

echo "== the verdict stops saying that the test was skipped =="
mutate tools/mutation-lib.sh \
  'INVALID $label — the test was SKIPPED, not green: it did not run' \
  'INVALID $label — the test did not run' \
  "$TESTS::TestASkippedTestIsNotAGreenTest::test_a_skipped_test_is_never_a_catch" \
  "the verdict no longer says the test was skipped"

echo "== the harness stops reading the summary at all =="
mutate tools/mutation-lib.sh \
  'mutation_run_skipped() {' \
  'mutation_run_skipped_disabled() {' \
  "$TESTS::TestASkippedTestIsNotAGreenTest::test_the_harness_names_the_skip_outright" \
  "the harness does not look at pytest's summary at all"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
