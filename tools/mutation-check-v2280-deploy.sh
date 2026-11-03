#!/usr/bin/env bash
# Mutation check for the wave that makes the deploy follow the order.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties this wave pins:
#
#   * the order is remove, mount, start — the mount has to be made
#     after the container is removed (the removal takes the mount away)
#     and before the stack is started (the bind is resolved when the
#     container is created);
#   * the script does not guess the ceiling: the path and the size come
#     from the deployment;
#   * `make restart` goes through the order instead of recreating the
#     stack in one command;
#   * the paths that have a ceiling declare it, so the agent checks the
#     filesystem it is looking at;
#   * the document states the order.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

TESTS=tests/test_deploy_order_v2280.py

# The tree has to be green before anything is broken: a mutation sweep
# against a red tree reports catches for a tree nobody broke.
mutation_require_green "$TESTS"

echo "== the mount is made after the stack is started =="
mutate tools/recreate.sh \
  'sudo bash tools/setup-workspace.sh "$WORKSPACE_PATH" "$WORKSPACE_SIZE"

# 3. Start on the filesystem that has the ceiling.
if [ "$#" -eq 0 ]; then
    docker compose -f "$COMPOSE_FILE" up -d --build
else
    docker compose -f "$COMPOSE_FILE" up -d --build "$@"
fi' \
  '# 3. Start on the filesystem that has the ceiling.
if [ "$#" -eq 0 ]; then
    docker compose -f "$COMPOSE_FILE" up -d --build
else
    docker compose -f "$COMPOSE_FILE" up -d --build "$@"
fi
sudo bash tools/setup-workspace.sh "$WORKSPACE_PATH" "$WORKSPACE_SIZE"' \
  "$TESTS::TestTheOrderIsRemoveMountStart::test_the_order_is_remove_mount_start" \
  "the mount is made after the stack has started"

echo "== the script guesses the workspace path =="
mutate tools/recreate.sh \
  '${AGENT_WORKSPACE_PATH:?AGENT_WORKSPACE_PATH must be set — the ceiling belongs to the deployment, not to this script}"' \
  '${AGENT_WORKSPACE_PATH:-/opt/owbs-workspace}"' \
  "$TESTS::TestTheOrderIsRemoveMountStart::test_the_script_does_not_guess_the_ceiling" \
  "the script falls back to a default workspace path"

echo "== the script guesses the ceiling =="
mutate tools/recreate.sh \
  '${AGENT_WORKSPACE_SIZE:?AGENT_WORKSPACE_SIZE must be set — a workspace without a ceiling is not a workspace}"' \
  '${AGENT_WORKSPACE_SIZE:-4g}"' \
  "$TESTS::TestTheOrderIsRemoveMountStart::test_the_script_does_not_guess_the_ceiling" \
  "the script falls back to a default ceiling"

echo "== make restart recreates the stack in one command =="
mutate Makefile \
  'AGENT_WORKSPACE_PATH="$(WORKSPACE_PATH)" AGENT_WORKSPACE_SIZE="$(WORKSPACE_SIZE)" bash tools/recreate.sh docker-compose.yml' \
  'AGENT_WORKSPACE_PATH="$(WORKSPACE_PATH)" $(COMPOSE) up -d --build --force-recreate' \
  "$TESTS::TestTheMakefileFollowsTheRule::test_restart_goes_through_the_order" \
  "make restart recreates the containers in one command"

echo "== make up stops declaring the ceiling =="
mutate Makefile \
  'AGENT_WORKSPACE_PATH="$(WORKSPACE_PATH)" AGENT_WORKSPACE_SIZE="$(WORKSPACE_SIZE)" $(COMPOSE) up -d --build' \
  'AGENT_WORKSPACE_PATH="$(WORKSPACE_PATH)" $(COMPOSE) up -d --build' \
  "$TESTS::TestTheMakefileFollowsTheRule::test_up_declares_the_ceiling" \
  "make up starts the stack without declaring the ceiling"

echo "== the CI recreates the agent in one command =="
mutate .github/workflows/ci.yml \
  'bash tools/recreate.sh docker-compose.ci.yml sandbox-agent' \
  'docker compose -f docker-compose.ci.yml up -d --force-recreate sandbox-agent' \
  "$TESTS::TestTheCiFollowsTheRuleToo::test_no_recreate_in_one_command_in_the_workflow" \
  "the CI recreates the agent in one command"

echo "== the CI does not declare the ceiling it mounts =="
mutate docker-compose.ci.yml \
  '      - AGENT_WORKSPACE_SIZE=${AGENT_WORKSPACE_SIZE:-1g}
' \
  '' \
  "$TESTS::TestTheCiFollowsTheRuleToo::test_the_ci_declares_the_ceiling_it_mounts" \
  "the CI agent is not told which ceiling it has"

echo "== the document stops stating the order =="
mutate docs/SECURITY.md \
  'the order is: stack down, filesystem mounted, stack up' \
  'the order is: stack up, filesystem mounted, stack down' \
  "$TESTS::TestTheDocumentNamesTheOrder::test_the_rule_is_stated" \
  "the document states the wrong order"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
