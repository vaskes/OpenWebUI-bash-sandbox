#!/usr/bin/env bash
# The workspace volume has to be a bind of the workspace.
#
# Docker does not change a volume's options once the volume exists. A
# volume left over from an earlier stack keeps the driver it was made
# with, and a plain local volume is a directory in Docker's own
# directory, on the host's partition, with no ceiling on it -- which is
# exactly the hole the workspace's filesystem closes.
#
# So the volume is checked, and a deployment that finds a volume which
# is not the workspace says so instead of starting a stack whose
# ceiling is a promise.
#
#   check-workspace-volume.sh [--remove] <workspace-path> [project]
#
# The project is the compose project the deployment belongs to. It
# matters: the host can carry more than one deployment, they use the
# same logical volume name, and a check that reaches into another
# deployment's volume takes away a volume that was exactly right.
#
# --remove takes the offending volumes away. The containers that use a
# volume have to be down first: Docker refuses to remove a volume that
# is in use, and that refusal is the right one.
#
# The volumes are read from docker, and the devices from docker's own
# answer about them. Both are read without changing anything, so the
# check is safe to run at any time -- and it needs no root, which is
# the point: the check is the thing that says "the ceiling is a fact",
# and a check that needs root is a check nobody runs.
set -uo pipefail

REMOVE=0
if [ "${1:-}" = "--remove" ]; then
    REMOVE=1
    shift
fi

WS_PATH="${1:?usage: check-workspace-volume.sh [--remove] <workspace-path>}"
LOGICAL=owbs-workspace

if ! command -v docker >/dev/null 2>&1; then
    echo "check-workspace-volume: docker is not installed." >&2
    exit 2
fi

PROJECT="${2:-}"
if [ -n "$PROJECT" ]; then
    volumes="$(docker volume ls -q \
        --filter "label=com.docker.compose.volume=${LOGICAL}" \
        --filter "label=com.docker.compose.project=${PROJECT}" 2>/dev/null || true)"
else
    volumes="$(docker volume ls -q --filter "label=com.docker.compose.volume=${LOGICAL}" 2>/dev/null || true)"
fi
if [ -z "$volumes" ]; then
    # No volume yet: compose makes it when the stack starts, with the
    # driver the compose file asks for. Nothing to check.
    echo "check-workspace-volume: there is no workspace volume yet."
    exit 0
fi

bad=0
for volume in $volumes; do
    # The keys of the options map are lower case ("device", "o", "type"),
    # and the raw map is the fallback: a template that does not resolve
    # prints "<no value>" or nothing at all, and both look like a volume
    # that binds nothing -- which would send the deployment after a
    # volume that is exactly right.
    device="$(docker volume inspect "$volume" --format '{{index .Options "device"}}' 2>/dev/null || echo "")"
    options="$(docker volume inspect "$volume" --format '{{.Options}}' 2>/dev/null || echo "")"
    if [ "$device" = "$WS_PATH" ] || printf '%s' "$options" | grep -qF "device:$WS_PATH"; then
        echo "check-workspace-volume: $volume binds $WS_PATH."
        continue
    fi
    bad=1
    echo "check-workspace-volume: $volume is NOT the workspace (device: '${device:-a plain directory}')." >&2
    if [ "$REMOVE" -eq 1 ]; then
        if docker volume rm "$volume" >/dev/null 2>&1; then
            echo "check-workspace-volume: $volume removed -- compose will make it again, with the driver the compose file asks for."
        else
            echo "check-workspace-volume: $volume could not be removed (is a container still using it?)." >&2
        fi
    fi
done

if [ "$bad" -ne 0 ]; then
    echo "check-workspace-volume: a volume that is not the workspace has no ceiling." >&2
    exit 1
fi

echo "check-workspace-volume: every workspace volume binds $WS_PATH."
exit 0
