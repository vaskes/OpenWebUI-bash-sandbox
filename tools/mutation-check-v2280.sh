#!/usr/bin/env bash
# Mutation check for the wave that takes the agent's own code out of the
# model's hands.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties this wave pins:
#
#   * the agent's modules are not copied into the image owned by uid
#     1000 — the uid the model's own shell runs as;
#   * /home/agent is owned by root and is not writable by group or by
#     others;
#   * the image does not try to write .pyc files into a directory the
#     service may not write;
#   * the model's own scratch space (its pip cache, its workspace) is
#     still the model's — the fix must not take that with it;
#   * docs/SECURITY.md names the rule the image implements.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

TESTS=tests/test_image_ownership_v2280.py

# The tree has to be green before anything is broken: a mutation sweep
# against a red tree reports catches for a tree nobody broke.
mutation_require_green "$TESTS"

echo "== the agent's directory belongs to root =="
mutate Dockerfile.sandbox \
  'chown root:root /home/agent' \
  'chown 1000:1000 /home/agent' \
  "$TESTS::TestTheAgentsCodeIsNotTheModelsToWrite::test_the_agents_directory_is_owned_by_root" \
  "the agent's directory is owned by the model's uid"

echo "== the agent's code belongs to root =="
mutate Dockerfile.sandbox \
  'COPY agent_api.py   /home/agent/agent_api.py' \
  'COPY --chown=1000:1000 agent_api.py   /home/agent/agent_api.py' \
  "$TESTS::TestTheAgentsCodeIsNotTheModelsToWrite::test_no_module_is_copied_as_the_models_uid" \
  "the agent's code is copied into the image as the model's uid"

echo "== the agent's directory is not writable by others =="
mutate Dockerfile.sandbox \
  'chmod 755 /home/agent' \
  'chmod 777 /home/agent' \
  "$TESTS::TestTheAgentsCodeIsNotTheModelsToWrite::test_the_agents_directory_is_not_writable_by_others" \
  "the agent's directory is writable by group and by others"

echo "== the image writes no bytecode into the agent's directory =="
mutate Dockerfile.sandbox \
  '    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="/home/agent" \
' \
  '    PYTHONPATH="/home/agent" \
' \
  "$TESTS::TestTheAgentsCodeIsNotTheModelsToWrite::test_the_image_does_not_write_bytecode_into_the_agents_directory" \
  "the image tries to write .pyc files into /home/agent"

echo "== the model's own scratch space is still the model's =="
mutate Dockerfile.sandbox \
  'chown 1000:1000 /workspace /var/cache/apt /home/agent/.cache/pip' \
  'chown root:root /workspace /var/cache/apt /home/agent/.cache/pip' \
  "$TESTS::TestTheFixDidNotTakeAnythingAway::test_the_models_own_writable_places_survive" \
  "the model's pip cache and workspace are owned by root"

echo "== the document names the rule =="
mutate docs/SECURITY.md \
  'modules owned by root, the directory left unwritable by uid
1000.' \
  'modules owned by the model, the directory left writable by uid
1000.' \
  "$TESTS::TestTheDocsSayWhatTheImageDoes::test_the_rule_is_written_down" \
  "the document describes a rule the image does not implement"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
