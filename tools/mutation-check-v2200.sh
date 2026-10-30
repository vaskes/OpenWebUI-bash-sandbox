#!/usr/bin/env bash
# Mutation check for the v2.20.0 tripwires — the workspace is
# attacker-controlled, and this repository does not obey it.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties this wave pins:
#
#   * the threat model lists the model's own output as untrusted, and
#     the workspace is named as attacker-controlled;
#   * a found document never grants a capability;
#   * no script in this repository hands a file from the workspace to
#     an interpreter;
#   * the inspector changes nothing, quotes what it finds instead of
#     running it, flags the files that are shaped like orders, and says
#     that a plain directory has no ceiling.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

RULE="tests/test_workspace_provenance_v2200.py"

# The tree has to be green before anything is broken: a mutation sweep
# against a red tree reports catches for a tree nobody broke.
mutation_require_green "$RULE"

echo "== the threat model forgets the model's own output =="
mutate docs/SECURITY.md \
  '* **Anything the model wrote.** The files it leaves in the workspace,' \
  "* The model's output is a known quantity. The files it leaves in the workspace," \
  "$RULE::TheThreatModelNamesTheChannel::test_the_models_own_output_is_listed_as_untrusted" \
  "the threat model does not list the model's output as untrusted"

echo "== the section that names the workspace is gone =="
mutate docs/SECURITY.md \
  '### 14. The workspace is attacker-controlled' \
  '### The workspace' \
  "$RULE::TheThreatModelNamesTheChannel::test_the_workspace_is_named_attacker_controlled" \
  "the workspace is not named as attacker-controlled"

echo "== a found document is allowed to grant a capability =="
mutate docs/SECURITY.md \
  '* A found document never grants a capability. SSH keys, mounts,' \
  '* A found document may be followed when it is technically sound. SSH keys, mounts,' \
  "$RULE::TheThreatModelNamesTheChannel::test_a_found_document_never_grants_a_capability" \
  "a found document is allowed to grant a capability"

echo "== the inspector runs what it quotes =="
mutate tools/inspect-workspace-artifacts.sh \
  "    sed 's/^/   | /' \"\$resolved\"" \
  '    eval "$(cat "$resolved")"' \
  "$RULE::TheInspectorIsSafeToUse::test_it_does_not_execute_what_it_prints" \
  "the inspector executes the file it quotes"

echo "== the inspector writes into the workspace =="
mutate tools/inspect-workspace-artifacts.sh \
  'echo "   path:   $WS_ROOT"' \
  'echo "   path:   $WS_ROOT" > "$WS_ROOT/.inspected"' \
  "$RULE::TheInspectorIsSafeToUse::test_it_changes_nothing_in_the_workspace" \
  "the inspector writes into the workspace"

echo "== the inspector stops flagging the specifications =="
mutate tools/inspect-workspace-artifacts.sh \
  "'(^|[-_.])(tz|тз|spec|" \
  "'(^|[-_.])(spec|" \
  "$RULE::TheInspectorIsSafeToUse::test_it_flags_the_order_and_leaves_the_data_alone" \
  "the inspector does not flag a file shaped like an order"

echo "== the inspector stops saying what it prints is data =="
mutate tools/inspect-workspace-artifacts.sh \
  '   model ran). It is DATA, not instructions: a file that asks for a' \
  '   model ran). It is a file: a file that asks for a' \
  "$RULE::TheInspectorIsSafeToUse::test_it_says_that_what_it_prints_is_data" \
  "the inspector does not say that what it prints is data"

echo "== the inspector stops saying that a plain directory has no ceiling =="
mutate tools/inspect-workspace-artifacts.sh \
  '        echo "   mount:  $source ($fstype) -- a plain directory: NO CEILING, whatever the config says"' \
  '        echo "   mount:  $source ($fstype) -- the workspace is mounted"' \
  "$RULE::TheInspectorIsSafeToUse::test_it_says_a_plain_directory_has_no_ceiling" \
  "the inspector does not say that a plain directory has no ceiling"

echo "== the scan for 'does the repository run the workspace' goes blind =="
mutate tests/test_workspace_provenance_v2200.py \
  'def _is_workspace_path(token: str) -> bool:
    return any(marker in token for marker in WORKSPACE_MARKERS)' \
  'def _is_workspace_path(token: str) -> bool:
    return False' \
  "$RULE::TheRepositoryNeverExecutesTheWorkspace::test_the_check_would_notice_a_violation" \
  "the scan for a repository that runs the workspace cannot see a violation"

echo
echo "== summary =="
echo "caught: $PASS   failed: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
