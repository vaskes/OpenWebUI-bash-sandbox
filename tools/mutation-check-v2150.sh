#!/usr/bin/env bash
# Mutation check for the v2.15.0 tripwires.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property of the new door on purpose; the named test must go
# red. If it stays green, the test is decoration and the property is
# unpinned.
#
# The wave this script belongs to gave the file door its other half:
# `POST /files` puts a file INTO the sandbox. The properties that
# matter there are the same ones the download half has always had --
# nothing is overwritten, the limit is a limit, the perimeter holds --
# plus the one that is new: nothing is left behind.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

RULE_DOOR="tests/test_file_door_upload_v2150.py"

echo "== the upload overwrites the file that is already there =="
mutate agent_api.py \
  '    if os.path.exists(target):
        raise HTTPException(status_code=409, detail=f"file already exists: {name}")' \
  '    if False:
        raise HTTPException(status_code=409, detail=f"file already exists: {name}")' \
  "$RULE_DOOR" \
  "the file that was there is not overwritten"

echo "== the limit is not enforced while the bytes arrive =="
mutate agent_api.py \
  '                if BINARY_LIMIT_BYTES and written > BINARY_LIMIT_BYTES:' \
  '                if False:' \
  "$RULE_DOOR" \
  "a file over the limit is refused"

echo "== the perimeter does not hold for the parent directory =="
mutate agent_api.py \
  '    parent = _safe_parent(_anchor(os.path.dirname(raw) or ".", allow_root=True))' \
  '    parent = _anchor(os.path.dirname(raw) or ".", allow_root=True)' \
  "$RULE_DOOR" \
  "a path outside the workspace is refused"

echo "== the file never lands, and the temporary file stays =="
mutate agent_api.py \
  '        os.replace(tmp_path, target)
        tmp_path = None' \
  '        tmp_path = None' \
  "$RULE_DOOR" \
  "the bytes land and nothing is left behind"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
