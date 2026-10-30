#!/usr/bin/env bash
# Mutation check for the v2.19.0 tripwires — the workspace ceiling that
# grows instead of wiping.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property of the sandbox on purpose; the named test must go
# red. If it stays green, the test is decoration and the property is
# unpinned.
#
# The properties this wave pins:
#
#   * a bigger ceiling GROWS the workspace — the image is never deleted,
#     the filesystem is never formatted over a file that is there, and a
#     file that is not a filesystem is asked about before it is made
#     bigger;
#   * a smaller ceiling is REFUSED, and the plan says so before
#     anything happens;
#   * the default ceiling is 4 GiB, in the script and in the Makefile
#     (two copies of one rule: both are pinned);
#   * the mount comes back at boot, and the entry carries the boundary
#     (nosuid, nodev) and does not stop the boot (nofail).
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

RULE="tests/test_workspace_growth_v2190.py"

# The tree has to be green before anything is broken: a mutation sweep
# against a red tree reports catches for a tree nobody broke.
mutation_require_green "$RULE"

echo "== the grow path wipes the workspace again =="
mutate tools/setup-workspace.sh \
  '    truncate -s "$want" "$IMAGE" || {' \
  '    rm -f "$IMAGE"
    truncate -s "$want" "$IMAGE" || {' \
  "$RULE::TheWorkspaceIsNotWiped::test_the_image_is_never_deleted" \
  "the grow path deletes the workspace image"

echo "== the filesystem is made over a file that is there =="
mutate tools/setup-workspace.sh \
  'if [ ! -e "$IMAGE" ]; then' \
  'if true; then' \
  "$RULE::TheWorkspaceIsNotWiped::test_the_workspace_is_never_formatted_over" \
  "the filesystem is formatted over an existing file"

echo "== the grow path stops asking what the file is =="
mutate tools/setup-workspace.sh \
  'if ! dumpe2fs -h "$IMAGE" >/dev/null 2>&1; then' \
  'if false; then' \
  "$RULE::TheWorkspaceIsNotWiped::test_a_file_that_is_not_a_filesystem_is_not_touched" \
  "the grow path does not ask whether the file is a filesystem"

echo "== the plan grows a smaller ceiling instead of refusing it =="
mutate tools/setup-workspace.sh \
  '        echo "refuse"' \
  '        echo "grow"' \
  "$RULE::ThePlanSaysWhatWouldHappen::test_the_ceiling_is_smaller" \
  "the plan grows a smaller ceiling"

echo "== the default ceiling is 1 GiB again =="
mutate tools/setup-workspace.sh \
  'SIZE="${2:-${AGENT_WORKSPACE_SIZE:-4g}}"' \
  'SIZE="${2:-${AGENT_WORKSPACE_SIZE:-1g}}"' \
  "$RULE::TheWorkspaceIsNotWiped::test_the_default_ceiling_is_four_gibibytes" \
  "the script's default ceiling is 1 GiB"

echo "== the Makefile's default ceiling is 1 GiB =="
mutate Makefile \
  'WORKSPACE_SIZE ?= 4g' \
  'WORKSPACE_SIZE ?= 1g' \
  "$RULE::TheWorkspaceIsNotWiped::test_the_makefile_says_the_same_thing" \
  "the Makefile's default ceiling is 1 GiB"

echo "== the documentation says 1 GiB =="
mutate docs/CONFIGURATION.md \
  'Default: `4g`.' \
  'Default: `1g`.' \
  "$RULE::TheDocumentsSayTheSameThing::test_the_configuration_reference_names_the_default_ceiling" \
  "the configuration reference says the default ceiling is 1 GiB"

echo
echo "== verdict =="
echo "caught:   $PASS"
echo "escaped:  $FAIL"
if [ "$FAIL" -ne 0 ]; then
    echo "MUTATIONS ESCAPED — the tripwire above is decoration."
    exit 1
fi
exit 0
