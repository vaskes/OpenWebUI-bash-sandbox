#!/usr/bin/env bash
# Mutation check for the vision door — vision_analyze.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties this wave pins:
#
#   * the door is closed unless the operator configured an endpoint, and
#     a closed door says so;
#   * the picture is a file inside the workspace — a path that points
#     out is refused, and a symlink that points out is not followed;
#   * the tool takes a path and not a URL: the endpoint is the
#     operator's, and the model cannot name another one;
#   * every budget is a budget: the picture, the question and the
#     answer are all bounded, and a truncated answer says so;
#   * the door is counted — the counting is the audit trail.
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

TESTS=tests/test_mcp_vision_v2230.py

# The tree has to be green before anything is broken: a mutation sweep
# against a red tree reports catches for a tree nobody broke.
mutation_require_green "$TESTS"

echo "== the door is open even with no endpoint =="
mutate agent_api.py \
  '    if not VISION_URL:' \
  '    if False:' \
  "$TESTS::TestTheDoorIsTheOperators::test_the_door_is_closed_when_no_endpoint_is_configured" \
  "the door is closed only when the operator says so"

echo "== the picture is not checked against the perimeter =="
mutate agent_api.py \
  '    target = _safe_file(path)

    question = str(prompt or "").strip()' \
  '    target = path

    question = str(prompt or "").strip()' \
  "$TESTS::TestThePictureStaysInsideTheWorkspace::test_a_picture_through_a_symlink_that_points_out_is_refused" \
  "the picture is read from wherever the caller says"

echo "== a file that is not a picture is sent anyway =="
mutate agent_api.py \
  '    if mime not in VISION_MIME_TYPES:' \
  '    if False:' \
  "$TESTS::TestThePictureStaysInsideTheWorkspace::test_a_file_that_is_not_a_picture_is_refused" \
  "the door looks at anything the workspace holds"

echo "== the picture is not bounded =="
mutate agent_api.py \
  '    if size > VISION_MAX_BYTES:' \
  '    if False:' \
  "$TESTS::TestTheDoorIsBounded::test_a_picture_over_the_limit_is_refused" \
  "the picture budget does not apply"

echo "== the question is not bounded =="
mutate agent_api.py \
  '    if len(question.encode("utf-8")) > VISION_MAX_PROMPT_BYTES:' \
  '    if False:' \
  "$TESTS::TestTheDoorIsBounded::test_a_question_over_the_limit_is_refused" \
  "the question budget does not apply"

echo "== the answer is not bounded =="
mutate agent_api.py \
  '    if len(encoded) > VISION_MAX_ANSWER_BYTES:' \
  '    if False:' \
  "$TESTS::TestTheDoorIsBounded::test_the_answer_is_bounded_and_says_so" \
  "the answer is whatever the endpoint said"

echo "== the endpoint is not the operator's =="
mutate agent_api.py \
  '            response = await client.post(VISION_URL, json=body, headers=headers)' \
  '            response = await client.post("http://elsewhere.invalid/v1/chat/completions", json=body, headers=headers)' \
  "$TESTS::TestTheAnswerComesBack::test_the_picture_goes_to_the_configured_endpoint" \
  "the picture goes somewhere other than the configured endpoint"

echo "== the door may take a URL =="
# A URL argument would turn this into a retrieval door. The schema is
# the contract the model sees, so the mutation has to show up there.
mutate mcp_server.py \
  'async def vision_analyze(path: str, prompt: str = "Describe this image.") -> VisionResult:' \
  'async def vision_analyze(path: str, prompt: str = "Describe this image.", url: str | None = None) -> VisionResult:' \
  "$TESTS::TestTheDoorIsTheOperators::test_the_tool_takes_no_url" \
  "the tool accepts a URL from the model"

echo "== a failing endpoint is not a refusal =="
mutate agent_api.py \
  '    if response.status_code >= 400:' \
  '    if False:' \
  "$TESTS::TestTheAnswerComesBack::test_an_endpoint_that_fails_is_a_refusal_with_the_reason" \
  "a broken endpoint is not a refusal"

echo "== the request is not counted =="
mutate agent_api.py \
  '    METRICS.inc("vision_requests_total")' \
  '    pass  # the counter is gone' \
  "$TESTS::TestTheDoorLeavesATrace::test_the_request_is_counted" \
  "the request leaves no trace"

echo "== a refusal is not counted =="
mutate mcp_server.py \
  '        METRICS.inc("vision_refused_total")' \
  '        pass  # the counter is gone' \
  "$TESTS::TestTheDoorLeavesATrace::test_a_refusal_is_counted" \
  "a refused request leaves no trace"

# The two mutations below break the DETECTOR, and the control catches them.
echo "== the detector cannot see a refusal outside the workspace any more =="
mutate tests/test_mcp_vision_v2230.py \
  '        self.assertIn("outside the workspace", str(caught.exception))
        # The picture outside did not leave: the endpoint was not called.
        self.assertEqual(recorder.calls, [])' \
  '        self.assertIn("a phrase the detector can no longer see", str(caught.exception))
        # The picture outside did not leave: the endpoint was not called.
        self.assertEqual(recorder.calls, [])' \
  "$TESTS::TestThePictureStaysInsideTheWorkspace::test_a_picture_outside_the_workspace_is_refused" \
  "the detector cannot see the refusal it was written for"

echo "== the detector cannot see a refusal over the budget any more =="
mutate tests/test_mcp_vision_v2230.py \
  '        self.assertIn("over the limit", reason)
        # The refusal names the size and the limit' \
  '        self.assertIn("a phrase the detector can no longer see", reason)
        # The refusal names the size and the limit' \
  "$TESTS::TestTheDoorIsBounded::test_a_picture_over_the_limit_is_refused" \
  "the detector cannot see the refusal it was written for"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
