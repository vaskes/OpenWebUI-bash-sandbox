#!/usr/bin/env bash
# Mutation check for the wave that turns the template's knobs on.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties this wave pins:
#
#   * the template's inbound token is ACTIVE (a commented-out knob is a
#     default-off knob) and carries a PLACEHOLDER (a published token is
#     a token every deployment that copies the template shares);
#   * the template's workspace ceiling is ACTIVE — a ceiling nobody
#     declared is a ceiling nobody checks;
#   * the template says how to fill the placeholder;
#   * the document says what the separation buys, what a forged agent
#     code would run with, and that `.env` is not a shell script;
#   * the number of scripts the CI comment names is the number of
#     scripts on disk.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

TESTS=tests/test_env_defaults_v2290.py

# The tree has to be green before anything is broken: a mutation sweep
# against a red tree reports catches for a tree nobody broke.
mutation_require_green "$TESTS"

echo "== the inbound token is commented out again =="
mutate .env.example \
  '
AGENT_INBOUND_TOKEN=CHANGE_ME_openssl_rand_hex_32' \
  '
# AGENT_INBOUND_TOKEN=CHANGE_ME_openssl_rand_hex_32' \
  "$TESTS::TestTheTemplateTurnsTheKnobsOn::test_the_inbound_token_is_not_commented_out" \
  "the template leaves the operator's public token in the agent's container"

echo "== the template carries a real token =="
# The value is a word, not a hex string: a secret scanner reads a
# 64-character hex literal as a credential, and a mutation script is
# not the place to publish one. The tripwire only asks whether the
# value is a placeholder, so any value at all breaks it.
mutate .env.example \
  'AGENT_INBOUND_TOKEN=CHANGE_ME_openssl_rand_hex_32' \
  'AGENT_INBOUND_TOKEN=example-published-token-not-a-real-secret' \
  "$TESTS::TestTheTemplateTurnsTheKnobsOn::test_the_inbound_token_is_a_placeholder_and_not_a_value" \
  "the template publishes a token every deployment would share"

echo "== the ceiling is commented out again =="
mutate .env.example \
  '
AGENT_WORKSPACE_SIZE=1g' \
  '
# AGENT_WORKSPACE_SIZE=1g' \
  "$TESTS::TestTheTemplateTurnsTheKnobsOn::test_the_ceiling_is_not_commented_out" \
  "the template declares no ceiling, so the agent's check never runs"

echo "== the recipe is gone =="
# Every occurrence: the recipe is written down more than once, and a
# mutation that changes only the first one leaves the others.
mutate_all .env.example \
  'openssl rand -hex 32' \
  'openssl rand -hex 16' \
  "$TESTS::TestTheTemplateTurnsTheKnobsOn::test_the_template_says_how_to_fill_the_placeholder" \
  "the template does not say how to generate the token"

echo "== the document stops naming what the separation buys =="
# The apostrophe needs double quotes: a single quote inside a
# single-quoted word ends the word.
mutate docs/CONFIGURATION.md \
  "the operator's public token is not in that container at all" \
  "the operator's public token is in that container" \
  "$TESTS::TestTheDocumentNamesWhatTheAbsenceCosts::test_the_cost_of_no_separation_is_written_down" \
  "the document does not say what the separation buys"

echo "== the document stops naming the forged code case =="
mutate docs/CONFIGURATION.md \
  'forged agent code would run with' \
  'agent code would run with' \
  "$TESTS::TestTheDocumentNamesWhatTheAbsenceCosts::test_the_forged_code_case_is_written_down" \
  "the document does not say what a forged agent code would run with"

echo "== the document stops saying that .env is not a shell script =="
mutate docs/CONFIGURATION.md \
  'is not a shell script' \
  'is a shell script' \
  "$TESTS::TestTheDocumentNamesWhatTheAbsenceCosts::test_the_env_file_is_not_a_shell_script_is_written_down" \
  "the document says the recipe is expanded in .env"

echo "== the CI comment names a stale count =="
mutate .github/workflows/ci.yml \
  '# The sweep is 33 scripts' \
  '# The sweep is 26 scripts' \
  "$TESTS::TestTheSweepCountInTheCommentIsTrue::test_the_comment_names_the_number_of_scripts_on_disk" \
  "the CI comment names a number of scripts that is not the real one"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
