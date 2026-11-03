#!/usr/bin/env bash
# Mutation check for the session registry lock and the exec answer
# contract.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties this wave pins:
#
#   * the create path of one session id is serialized — two requests for
#     the same id must not create two tmux sessions, and the registry
#     must not end up naming a directory the pane is not running in;
#   * the registry is written under the registry lock;
#   * the listing answers from its own snapshot — a registry that
#     changes underneath it does not leak into the answer;
#   * the persist path copies the registry under the lock and dumps the
#     **copy** — dumping the live dict in a worker thread is the
#     "dictionary changed size during iteration" crash;
#   * the creation lock of a session that is in use is not dropped;
#   * every exec answer says whether the command started, and the
#     contract is written down where a caller reads it.
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

TESTS=tests/test_session_lock_v2260.py

# The tree has to be green before anything is broken: a mutation sweep
# against a red tree reports catches for a tree nobody broke.
mutation_require_green "$TESTS"

echo "== two requests for one id create two sessions =="
mutate agent_api.py \
  '    async with lock:
        return await _create_or_adapt_session(sid, name, requested)' \
  '    return await _create_or_adapt_session(sid, name, requested)' \
  "$TESTS::TestTheCreatePathIsSerialized::test_only_one_tmux_session_is_created" \
  "the create path of one id is not serialized"

echo "== the registry is written without the lock =="
mutate agent_api.py \
  '    async with SESSIONS_LOCK:
        SESSIONS[sid] = {"tmux_name": name, "created": time.time(), "cwd": cwd}' \
  '    if True:
        SESSIONS[sid] = {"tmux_name": name, "created": time.time(), "cwd": cwd}' \
  "$TESTS::TestTheCreatePathIsSerialized::test_the_registry_is_written_under_the_lock" \
  "the create path writes the registry without the lock"

echo "== the listing answers from the live registry =="
mutate agent_api.py \
  '        known = list(SESSIONS.items())' \
  '        known = SESSIONS.items()' \
  "$TESTS::TestTheRegistryIsNeverWalkedWhileItChanges::test_the_listing_answers_from_its_own_snapshot" \
  "the listing holds a live view of the registry instead of a snapshot"

echo "== the answer is built from the live registry =="
mutate agent_api.py \
  '            for sid, info in known' \
  '            for sid, info in SESSIONS.items()' \
  "$TESTS::TestTheRegistryIsNeverWalkedWhileItChanges::test_the_listing_answers_from_its_own_snapshot" \
  "the answer is read out of the live registry"

echo "== the persist path copies the registry without the lock =="
mutate agent_api.py \
  '    async with SESSIONS_LOCK:
        snapshot = dict(SESSIONS)' \
  '    if True:
        snapshot = dict(SESSIONS)' \
  "$TESTS::TestTheRegistryIsNeverWalkedWhileItChanges::test_the_persist_path_takes_the_registry_lock" \
  "the persist path copies the registry without taking the lock"

echo "== the persist path dumps the live registry =="
mutate agent_api.py \
  '            json.dump(snapshot, f)' \
  '            json.dump(SESSIONS, f)' \
  "$TESTS::TestTheRegistryIsNeverWalkedWhileItChanges::test_the_persisted_registry_is_a_copy" \
  "the live registry is dumped — a change to it during the write crashes the dump"

echo "== the listing reads the registry without the lock =="
mutate agent_api.py \
  '    async with SESSIONS_LOCK:
        known = list(SESSIONS.items())' \
  '    if True:
        known = list(SESSIONS.items())' \
  "$TESTS::TestTheRegistryIsNeverWalkedWhileItChanges::test_the_listing_takes_the_registry_lock" \
  "the listing reads the registry without taking the lock"

echo "== the registry has no lock at all =="
mutate agent_api.py \
  'SESSIONS_LOCK = asyncio.Lock()' \
  'SESSIONS_LOCK = None' \
  "$TESTS::TestTheRegistryIsNeverWalkedWhileItChanges::test_the_registry_has_a_lock" \
  "the registry is not guarded by a lock"

echo "== a creation lock in use is dropped =="
mutate agent_api.py \
  '    if lock is not None and not lock.locked():' \
  '    if lock is not None:' \
  "$TESTS::TestTheCreationLockIsNotDroppedWhileItIsHeld::test_a_held_creation_lock_is_not_dropped" \
  "a creation lock that is in use is dropped — a second request gets a fresh one"

echo "== the busy answer does not say the command did not start =="
mutate agent_api.py \
  '                    "stderr": _BUSY_NOTICE,
                    "exit_code": None,
                    "timed_out": True,
                    "duration_ms": duration_ms,
                    "command_started": False,' \
  '                    "stderr": _BUSY_NOTICE,
                    "exit_code": None,
                    "timed_out": True,
                    "duration_ms": duration_ms,' \
  "$TESTS::TestTheExecAnswerContract::test_a_busy_session_says_the_command_did_not_start" \
  "a command that never started says nothing about it"

echo "== the stuck answer does not say the command did not start =="
mutate agent_api.py \
  '                        "stderr": _STUCK_NOTICE,
                        "exit_code": None,
                        "timed_out": True,
                        "duration_ms": duration_ms,
                        "command_started": False,' \
  '                        "stderr": _STUCK_NOTICE,
                        "exit_code": None,
                        "timed_out": True,
                        "duration_ms": duration_ms,' \
  "$TESTS::TestTheExecAnswerContract::test_every_answer_that_reports_a_timeout_also_reports_the_start" \
  "a command that never started says nothing about it"

echo "== an answer that ran says nothing about it =="
mutate agent_api.py \
  '            "command_started": True,' \
  '' \
  "$TESTS::TestTheExecAnswerContract::test_every_answer_that_reports_a_timeout_also_reports_the_start" \
  "an answer that reports a timeout does not report whether the command started"

echo "== the contract is not written down =="
mutate docs/HANDOFF-mcp.md \
  '`command_started`, `session_id` and `cwd`.' \
  '`session_id` and `cwd`.' \
  "$TESTS::TestTheExecAnswerContract::test_the_contract_is_documented" \
  "the exec answer contract is not in the handoff"

echo "== the registry lock is not in the architecture =="
mutate docs/ARCHITECTURE.md \
  '**`SESSIONS_LOCK`**' \
  '**`SESSIONS`**' \
  "$TESTS::TestTheExecAnswerContract::test_the_contract_is_documented" \
  "the registry lock is not described"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
