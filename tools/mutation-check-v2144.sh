#!/usr/bin/env bash
# Mutation check for the v2.14.4 tripwires (the stuck session).
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties:
#
#   * a command that would leave the shell waiting is refused before it
#     is typed;
#   * the refusal names the reason, and the shell is never touched;
#   * a shell that is waiting for the rest of an unfinished command is
#     interrupted, and the answer says so;
#   * a session that is busy running something is NOT interrupted;
#   * the exec limit is the deployment's, and the paths that start the
#     stack say whose env file they use.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

PASS=0
FAIL=0

RULE="tests/test_stuck_session_v2144.py"

echo "== the gate is switched off =="
mutate agent_api.py \
    '    stuck_reason = await asyncio.to_thread(command_would_stick, body.command)
    if stuck_reason:' \
    '    stuck_reason = await asyncio.to_thread(command_would_stick, body.command)
    if False:' \
    "$RULE::TestTheGate::test_an_unterminated_quote_is_refused" \
    "an unterminated quote is refused"

echo "== the refusal is not marked as a refusal =="
mutate agent_api.py \
    '            "refused": True,' \
    '            "refused": False,' \
    "$RULE::TestTheGate::test_the_command_is_never_typed" \
    "the answer says the command was refused"

echo "== an unfinished construct is called a syntax error again =="
mutate agent_core.py \
    '        if _is_an_unfinished_command(message):' \
    '        if False:' \
    "$RULE::TestTheRefusalNamesTheRightThing::test_an_unfinished_construct_is_not_called_a_syntax_error" \
    "an unfinished construct is not called a syntax error"

echo "== the line continuation is not noticed =="
mutate agent_core.py \
    '    return trailing % 2 == 1' \
    '    return False' \
    "$RULE::TestTheGate::test_a_trailing_backslash_is_refused" \
    "a trailing backslash is refused"

echo "== bash is not asked to parse the command =="
mutate agent_core.py \
    '    verdict = _bash_parse_verdict(command)' \
    '    verdict = None' \
    "$RULE::TestTheGate::test_an_unterminated_here_document_is_refused" \
    "an unterminated here-document is refused"

echo "== the warning of bash is ignored =="
mutate agent_core.py \
    '    if warns_about_a_here_document:' \
    '    if False:' \
    "$RULE::TestTheGate::test_an_unterminated_here_document_is_refused" \
    "the here-document warning is taken seriously"

echo "== the stuck shell is not interrupted =="
mutate agent_api.py \
    '    await _tmux("send-keys", "-t", name, "C-c")' \
    '    pass  # the shell keeps waiting' \
    "$RULE::TestTheStuckShellIsFreed::test_a_stuck_shell_is_interrupted_and_the_answer_says_so" \
    "the stuck shell is interrupted"

echo "== a busy session is interrupted too =="
mutate agent_api.py \
    '    if not await _pane_is_waiting_for_input(name, protocol_ran=protocol_ran):
        return False' \
    '    if False:
        return False' \
    "$RULE::TestTheStuckShellIsFreed::test_a_busy_session_is_not_interrupted" \
    "a busy session is not interrupted"

echo "== any line counts as a continuation prompt =="
mutate agent_api.py \
    '    if is_a_continuation_prompt(tail[-1]):
        return True' \
    '    if True:
        return True' \
    "$RULE::TestTheStuckShellIsFreed::test_a_running_command_whose_output_ends_with_a_dollar_is_not_interrupted" \
    "a running command is not mistaken for a waiting shell"

echo "== the interrupt is never checked for having worked =="
mutate agent_api.py \
    '    return looks_like_a_bare_prompt(tail[-1])' \
    '    return True' \
    "$RULE::TestTheStuckShellIsFreed::test_an_interrupt_that_frees_nothing_is_not_reported_as_a_reset" \
    "an interrupt that frees nothing is not reported as a reset"

echo "== the protocol stops installing the marker =="
mutate agent_core.py \
    '; PS2=\"{CONTINUATION_PROMPT} \"' \
    '; PS2=\"$PS1\"' \
    "$RULE::TestTheProtocolIsTheClaim::test_the_protocol_installs_the_marker" \
    "the protocol installs the marker"

echo "== the unit stops naming the env file =="
mutate agent-stack.service.example \
    'Environment=COMPOSE_ENV_FILE=/home/git/agent-stack/.env' \
    '# the env file is not named' \
    "$RULE::TestTheChainIsWhole::test_the_unit_names_the_env_file" \
    "the unit names the env file"

echo "== the documentation stops naming the trap =="
mutate docs/CONFIGURATION.md \
    'the session is busy' \
    'the session is occupied' \
    "$RULE::TestTheChainIsWhole::test_the_documentation_says_the_limit_is_the_deployments" \
    "the documentation names the trap"

echo "== the shipped default moves on its own =="
mutate proxy_core.py \
    'EXEC_TIMEOUT_SECONDS = 60.0' \
    'EXEC_TIMEOUT_SECONDS = 3600.0' \
    "$RULE::TestTheChainIsWhole::test_the_shipped_default_is_unchanged" \
    "the shipped default is the deployment's to raise"

echo
echo "mutations: $PASS went red as expected, $FAIL did not"
[ "$FAIL" -eq 0 ] || exit 1
