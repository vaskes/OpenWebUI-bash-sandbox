#!/usr/bin/env bash
# Mutation check for the v2.16.0 tripwires — the storage budget.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property of the sandbox's storage on purpose; the named
# test must go red. If it stays green, the test is decoration and the
# property is unpinned.
#
# The wave this script belongs to closed the last hole that reached past
# the container. The workspace used to be a read-write bind of a host
# directory on the very partition the whole host lives on, so a `dd` in
# the sandbox filled the operator's disk — the one kind of damage a
# container reset does not undo. The workspace is a capped tmpfs inside
# the container now, and the tripwires below are what keeps it that way.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

RULE="tests/test_workspace_budget_v2160.py"

echo "== the workspace is a host directory again =="
mutate docker-compose.yml \
  '      - /workspace:size=${AGENT_WORKSPACE_SIZE:-1g},mode=1777' \
  '      - /opt/agent-workspace:/workspace' \
  "$RULE::TestTheWorkspaceIsCapped::test_the_workspace_is_a_tmpfs_and_not_a_host_path" \
  "the default stack's workspace is a tmpfs again"

echo "== the production stack's workspace is a host directory again =="
mutate docker-compose.production.yml \
  '      - /workspace:size=${AGENT_WORKSPACE_SIZE:-2g},mode=1777' \
  '      - /opt/telegrambot/sandbox-workspace:/workspace' \
  "$RULE::TestTheWorkspaceIsCapped::test_the_workspace_is_a_tmpfs_and_not_a_host_path" \
  "the production stack's workspace is a tmpfs again"

echo "== the CI stack's workspace is a host directory again =="
mutate docker-compose.ci.yml \
  '      - /workspace:size=${AGENT_WORKSPACE_SIZE:-1g},mode=1777' \
  '      - ci-agent-workspace:/workspace' \
  "$RULE::TestTheWorkspaceIsCapped::test_the_workspace_is_a_tmpfs_and_not_a_host_path" \
  "the CI stack's workspace is a tmpfs again"

echo "== the workspace has no ceiling =="
mutate docker-compose.yml \
  '      - /workspace:size=${AGENT_WORKSPACE_SIZE:-1g},mode=1777' \
  '      - /workspace:mode=1777' \
  "$RULE::TestTheWorkspaceIsCapped::test_the_workspace_has_a_ceiling" \
  "the workspace tmpfs has a size"

echo "== the ceiling is smaller than the file policy =="
mutate docker-compose.yml \
  '      - /workspace:size=${AGENT_WORKSPACE_SIZE:-1g},mode=1777' \
  '      - /workspace:size=${AGENT_WORKSPACE_SIZE:-1m},mode=1777' \
  "$RULE::TestTheWorkspaceIsCapped::test_the_ceiling_can_hold_a_file_the_door_accepts" \
  "the workspace can hold a file the door accepts"

echo "== the tmpfs budget is bigger than the memory ceiling =="
mutate docker-compose.yml \
  '    mem_limit: 2g' \
  '    mem_limit: 512m' \
  "$RULE::TestTheWorkspaceIsCapped::test_the_tmpfs_budget_fits_the_memory_ceiling" \
  "the tmpfs mounts fit the container's memory limit"

echo "== a host path is mounted writable =="
mutate docker-compose.yml \
  '      - /opt/agent-workspace/.ssh:/home/agent/.ssh:ro' \
  '      - /opt/agent-workspace/.ssh:/home/agent/.ssh:rw' \
  "$RULE::TestNothingHostSideIsWritable::test_no_host_path_is_mounted_writable" \
  "no host path is mounted writable into the agent"

echo "== the agent gets a named volume back =="
mutate docker-compose.yml \
  '      - /opt/agent-workspace/.ssh:/home/agent/.ssh:ro' \
  '      - sandbox-agent-apt:/var/cache/apt:rw' \
  "$RULE::TestNothingHostSideIsWritable::test_the_agent_has_no_named_volume" \
  "the agent has no named volume"

echo "== the root filesystem becomes writable by default =="
mutate docker-compose.yml \
  '    read_only: "${AGENT_READ_ONLY:-true}"' \
  '    read_only: "${AGENT_READ_ONLY:-false}"' \
  "$RULE::TestNothingHostSideIsWritable::test_the_root_filesystem_is_read_only_by_default" \
  "the root filesystem is read-only by default"

echo "== the image stops creating the mount points =="
mutate Dockerfile.sandbox \
  'RUN mkdir -p /workspace /var/cache/apt /home/agent/.cache/pip \' \
  'RUN mkdir -p /opt/sandbox \' \
  "$RULE::TestTheImageHasTheMountPoints::test_the_image_creates_every_mount_point" \
  "the image creates every tmpfs mount point"

RULE_FULL="tests/test_upload_full_workspace_v2160.py"

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
