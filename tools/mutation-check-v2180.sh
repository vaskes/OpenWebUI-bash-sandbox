#!/usr/bin/env bash
# Mutation check for the v2.18.0 tripwires — the python environment.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property of the sandbox on purpose; the named test must go
# red. If it stays green, the test is decoration and the property is
# unpinned.
#
# What this wave is about: the model asked for pip, and the image had
# none at runtime. pip was in the builder stage only, so every install
# cost a trip through apt and sudo, paid out of the command's timeout.
#
# The properties pinned here:
#
#   * the runtime image has a package manager (pip) and can make a
#     virtual environment (venv -- that is ensurepip);
#   * the preinstalled tree and its wheelhouse are in the image, and
#     pip is pointed at the wheelhouse;
#   * the tree is built from the local wheels, offline;
#   * the tree is installed with --target, out of the interpreter's
#     site-packages, and the service's PYTHONPATH does not carry it --
#     the tree has its own pydantic and httpx, and the service's pins
#     must not be shadowed by them;
#   * the model's shell does get it -- that is the whole point;
#   * the model is told: the tool description names every package the
#     image ships.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

RULE="tests/test_python_env_v2180.py"

# The tripwire has to be green before anything is broken. A script that
# runs against a red tree reports catches for a tree that was already
# broken -- and the verdicts look perfectly fine while measuring nothing.
mutation_require_green "$RULE"

echo "== the runtime image loses pip =="
mutate Dockerfile.sandbox \
  '        python3 \
        python3-pip \
        python3-venv \
' \
  '        python3 \
        python3-venv \
' \
  "$RULE::TestTheRuntimeImageHasAPackageManager::test_the_runtime_stage_installs_pip" \
  "the runtime image has a package manager"

echo "== the runtime image loses venv =="
mutate Dockerfile.sandbox \
  '        python3 \
        python3-pip \
        python3-venv \
' \
  '        python3 \
        python3-pip \
' \
  "$RULE::TestTheRuntimeImageHasAPackageManager::test_the_runtime_stage_installs_venv" \
  "the runtime image can make a virtual environment"

echo "== the preinstalled tree is not copied into the image =="
mutate Dockerfile.sandbox \
  'COPY --from=builder /opt/pylibs /opt/pylibs' \
  '# the tree is not copied' \
  "$RULE::TestTheImageCarriesTheTreeAndItsWheels::test_the_tree_is_copied_into_the_image" \
  "the preinstalled tree is in the image"

echo "== the wheelhouse is not copied into the image =="
mutate Dockerfile.sandbox \
  'COPY --from=builder /opt/wheelhouse /opt/wheelhouse' \
  '# the wheelhouse is not copied' \
  "$RULE::TestTheImageCarriesTheTreeAndItsWheels::test_the_wheelhouse_is_copied_into_the_image" \
  "the wheelhouse is in the image"

echo "== pip is not pointed at the wheelhouse =="
mutate Dockerfile.sandbox \
  '    PIP_FIND_LINKS="/opt/wheelhouse" \
' \
  '' \
  "$RULE::TestTheImageCarriesTheTreeAndItsWheels::test_pip_is_pointed_at_the_wheelhouse" \
  "pip finds the local wheels"

echo "== the tree is installed from the network instead of the wheels =="
mutate Dockerfile.sandbox \
  'python3 -m pip install --no-index --find-links=/opt/wheelhouse' \
  'python3 -m pip install --find-links=/opt/wheelhouse' \
  "$RULE::TestTheImageCarriesTheTreeAndItsWheels::test_the_tree_is_built_from_the_wheelhouse_offline" \
  "the tree is built from the local wheels"

echo "== the wheels are fetched into the wrong place =="
mutate Dockerfile.sandbox \
  'python3 -m pip download --dest /opt/wheelhouse' \
  'python3 -m pip download --dest /tmp/wheelhouse' \
  "$RULE::TestTheImageCarriesTheTreeAndItsWheels::test_the_wheels_are_fetched_once" \
  "the wheels are fetched into the wheelhouse"

echo "== the tree is installed into the interpreter instead of --target =="
mutate Dockerfile.sandbox \
  '--target=/opt/pylibs' \
  '--target=/usr/local/lib/python3.14/site-packages' \
  "$RULE::TestTheImageCarriesTheTreeAndItsWheels::test_the_tree_is_installed_with_target" \
  "the tree is installed with --target"

echo "== the service sees the preinstalled tree =="
mutate Dockerfile.sandbox \
  '    PYTHONPATH="/home/agent" \
' \
  '    PYTHONPATH="/home/agent:/opt/pylibs" \
' \
  "$RULE::TestTheServiceDoesNotSeeThePreinstalledTree::test_the_image_pythonpath_does_not_carry_the_tree" \
  "the service's dependencies are not shadowed by the tree"

echo "== the model's shell does not get the tree =="
mutate agent_api.py \
  '    env["PYTHONPATH"] = (
        f"{PREINSTALLED_PYTHON_PATH}:{pythonpath}" if pythonpath else PREINSTALLED_PYTHON_PATH
    )' \
  '    env["PYTHONPATH"] = pythonpath' \
  "$RULE::TestTheModelShellGetsTheTree::test_the_tmux_environment_carries_the_tree" \
  "the model's shell sees the preinstalled packages"

echo "== the code looks for the tree in another place than the image builds it =="
mutate agent_config.py \
  'PREINSTALLED_PYTHON_PATH = "/opt/pylibs"' \
  'PREINSTALLED_PYTHON_PATH = "/opt/pylibs-elsewhere"' \
  "$RULE::TestTheModelShellGetsTheTree::test_the_code_and_the_image_agree_on_the_path" \
  "the code and the image agree on the path"

echo "== the model is not told about one of the packages =="
mutate mcp_server.py \
  '"numpy, matplotlib, pandas, sqlalchemy, openai, requests, httpx, "' \
  '"matplotlib, pandas, sqlalchemy, openai, requests, httpx, "' \
  "$RULE::TestTheModelIsToldWhatItCanInstall::test_the_tool_description_names_every_preinstalled_package" \
  "the tool description names every preinstalled package"

echo "== the model is not told how to install a package =="
mutate mcp_server.py \
  '`python3 -m venv /workspace/.venv && ' \
  '`pip install --target /workspace/libs ' \
  "$RULE::TestTheModelIsToldWhatItCanInstall::test_the_tool_description_says_how_to_add_a_package" \
  "the tool description says how to add a package"

echo
echo "mutations: $PASS caught, $FAIL failed"
[ "$FAIL" -eq 0 ]
