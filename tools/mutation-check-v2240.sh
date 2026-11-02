#!/usr/bin/env bash
# Mutation check for the session tools — todo_list, memory, session_search,
# clarify.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties this wave pins:
#
#   * the task list is replaceable and bounded — over the limit is
#     refused, and the list that was there survives the refusal;
#   * the memory is a file: it is on disk, it is bounded, and a note
#     that is too long is refused;
#   * the recall finds the commands a session ran, its pattern is
#     literal text, and its answer is bounded;
#   * the question goes back to the caller, and the session record says
#     what is being waited for;
#   * the tools are counted.
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

TESTS=tests/test_mcp_session_tools_v2240.py

# The tree has to be green before anything is broken: a mutation sweep
# against a red tree reports catches for a tree nobody broke.
mutation_require_green "$TESTS"

echo "== the task list is not bounded =="
mutate agent_api.py \
  '    if len(cleaned) > TODO_MAX_ITEMS:' \
  '    if False:' \
  "$TESTS::TestTheTaskListIsAPlan::test_a_list_over_the_limit_is_refused_and_the_old_list_stays" \
  "the task list has no limit"

echo "== an item of the task list is not bounded =="
mutate agent_api.py \
  '        if len(text.encode("utf-8")) > limit:' \
  '        if False:' \
  "$TESTS::TestTheTaskListIsAPlan::test_an_item_over_the_limit_is_refused" \
  "an item of the task list has no limit"

echo "== the task list is merged instead of replaced =="
mutate agent_api.py \
  '    TODO_LISTS[session_id] = cleaned' \
  '    TODO_LISTS[session_id] = todo_list_state(session_id) + cleaned' \
  "$TESTS::TestTheTaskListIsAPlan::test_the_list_is_replaced_and_not_merged" \
  "the task list cannot be rewritten"

echo "== the session record does not carry the task list =="
mutate agent_api.py \
  '                "todo": todo_list_state(sid),' \
  '                "todo": [],' \
  "$TESTS::TestTheTaskListIsAPlan::test_the_session_record_carries_the_list" \
  "the session record says nothing about the plan"

echo "== the memory is not bounded =="
mutate agent_api.py \
  '    if len(notes) >= MEMORY_MAX_NOTES:' \
  '    if False:' \
  "$TESTS::TestTheMemoryIsAFile::test_the_memory_is_bounded" \
  "the memory has no limit"

echo "== a note of the memory is not bounded =="
mutate agent_api.py \
  '    if len(note.encode("utf-8")) > limit:' \
  '    if False:' \
  "$TESTS::TestTheMemoryIsAFile::test_a_note_over_the_limit_is_refused" \
  "a note of the memory has no limit"

echo "== the memory is not a file =="
# The memory has to survive the agent: a memory that lives in the
# process is a memory that forgets.
mutate agent_api.py \
  '    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(notes, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, MEMORY_FILE)' \
  '    return' \
  "$TESTS::TestTheMemoryIsAFile::test_the_memory_survives_the_agent_going_away" \
  "the memory does not survive the agent"

echo "== an unknown action is not refused =="
mutate mcp_server.py \
  '        else:
            raise ToolError(
                f"unknown action: {what} — the memory knows list, add, delete and clear"
            )' \
  '        else:
            notes = memory_notes()' \
  "$TESTS::TestTheMemoryIsAFile::test_an_unknown_action_is_refused" \
  "the memory accepts an action it does not know"

echo "== the recall's pattern is a regular expression =="
mutate agent_api.py \
  '            if needle not in command:' \
  '            if not re.search(needle, command):' \
  "$TESTS::TestTheRecallFindsWhatRan::test_the_pattern_is_literal_text" \
  "the recall's pattern is a regular expression"

echo "== the recall's answer is not bounded =="
mutate agent_api.py \
  '            if len(matches) >= cap:
                truncated = True
                reason = "the answer was cut at the limit"' \
  '            if False:
                truncated = True
                reason = "the answer was cut at the limit"' \
  "$TESTS::TestTheRecallFindsWhatRan::test_the_answer_is_bounded" \
  "the recall returns every match it finds"

echo "== the history is not bounded =="
mutate agent_api.py \
  '    if len(history) > SESSION_HISTORY_MAX_COMMANDS:' \
  '    if False:' \
  "$TESTS::TestTheRecallFindsWhatRan::test_the_history_is_bounded" \
  "the history grows without limit"

echo "== the question is not remembered =="
mutate agent_api.py \
  '    PENDING_QUESTIONS[session_id] = text' \
  '    pass  # the question is forgotten' \
  "$TESTS::TestTheQuestionGoesToTheCaller::test_the_question_is_asked_and_remembered" \
  "the session record says nothing about the question"

echo "== the tools are not counted =="
mutate agent_api.py \
  '    METRICS.inc("todo_ops_total")' \
  '    pass  # the counter is gone' \
  "$TESTS::TestTheSessionToolsLeaveATrace::test_the_task_list_is_counted" \
  "the task list leaves no trace"

# The mutation below breaks the DETECTOR, and the control catches it.
echo "== the detector cannot see a refusal over the limit any more =="
mutate tests/test_mcp_session_tools_v2240.py \
  '        self.assertIn("over the limit", reason)
        self.assertIn("2", reason)' \
  '        self.assertIn("a phrase the detector can no longer see", reason)
        self.assertIn("2", reason)' \
  "$TESTS::TestTheTaskListIsAPlan::test_a_list_over_the_limit_is_refused_and_the_old_list_stays" \
  "the detector cannot see the refusal it was written for"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
