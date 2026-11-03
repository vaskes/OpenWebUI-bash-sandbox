#!/usr/bin/env bash
# Mutation check for the wave that makes the gates honest.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties this wave pins:
#
#   * the coverage step of CI cannot hide a failing suite: the fallback
#     to the stdlib runner exists for a runner that has NO pytest, and
#     not for a pytest that failed;
#   * the stdlib promise has its own unconditional step — it is the net
#     under the first one;
#   * the lifespan test can actually fail: the helper that runs the
#     shutdown is a plain function (a coroutine that is called and never
#     awaited never runs), and the session survives the startup reaper,
#     so there is something at the shutdown that COULD be killed.
#
# The first mutation is the reviewer's own proof, turned into a permanent
# tripwire: killing every session in the lifespan must turn the suite red.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

TESTS=tests/test_ci_gate_v2270.py
WAVE2=tests/test_wave2_fixes.py

# The tree has to be green before anything is broken: a mutation sweep
# against a red tree reports catches for a tree nobody broke.
mutation_require_green "$TESTS"
mutation_require_green "$WAVE2"

echo "== the lifespan kills the sessions on shutdown =="
mutate agent_api.py \
  '        finally:
            # Best-effort flush so an in-flight session add doesn'"'"'t get lost
            # on a clean restart. Skip if the loop is already shutting down.' \
  '        finally:
            for _sid in list(SESSIONS):
                await _tmux("kill-session", "-t", SESSIONS[_sid].get("tmux_name", ""))
            # Best-effort flush so an in-flight session add doesn'"'"'t get lost
            # on a clean restart. Skip if the loop is already shutting down.' \
  "$WAVE2::TestLifespanNoLongerKillsSessions::test_lifespan_does_not_call_kill_session" \
  "the lifespan kills every session on shutdown"

echo "== the shutdown helper is a coroutine again =="
mutate tests/test_wave2_fixes.py \
  '        def _shutdown():' \
  '        async def _shutdown():' \
  "$WAVE2::TestTheLifespanTestCanActuallyFail::test_the_shutdown_helper_is_not_a_coroutine" \
  "the helper that runs the shutdown is a coroutine — called without await, it never runs"

echo "== the session is reaped before the shutdown =="
mutate tests/test_wave2_fixes.py \
  '        if cmd == "list-sessions":
            return (0, "agent_preserved\n", "")' \
  '        if cmd == "list-sessions":
            return (0, "", "")' \
  "$WAVE2::TestTheLifespanTestCanActuallyFail::test_the_session_survives_to_the_shutdown" \
  "the session is gone before the shutdown — there is nothing to kill and the test is vacuous"

echo "== the coverage step falls back on a failing pytest =="
mutate .github/workflows/ci.yml \
  '          if command -v pytest >/dev/null 2>&1; then
            pytest --cov=. --cov-report=xml --cov-report=term tests/
          else
            coverage run --source=. -m unittest discover -s tests
          fi
          coverage report' \
  '          pytest --cov=. --cov-report=xml --cov-report=term tests/ || \
            coverage run --source=. -m unittest discover -s tests && \
            coverage report' \
  "$TESTS::TestTheCoverageStepCannotHideAFailingSuite::test_the_script_has_no_or_shortcut" \
  "a red pytest is covered by the fallback and the step ends green"

echo "== the fallback is not guarded by pytest's absence =="
mutate .github/workflows/ci.yml \
  '          if command -v pytest >/dev/null 2>&1; then' \
  '          if true; then' \
  "$TESTS::TestTheCoverageStepCannotHideAFailingSuite::test_the_fallback_is_guarded_by_pytests_absence" \
  "the fallback runs on a pytest that failed — the presence check no longer decides"

echo "== the stdlib step became conditional =="
mutate .github/workflows/ci.yml \
  '      - name: The suite also runs on the stdlib' \
  '      - name: The suite also runs on the stdlib
        if: false' \
  "$TESTS::TestTheCoverageStepCannotHideAFailingSuite::test_the_stdlib_promise_has_its_own_unconditional_step" \
  "the net under the first step has a hole in it"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
