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
RULE_LIMITS="tests/test_upload_door_limits_v2150.py"
RULE_DAMAGE="tests/test_damage_surface_v2150.py"

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

echo "== the containment check does not hold =="
# The check that says "this resolved path is inside the workspace".
# Without it, an absolute path outside is simply obeyed -- the door
# writes where it is told to. CodeQL points at this code (the
# py/path-injection alerts on the upload door), and the code is
# guarded; these tests are what makes that guard a fact rather than an
# opinion.
mutate agent_api.py \
  '    if real.startswith(WORKSPACE_PREFIX):
        return real' \
  '    if True:
        return real' \
  "$RULE_DOOR" \
  "the resolved path is checked against the perimeter"

echo "== the file never lands, and the temporary file stays =="
mutate agent_api.py \
  '        os.replace(tmp_path, target)
        tmp_path = None' \
  '        tmp_path = None' \
  "$RULE_DOOR" \
  "the bytes land and nothing is left behind"

echo "== the upload door has no ceiling of its own =="
mutate agent_api.py \
  'app.add_middleware(
    BodySizeLimit,
    limit=MAX_BODY_BYTES,
    overrides={"/files": UPLOAD_MAX_BYTES},
)' \
  'app.add_middleware(
    BodySizeLimit,
    limit=MAX_BODY_BYTES,
)' \
  "$RULE_LIMITS" \
  "a body over two megabytes reaches the upload door"

echo "== the guard answers one ceiling for every path =="
mutate body_limit.py \
  '        return self.limit if best is None else self.overrides[best]' \
  '        return self.limit' \
  "$RULE_LIMITS" \
  "the path decides which ceiling applies"

echo "== the proxy has no way in =="
mutate openapi_proxy.py \
  '@app.post("/files", operation_id="file_upload")' \
  '@app.post("/files-nope", operation_id="file_upload")' \
  "$RULE_LIMITS" \
  "the published door has an upload"

echo "== the proxy buffers the body instead of streaming it =="
mutate openapi_proxy.py \
  '                content=_body_stream(request),' \
  '                content=b"",' \
  "$RULE_LIMITS" \
  "the bytes arrive at the agent unchanged"

echo "== the proxy has no ceiling of its own =="
mutate openapi_proxy.py \
  'app.add_middleware(
    BodySizeLimit,
    limit=MAX_BODY_BYTES,
    overrides={"/files": UPLOAD_MAX_BYTES},
)' \
  'app.add_middleware(
    BodySizeLimit,
    limit=MAX_BODY_BYTES,
)' \
  "$RULE_LIMITS" \
  "a body over two megabytes reaches the agent"

echo "== the refusal changes its wording =="
mutate agent_api.py \
  'detail=f"path outside the workspace: {candidate}"' \
  'detail=f"path outside workspace: {candidate}"' \
  "$RULE_LIMITS" \
  "the documented refusal is the refusal the code produces"

echo "== the root filesystem stops being read-only =="
mutate docker-compose.yml \
  'read_only: "${AGENT_READ_ONLY:-true}"' \
  'read_only: "${AGENT_READ_ONLY:-false}"' \
  "$RULE_DAMAGE" \
  "the root filesystem is read-only by default"

echo "== the privilege mode is on by default =="
mutate docker-compose.yml \
  'AGENT_ALLOW_SUDO: "${AGENT_ALLOW_SUDO:-0}"' \
  'AGENT_ALLOW_SUDO: "${AGENT_ALLOW_SUDO:-1}"' \
  "$RULE_DAMAGE" \
  "sudo is off by default"

echo "== the image ships sudo unconditionally =="
mutate Dockerfile.sandbox \
  '$( [ "$AGENT_ALLOW_SUDO" = "1" ] && echo sudo )' \
  'sudo' \
  "$RULE_DAMAGE" \
  "sudo is in the image only when the privilege mode asks"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
