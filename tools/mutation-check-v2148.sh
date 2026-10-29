#!/usr/bin/env bash
# Mutation check for the v2.14.8 tripwires.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property of the code on purpose; the named test must go red.
# If it stays green, the test is decoration and the property is unpinned.
#
# The wave this script belongs to closed three findings of the fourth
# review: a mutation script that counted a catch for a test that never
# ran (U1), the shell's own prompt read as a continuation prompt (U2), and
# a correction that did not live where the claim lives (U3).
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

RULE_MUTATION="tests/test_mutation_scripts_run_v2148.py"
RULE_PROMPT="tests/test_bare_prompt_v2148.py"
RULE_CHANGELOG="tests/test_changelog_correction_v2148.py"

echo "== a run that collected nothing counts as a catch again =="
mutate tools/mutation-lib.sh \
  'mutation_run_was_invalid() {' \
  'mutation_run_was_invalid() {
    return 1
}' \
  "$RULE_MUTATION" \
  "a run that collected nothing is not a catch"

echo "== the script does not refuse a python without pytest =="
mutate tools/mutation-lib.sh \
  '    echo "no python with pytest found — set PY=/path/to/python" >&2
    exit 2' \
  '    echo "no python with pytest found — set PY=/path/to/python" >&2
    exit 1' \
  "$RULE_MUTATION" \
  "no interpreter with pytest is a loud failure"

echo "== the script does not refuse a PY without pytest =="
mutate tools/mutation-lib.sh \
  '        echo "PY=$PY cannot import pytest — point PY at an interpreter that has pytest" >&2
        exit 2' \
  '        echo "PY=$PY cannot import pytest — point PY at an interpreter that has pytest" >&2
        exit 1' \
  "$RULE_MUTATION" \
  "a PY that cannot import pytest is a loud failure"

echo "== a script names the interpreter of a machine =="
mutate tools/mutation-lib.sh \
  '# The interpreter the scripts use. PY is honoured only when it can import' \
  '# The interpreter the scripts use: /workspace/.venv-owbs/bin/python3. PY is honoured only when it can import' \
  "$RULE_MUTATION" \
  "no script names the interpreter of a machine"

echo "== the marker check takes the shell's own prompt again =="
mutate agent_core.py \
  '    return line.rstrip() == CONTINUATION_PROMPT' \
  '    return line.rstrip() in (CONTINUATION_PROMPT, DEFAULT_CONTINUATION_PROMPT)' \
  "$RULE_PROMPT" \
  "the marker is the only continuation prompt"

echo "== the protocol flag is ignored =="
mutate agent_api.py \
  '    if protocol_ran:' \
  '    if False:' \
  "$RULE_PROMPT" \
  "the shell's own prompt is a prompt only before the protocol ran"

echo "== the protocol is never noted =="
mutate agent_api.py \
  '    session["protocol"] = True' \
  '    session["protocol"] = False' \
  "$RULE_PROMPT" \
  "the agent knows that the protocol ran"

echo "== the correction is not where the claim is =="
mutate CHANGELOG.md \
  '  **Corrected in [2.14.6]: the count on v2.14.4 is 98, not 101.** The' \
  '  The count was wrong and nobody said so. The' \
  "$RULE_CHANGELOG" \
  "the claim's own section points at the correction"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
