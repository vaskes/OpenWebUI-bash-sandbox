#!/usr/bin/env bash
# Mutation check for the file tools — write_file, patch, search_files.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties this wave pins:
#
#   * a write lands inside the workspace or nowhere at all — a symlink
#     that points out is refused, and the target is resolved before it
#     is used;
#   * a write is bounded — over the budget, nothing is written;
#   * a patch that does not apply changes nothing, and a diff of two
#     files, or of no hunks, is refused;
#   * a search never reads outside the workspace, and its answer is
#     bounded;
#   * the file operations are counted — that counting is the audit
#     trail this wave exists to restore.
#
# The mutations below also break the DETECTOR, and the control catches
# them. A verdict about "the property still holds" is a statement about
# the detector as much as about the property, and a detector that cannot
# see the thing it was written for would report a clean bill of health
# for a tree nobody broke.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

TESTS=tests/test_mcp_file_tools_v2220.py

# The tree has to be green before anything is broken: a mutation sweep
# against a red tree reports catches for a tree nobody broke.
mutation_require_green "$TESTS"

echo "== the write does not resolve the target =="
# The path is resolved before it is used. Without the resolution a
# symlink that points out of the workspace is not a symlink any more:
# the write replaces the link itself and the file it pointed at is
# never touched -- which is safe, but it is not what the tool says it
# does, and a link the model made is a link the model meant.
mutate agent_api.py \
  'real = os.path.realpath(candidate)' \
  'real = candidate' \
  "$TESTS::TestTheWriteStaysInsideTheWorkspace::test_a_write_through_a_symlink_that_points_out_is_refused" \
  "the write does not resolve the path it writes"

echo "== the resolved path is not checked against the perimeter =="
# This is the shared rule: resolve, and confirm the result is inside
# the workspace. Every door that resolves a path goes through it.
mutate agent_api.py \
  '    if real.startswith(WORKSPACE_PREFIX):
        return real' \
  '    if True:
        return real' \
  "$TESTS::TestTheWriteStaysInsideTheWorkspace::test_a_write_outside_the_workspace_is_refused" \
  "the write does not compare the path it writes against the perimeter"

echo "== the write is not bounded =="
mutate agent_api.py \
  '    if len(payload) > limit:' \
  '    if False:' \
  "$TESTS::TestTheWriteStaysInsideTheWorkspace::test_a_write_over_the_limit_is_refused" \
  "the write budget does not apply"

echo "== the patch does not check that the hunk matched =="
mutate agent_core.py \
  'if _diff_line_content(actual_line) != expected:' \
  'if False:' \
  "$TESTS::TestTheDiffApplier::test_a_hunk_that_does_not_match_is_refused" \
  "a patch that does not apply is applied anyway"

echo "== the diff may carry two files =="
mutate agent_core.py \
  '                if seen_hunk:' \
  '                if False:' \
  "$TESTS::TestTheDiffApplier::test_a_diff_of_two_files_is_refused" \
  "a diff of two files is applied to the first one"

echo "== a diff without hunks is not refused =="
mutate agent_core.py \
  '    if not hunks:' \
  '    if False:' \
  "$TESTS::TestTheDiffApplier::test_a_diff_without_hunks_is_refused" \
  "a diff with nothing in it is not refused"

echo "== the search reads through a symlink that points out =="
mutate agent_api.py \
  '            if not real.startswith(WORKSPACE_PREFIX):' \
  '            if False:' \
  "$TESTS::TestTheSearchStaysInsideTheWorkspace::test_a_search_does_not_read_through_a_symlink_that_points_out" \
  "the search reads a file outside the workspace"

echo "== the search's root is not checked =="
mutate agent_api.py \
  'root = _safe_parent(_anchor(path or ".", allow_root=True))' \
  'root = _anchor(path or ".", allow_root=True)' \
  "$TESTS::TestTheSearchStaysInsideTheWorkspace::test_a_search_over_a_path_outside_the_workspace_is_refused" \
  "the search walks outside the workspace"

echo "== the search's answer is not bounded =="
mutate_all agent_api.py \
  'if len(matches) >= cap:' \
  'if False:' \
  "$TESTS::TestTheSearchStaysInsideTheWorkspace::test_a_search_is_bounded_and_says_so" \
  "the search returns every match it finds"

echo "== the search reads a file over the read limit =="
mutate agent_api.py \
  '            if size > per_file:' \
  '            if False:' \
  "$TESTS::TestTheSearchStaysInsideTheWorkspace::test_a_search_skips_a_file_over_the_read_limit" \
  "the search reads a file the read door would refuse"

echo "== the write is not counted =="
mutate agent_api.py \
  'METRICS.inc("file_writes_total")' \
  'pass  # the counter is gone' \
  "$TESTS::TestTheFileToolsLeaveATrace::test_a_write_is_counted" \
  "the write leaves no trace"

echo "== the catalogue does not name the tools =="
mutate mcp_server.py \
  '        "write_file",
' \
  '' \
  "$TESTS::TestTheToolsAreTheDocumentedTools::test_the_tool_names_are_the_ones_the_code_registers" \
  "the tool list does not name write_file"

# The two mutations below break the DETECTOR, and the control catches them.
echo "== the detector cannot see a write that does not apply any more =="
mutate tests/test_mcp_file_tools_v2220.py \
  'self.assertIn("does not apply", reason)' \
  'self.assertIn("a phrase the detector can no longer see", reason)' \
  "$TESTS::TestThePatchChangesNothingWhenItDoesNotApply::test_a_patch_that_does_not_apply_is_refused" \
  "the detector cannot see the refusal it was written for"

echo "== the detector cannot see a refusal outside the workspace any more =="
mutate tests/test_mcp_file_tools_v2220.py \
  'self.assertIn("outside the workspace", reason)' \
  'self.assertIn("a phrase the detector can no longer see", reason)' \
  "$TESTS::TestTheWriteStaysInsideTheWorkspace::test_a_write_outside_the_workspace_is_refused" \
  "the detector cannot see the refusal it was written for"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
