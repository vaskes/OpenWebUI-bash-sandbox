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

# THE CEILING HAS TO BE A FACT, NOT A PATH.
#
# A volume that binds the workspace path is not yet a workspace with a
# ceiling: the path is only the workspace while a FILESYSTEM is mounted
# on it. When the mount is gone the path is a plain directory on the
# host's disk, the volume still binds it, and the container starts on
# the host's partition -- 1.8 TB instead of the 4 GB that were
# promised. The agent notices and refuses to start, which is the
# ceiling check doing its job, but by then the stack is down: the
# deployment took the service away to find out.
#
# So the mount is checked HERE, while the stack is still up and the
# deployment can stop before anything is started. A deployment that
# finds the workspace unmounted says so and stops.
if [ ! -e "$WS_PATH" ]; then
    # Nothing is there yet: the deployment makes the filesystem before
    # the stack starts, and there is nothing to check until then.
    echo "check-workspace-volume: $WS_PATH does not exist yet."
    WS_SRC=""
    WS_FS=""
elif [ ! -d "$WS_PATH" ]; then
    echo "check-workspace-volume: $WS_PATH is not a directory." >&2
    exit 1
else
    WS_SRC="$(findmnt -no SOURCE "$WS_PATH" 2>/dev/null || true)"
    WS_FS="$(findmnt -no FSTYPE "$WS_PATH" 2>/dev/null || true)"
fi
if [ -e "$WS_PATH" ] && { [ -z "$WS_SRC" ] || [ "$WS_FS" != "ext4" ]; }; then
    echo "check-workspace-volume: $WS_PATH is not an ext4 filesystem." >&2
    echo "check-workspace-volume:   source: '${WS_SRC:-nothing is mounted there}'" >&2
    echo "check-workspace-volume:   type:   '${WS_FS:-nothing is mounted there}'" >&2
    echo "check-workspace-volume: a volume that binds a plain directory has no ceiling." >&2
    echo "check-workspace-volume: run tools/setup-workspace.sh $WS_PATH first." >&2
    exit 1
fi
echo "check-workspace-volume: the workspace is $WS_SRC ($WS_FS)."


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
