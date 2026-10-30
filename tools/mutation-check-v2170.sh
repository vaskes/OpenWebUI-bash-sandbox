#!/usr/bin/env bash
# Mutation check for the v2.17.0 tripwires — the storage budget and the
# privilege posture.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property of the sandbox on purpose; the named test must go
# red. If it stays green, the test is decoration and the property is
# unpinned.
#
# This wave reverses two decisions of v2.16.0, on the operator's ruling:
#
#   * the workspace goes back to disk, with a hard ceiling. It is a
#     volume over a filesystem of a fixed size, because Docker has no
#     quota for a volume and the ceiling has to come from somewhere;
#   * where sudo is on, the root may write. A root that cannot write
#     is a half-root, and a half-root is a deployment that passes every
#     check and fails at the first apt-get.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

RULE="tests/test_workspace_budget_v2170.py"

echo "== the workspace is a host directory again =="
mutate docker-compose.yml \
  '      - owbs-workspace:/workspace' \
  '      - /opt/agent-workspace/sandbox-workspace:/workspace' \
  "$RULE::TestTheWorkspaceIsOnDiskWithACeiling::test_the_workspace_is_a_volume_and_not_a_tmpfs" \
  "the default stack's workspace is a volume again"

echo "== the production stack's workspace is a host directory again =="
mutate docker-compose.production.yml \
  '      - owbs-workspace:/workspace' \
  '      - /opt/telegrambot/sandbox-workspace:/workspace' \
  "$RULE::TestTheWorkspaceIsOnDiskWithACeiling::test_the_workspace_is_a_volume_and_not_a_tmpfs" \
  "the production stack's workspace is a volume again"

echo "== the CI stack's workspace is a host directory again =="
mutate docker-compose.ci.yml \
  '      - owbs-workspace:/workspace' \
  '      - ci-agent-workspace:/workspace' \
  "$RULE::TestTheWorkspaceIsOnDiskWithACeiling::test_the_workspace_is_a_volume_and_not_a_tmpfs" \
  "the CI stack's workspace is a volume again"

echo "== the workspace is a tmpfs again =="
mutate docker-compose.yml \
  '      - /var/cache/apt:size=128m,mode=1777' \
  '      - /workspace:size=${AGENT_WORKSPACE_SIZE:-1g},mode=1777
      - /var/cache/apt:size=128m,mode=1777' \
  "$RULE::TestTheWorkspaceIsOnDiskWithACeiling::test_the_workspace_is_a_volume_and_not_a_tmpfs" \
  "the workspace is not a tmpfs again"

echo "== the workspace path is relative =="
mutate docker-compose.yml \
  '      device: ${AGENT_WORKSPACE_PATH:-/opt/owbs-workspace}' \
  '      device: ./sandbox-workspace' \
  "$RULE::TestTheWorkspaceIsOnDiskWithACeiling::test_the_volume_binds_a_host_path" \
  "the volume binds an absolute host path"

echo "== the workspace path is not a knob =="
mutate docker-compose.yml \
  '      device: ${AGENT_WORKSPACE_PATH:-/opt/owbs-workspace}' \
  '      device: /opt/owbs-workspace' \
  "$RULE::TestTheWorkspaceIsOnDiskWithACeiling::test_the_volume_binds_a_host_path" \
  "the workspace path is the knob"

echo "== the workspace's mode is not the scratchpad's mode =="
mutate tools/setup-workspace.sh \
  'WORKSPACE_MODE=1777' \
  'WORKSPACE_MODE=0755' \
  "$RULE::TestTheWorkspaceIsOnDiskWithACeiling::test_the_workspace_is_writable_by_the_root_the_container_hands_out" \
  "the workspace is writable by the root the container hands out"

echo "== the setup script never sets the mode =="
# The chmod is in two places -- the workspace that is already there and
# the one this run makes -- so both copies have to go: breaking one
# leaves the other in place, and the property survives.
mutate_all tools/setup-workspace.sh \
  'chmod "$WORKSPACE_MODE" "$MOUNTPOINT"' \
  ':' \
  "$RULE::TestTheWorkspaceIsOnDiskWithACeiling::test_the_workspace_is_writable_by_the_root_the_container_hands_out" \
  "the setup script sets the mode of the workspace"

echo "== the volume check accepts any volume =="
mutate tools/check-workspace-volume.sh \
  'if [ "$device" = "$WS_PATH" ]; then' \
  'if true; then' \
  "$RULE::TestTheWorkspaceIsOnDiskWithACeiling::test_the_workspace_volume_has_to_be_the_workspace" \
  "the workspace volume has to be the workspace"

echo "== the setup script stops making the file =="
mutate tools/setup-workspace.sh \
  '    truncate -s "$SIZE" "$IMAGE"' \
  '    :' \
  "$RULE::TestTheWorkspaceIsOnDiskWithACeiling::test_the_ceiling_comes_from_a_filesystem_somebody_makes" \
  "the setup script makes a file of the ceiling"

echo "== the setup script stops making a filesystem =="
mutate tools/setup-workspace.sh \
  '    mkfs.ext4 -F -q -E root_owner=1000:1000 "$IMAGE"' \
  '    :' \
  "$RULE::TestTheWorkspaceIsOnDiskWithACeiling::test_the_ceiling_comes_from_a_filesystem_somebody_makes" \
  "the setup script makes a filesystem"

echo "== the setup script stops mounting it =="
mutate tools/setup-workspace.sh \
  'mount -o loop "$IMAGE" "$MOUNTPOINT"' \
  'mount --bind "$IMAGE" "$MOUNTPOINT"' \
  "$RULE::TestTheWorkspaceIsOnDiskWithACeiling::test_the_ceiling_comes_from_a_filesystem_somebody_makes" \
  "the setup script mounts the filesystem"

echo "== nobody runs the setup script =="
# The step is in two jobs, so both copies have to go: breaking one
# leaves the other in place, and the property survives.
mutate_all .github/workflows/ci.yml \
  'run: sudo bash tools/setup-workspace.sh "${AGENT_WORKSPACE_PATH:-/tmp/owbs-workspace}" "${AGENT_WORKSPACE_SIZE:-1g}"' \
  'run: echo "the workspace can wait"' \
  "$RULE::TestTheWorkspaceIsOnDiskWithACeiling::test_the_deployment_runs_the_setup_script" \
  "the deployment runs the setup script"

echo "== the setup script does not know the file policy =="
mutate tools/setup-workspace.sh \
  'POLICY="${AGENT_BINARY_LIMIT:-0}"' \
  'POLICY=0' \
  "$RULE::TestTheWorkspaceIsOnDiskWithACeiling::test_the_ceiling_has_to_hold_what_the_door_accepts" \
  "the ceiling is compared with the file policy"

echo "== the tmpfs budget is bigger than the memory ceiling =="
mutate docker-compose.yml \
  '    mem_limit: 2g' \
  '    mem_limit: 512m' \
  "$RULE::TestTheWorkspaceIsOnDiskWithACeiling::test_the_tmpfs_budget_fits_the_memory_ceiling" \
  "the tmpfs mounts fit the container's memory limit"

echo "== the root filesystem becomes writable by default =="
mutate docker-compose.yml \
  '    read_only: "${AGENT_READ_ONLY:-true}"' \
  '    read_only: "${AGENT_READ_ONLY:-false}"' \
  "$RULE::TestTheRootCanWriteWhereItIsRoot::test_the_stock_deployment_has_no_sudo_and_a_read_only_root" \
  "the root filesystem is read-only by default"

echo "== the privilege mode is on by default =="
mutate docker-compose.yml \
  '        AGENT_ALLOW_SUDO: "${AGENT_ALLOW_SUDO:-0}"' \
  '        AGENT_ALLOW_SUDO: "${AGENT_ALLOW_SUDO:-1}"' \
  "$RULE::TestTheRootCanWriteWhereItIsRoot::test_the_stock_deployment_has_no_sudo_and_a_read_only_root" \
  "the privilege mode is off by default"

echo "== the posture check accepts a half-root =="
mutate tools/check-posture.sh \
  'if [ "$SUDO_ON" -eq 1 ] && [ "$ROOT_RO" -eq 1 ]; then' \
  'if false; then' \
  "$RULE::TestTheRootCanWriteWhereItIsRoot::test_a_deployment_with_sudo_may_not_keep_the_root_read_only" \
  "a half-root is refused"

echo "== the posture check refuses a real root =="
mutate tools/check-posture.sh \
  'exit 0' \
  'exit 1' \
  "$RULE::TestTheRootCanWriteWhereItIsRoot::test_a_deployment_with_sudo_may_not_keep_the_root_read_only" \
  "a real root is accepted"

echo "== the image stops creating the mount points =="
mutate Dockerfile.sandbox \
  'RUN mkdir -p /workspace /var/cache/apt /home/agent/.cache/pip \' \
  'RUN mkdir -p /opt/sandbox \' \
  "$RULE::TestTheImageHasTheMountPoints::test_the_image_creates_every_mount_point" \
  "the image creates every mount point"

echo "== the configuration reference calls the workspace a tmpfs again =="
mutate docs/CONFIGURATION.md \
  '| `/workspace`        | volume `owbs-workspace` over a filesystem of `AGENT_WORKSPACE_SIZE` | yes (and capped) |' \
  '| `/workspace`        | tmpfs inside the container, size `AGENT_WORKSPACE_SIZE` | yes (and capped) |' \
  "$RULE::TestTheDocumentsMatchTheTree::test_the_configuration_reference_describes_the_workspace" \
  "the layout table describes the workspace"

echo "== the configuration reference forgets the workspace path =="
mutate_all docs/CONFIGURATION.md \
  'AGENT_WORKSPACE_PATH' \
  'AGENT_WORKSPACE_PLACE' \
  "$RULE::TestTheDocumentsMatchTheTree::test_the_configuration_reference_names_the_workspace_path" \
  "the configuration reference names the workspace path"

echo "== the security model forgets the ceiling =="
mutate_all docs/SECURITY.md \
  'AGENT_WORKSPACE_SIZE' \
  'AGENT_WORKSPACE_CEILING' \
  "$RULE::TestTheDocumentsMatchTheTree::test_the_security_model_names_the_ceiling_and_its_price" \
  "the security model names the ceiling"

echo "== the security model forgets where the workspace lives =="
mutate_all docs/SECURITY.md \
  'AGENT_WORKSPACE_PATH' \
  'AGENT_WORKSPACE_PLACE' \
  "$RULE::TestTheDocumentsMatchTheTree::test_the_security_model_names_the_ceiling_and_its_price" \
  "the security model names the workspace path"

echo "== the readme forgets the ceiling =="
mutate_all README.md \
  'AGENT_WORKSPACE_SIZE' \
  'AGENT_WORKSPACE_CEILING' \
  "$RULE::TestTheDocumentsMatchTheTree::test_the_readme_names_the_ceiling" \
  "the readme names the ceiling"

echo "== the env example forgets the workspace path =="
mutate_all .env.example \
  'AGENT_WORKSPACE_PATH' \
  'AGENT_WORKSPACE_PLACE' \
  "$RULE::TestTheDocumentsMatchTheTree::test_the_env_example_names_the_workspace_path" \
  "the env example names the workspace path"

RULE_FULL="tests/test_upload_full_workspace_v2170.py"

echo "== a full workspace is a 500 =="
mutate agent_api.py \
  '    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise HTTPException(
                status_code=507,' \
  '    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise HTTPException(
                status_code=500,' \
  "$RULE_FULL::TestAFullWorkspaceIsNamed::test_the_file_that_cannot_land_answers_507" \
  "a full workspace answers 507"

echo "== a workspace that cannot take the file is a 500 =="
mutate agent_api.py \
  '        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise HTTPException(
                    status_code=507,' \
  '        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise HTTPException(
                    status_code=500,' \
  "$RULE_FULL::TestAFullWorkspaceIsNamed::test_a_workspace_that_cannot_take_the_file_answers_507" \
  "a workspace that cannot take the file answers 507"

echo "== the refusal changes its wording =="
mutate agent_api.py \
  '        "the sandbox workspace is full — the file did not land. The "' \
  '        "the storage error — the file did not land. The "' \
  "$RULE_FULL::TestTheDocumentedRefusalIsTheRefusalTheCodeProduces::test_the_code_produces_this_refusal" \
  "the code produces the refusal the handoff promises"

echo "== the handoff stops promising the refusal =="
mutate docs/HANDOFF-binary-transport.md \
  '*the sandbox workspace is full' \
  '*the storage is full' \
  "$RULE_FULL::TestTheDocumentedRefusalIsTheRefusalTheCodeProduces::test_the_handoff_promises_this_refusal" \
  "the handoff promises the full-workspace refusal"

echo "== the contract stops naming the 507 =="
mutate docs/HANDOFF-binary-transport.md \
  'workspace with no room left is a **507**.' \
  'workspace with no room left is a **517**.' \
  "tests/test_doc_drift_v2149.py::TestTheHandoffContractNamesEveryStatusTheDoorAnswers::test_every_status_the_door_raises_is_in_the_enumeration" \
  "the contract's status enumeration names every code the door answers"

echo "== the refused upload leaves a temporary file behind =="
mutate agent_api.py \
  '    finally:
        if tmp_path is not None:' \
  '    finally:
        if False:' \
  "$RULE_FULL::TestAFullWorkspaceIsNamed::test_nothing_is_left_behind_when_the_workspace_is_full" \
  "a refused upload leaves nothing behind"

echo "== the proxy flattens the full-workspace answer =="
# The relay lives in three places (download, upload, delete), and the
# fact under test is "the proxy relays the upstream status as it is" --
# so all three copies have to break, or the mutation measures a tree
# that still tells the truth.
mutate_all openapi_proxy.py \
  '            status_code=upstream.status_code, detail=_upstream_detail(body)' \
  '            status_code=502, detail=_upstream_detail(body)' \
  "$RULE_FULL::TestTheProxyRelaysTheFullWorkspace::test_the_status_is_relayed" \
  "the proxy relays the agent's status as it is"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
