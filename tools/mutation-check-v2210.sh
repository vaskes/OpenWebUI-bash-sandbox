#!/usr/bin/env bash
# Mutation check for the v2.21.0 tripwires — the mount of the
# workspace belongs to the stack, and no commit may credit a model.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties this wave pins:
#
#   * nothing in the repository puts the workspace into the host's boot
#     table, and the tool that used to do it is gone;
#   * the ceiling travels into the container, and the agent compares the
#     size of the filesystem it is looking at with the ceiling that was
#     promised -- and refuses to serve when they disagree;
#   * no commit message credits a model with a co-author trailer.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python


# The tree has to be green before anything is broken: a mutation sweep
# against a red tree reports catches for a tree nobody broke.
mutation_require_green "tests/test_workspace_ceiling_v2210.py"

echo "== the tool that wrote into the host's boot table is back =="
mutate 'tests/test_workspace_ceiling_v2210.py' \
  '        self.assertFalse(' \
  '        self.skipTest("the boot tool is back: " ' \
  "tests/test_workspace_ceiling_v2210.py::TheMountIsLocalToTheStack::test_there_is_no_tool_that_writes_into_the_boots_table" \
  "the tool that writes into the host's boot table is back"

echo "== the Makefile installs a boot entry again =="
mutate Makefile \
  '	sudo bash tools/setup-workspace.sh "$(WORKSPACE_PATH)" "$(WORKSPACE_SIZE)"' \
  '	sudo bash tools/setup-workspace.sh "$(WORKSPACE_PATH)" "$(WORKSPACE_SIZE)"
	sudo bash tools/install-workspace-mount.sh "$(WORKSPACE_PATH)"' \
  "tests/test_workspace_ceiling_v2210.py::TheMountIsLocalToTheStack::test_the_makefile_does_not_install_a_boot_entry" \
  "the workspace target installs a boot entry again"

echo "== the ceiling does not travel into the container =="
mutate docker-compose.production.yml \
  '      - AGENT_WORKSPACE_SIZE=${AGENT_WORKSPACE_SIZE:-}' \
  '      - AGENT_WORKSPACE_SIZE_UNUSED=${AGENT_WORKSPACE_SIZE:-}' \
  "tests/test_workspace_ceiling_v2210.py::TheMountIsLocalToTheStack::test_the_ceiling_travels_into_the_container" \
  "the container is not told what the ceiling is"

echo "== the agent stops reading the size of the filesystem =="
mutate agent_api.py \
  '        st = os.statvfs(WORKSPACE)' \
  '        st = os.statvfs("/nonexistent-workspace-size-probe")' \
  "tests/test_workspace_ceiling_v2210.py::TheAgentRefusesAWorkspaceWithoutACeiling::test_a_workspace_that_is_not_the_declared_size_is_refused" \
  "the agent does not read the size of the filesystem it is looking at"

echo "== the agent stops noticing a workspace without a ceiling =="
mutate agent_api.py \
  '    if abs(have - want) > want * CEILING_TOLERANCE:' \
  '    if False:' \
  "tests/test_workspace_ceiling_v2210.py::TheAgentRefusesAWorkspaceWithoutACeiling::test_a_workspace_that_is_not_the_declared_size_is_refused" \
  "a plain directory passes as the workspace"

echo "== the refusal does not stop the agent =="
mutate agent_api.py \
  '        raise SystemExit(2)' \
  '        return' \
  "tests/test_workspace_ceiling_v2210.py::TheAgentRefusesAWorkspaceWithoutACeiling::test_the_refusal_stops_the_agent" \
  "the agent keeps serving against a workspace without a ceiling"
