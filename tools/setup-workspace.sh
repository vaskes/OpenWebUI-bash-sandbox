#!/usr/bin/env bash
# Give the sandbox a workspace with a hard ceiling, on disk.
#
#   setup-workspace.sh <mountpoint> [size]
#   setup-workspace.sh --check <mountpoint> [size]
#
# The workspace is an ext4 filesystem living in a file of a fixed size,
# mounted on the host and handed to the container as a volume. The file
# is sparse: it costs what the workspace actually holds, and it cannot
# grow past the ceiling -- the filesystem inside it runs out of space
# and the write fails with ENOSPC.
#
# WHY A FILESYSTEM IN A FILE
#
# Docker has no quota for a volume. The `local` driver stores a volume
# as a directory under /var/lib/docker/volumes, on the host's own
# partition, and there is no knob that caps it: compose has no
# per-volume storage option, and `--storage-opt size` only ever worked
# with the devicemapper storage driver, which Docker no longer ships
# (this host runs overlayfs). A named volume is therefore a hole in the
# disk by construction.
#
# The ceiling has to come from the filesystem, and the only filesystem
# that can be sized exactly is one we make ourselves. That is what this
# script does, and it is why the workspace is a mount point rather than
# a directory.
#
# A named volume whose driver binds a host path is how that mount point
# reaches the container. Docker cannot cap a volume, so it does not
# pretend to: the ceiling is the filesystem's, and this script is what
# makes the filesystem.
#
# THE SIZE CHANGES THE WORKSPACE
#
# The ceiling is the size of the image file. Changing it means a new
# file and a new filesystem -- the old workspace is gone. The workspace
# is a scratchpad, so that is the intended behaviour, but it is
# announced loudly instead of done quietly.
#
# THE MODE OF THE SCRATCHPAD
#
# The filesystem's root directory is mode 1777, exactly like the tmpfs
# this workspace replaced. It is not a detail: the agent's user writes
# there, and so does the root the privilege mode hands out -- and a
# container that drops every capability has no CAP_DAC_OVERRIDE, so
# that root cannot write into a directory it does not own. A 755
# directory owned by the agent's user is a scratchpad that the root
# cannot use, and the CI found it: the deploy job's file-door step
# asked the container's root to create a file, and the file was not
# there afterwards.
#
# The sticky bit keeps the tmpfs's semantics: everybody may create and
# delete their own files, and nobody may delete somebody else's.
#
# THE CEILING HAS TO HOLD WHAT THE DOOR ACCEPTS
#
# The file door accepts a file up to AGENT_BINARY_LIMIT and writes it
# into the workspace. A ceiling below the policy refuses a file the
# door already promised to accept, and the refusal would come from the
# wrong side: the client would be told "no space" for a file that was
# in range. So the script compares the ceiling with the policy and
# refuses to make a workspace that cannot hold it.
#
# --check DOES NOT NEED ROOT
#
# It only reads: the mount table, the loop table and the image file. A
# deployment can therefore prove its own ceiling without privileges --
# which is the point: the check is the thing that says "the ceiling is
# a fact", and a check that needs root is a check nobody runs.
#
# WHAT THIS DOES NOT BOUND
#
# The container's own writable layer is on the host's partition and is
# not capped: overlayfs has no size option, and a container whose root
# filesystem is writable can grow there without limit. That is the
# price of a root that can really write, and it is the operator's call.
# The workspace -- where the agent's files actually live -- is bounded.
set -euo pipefail

CHECK_ONLY=0
if [ "${1:-}" = "--check" ]; then
    CHECK_ONLY=1
    shift
fi

MOUNTPOINT="${1:?usage: setup-workspace.sh [--check] <mountpoint> [size]}"
SIZE="${2:-${AGENT_WORKSPACE_SIZE:-1g}}"
# The mode of the workspace's root directory. 1777, like the tmpfs this
# workspace replaced: see the header.
WORKSPACE_MODE=1777
IMAGE="${WORKSPACE_IMAGE:-${MOUNTPOINT%/}.img}"
POLICY="${AGENT_BINARY_LIMIT:-0}"

for tool in truncate mkfs.ext4 mount findmnt losetup df stat; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "setup-workspace: $tool is not installed." >&2
        exit 1
    }
done

# The same 1024-based reading of a size that `truncate -s` uses, so the
# number compared below is the number the file actually gets.
size_to_bytes() {
    local s="$1" unit
    case "$s" in
        *[Kk]) unit=1024; s="${s%[Kk]}" ;;
        *[Mm]) unit=$((1024 * 1024)); s="${s%[Mm]}" ;;
        *[Gg]) unit=$((1024 * 1024 * 1024)); s="${s%[Gg]}" ;;
        *[Tt]) unit=$((1024 * 1024 * 1024 * 1024)); s="${s%[Tt]}" ;;
        *) unit=1 ;;
    esac
    case "$s" in
        '' | *[!0-9.]*) return 1 ;;
    esac
    echo $(( ${s%.*} * unit ))
}

want_bytes="$(size_to_bytes "$SIZE" || true)"
if [ -z "$want_bytes" ] || [ "$want_bytes" -le 0 ]; then
    echo "setup-workspace: cannot read the size '$SIZE'." >&2
    exit 1
fi

# The loop device that carries our image, if any.
loop_device_for_image() {
    losetup --list --noheadings --output NAME,BACK-FILE 2>/dev/null |
        awk -v img="$IMAGE" '
            {
                dev = $1; file = $2
                for (i = 3; i <= NF; i++) { file = file " " $i }
                if (file == img) { print dev; exit }
            }'
}

# The source device mounted at the mount point, if any.
mounted_source() {
    findmnt -no SOURCE --target "$MOUNTPOINT" 2>/dev/null || true
}

# The mode of the workspace's root directory, as it is right now.
workspace_mode() {
    stat -c %a "$MOUNTPOINT" 2>/dev/null || echo 0
}

# The bytes the workspace can actually hold right now. A filesystem of
# N bytes holds a little less than N: ext4 keeps a reserve for root.
usable_bytes() {
    df --output=avail --block-size=1 "$1" 2>/dev/null | tail -1 | tr -d ' '
}

report() {
    df -h --output=source,size,avail,target "$MOUNTPOINT" | tail -1
}

check_policy() {
    # The policy is optional: a deployment that does not publish one
    # (the CI job) has nothing to compare against.
    [ "$POLICY" -gt 0 ] 2>/dev/null || return 0
    local have
    have="$(usable_bytes "$MOUNTPOINT" || echo 0)"
    if [ "${have:-0}" -lt "$POLICY" ]; then
        echo "setup-workspace: the workspace holds $have bytes, less than the" >&2
        echo "setup-workspace: file policy ($POLICY bytes) the door accepts -- a" >&2
        echo "setup-workspace: file in range would not land. Raise the ceiling." >&2
        return 1
    fi
    return 0
}

# Is the workspace mounted, and is it ours?
#
# Three facts, all readable without root: the mount point carries a
# mount, the source of that mount is the loop device that carries our
# image, and the image file is the size we were asked for.
workspace_state() {
    local src dev have
    src="$(mounted_source)"
    if [ -z "$src" ]; then
        echo "unmounted"
        return 0
    fi
    dev="$(loop_device_for_image)"
    if [ -z "$dev" ] || [ "$dev" != "$src" ]; then
        echo "foreign:$src"
        return 0
    fi
    have="$(stat -c %s "$IMAGE" 2>/dev/null || echo 0)"
    echo "ours:$have"
}

state="$(workspace_state)"
kind="${state%%:*}"
have_bytes="${state##*:}"

case "$kind" in
    foreign)
        echo "setup-workspace: $MOUNTPOINT is already mounted from ${state#foreign:}," >&2
        echo "setup-workspace: which is not our image ($IMAGE);" >&2
        echo "setup-workspace: refusing to mount the workspace over it." >&2
        exit 1
        ;;
    ours)
        if [ "$have_bytes" = "$want_bytes" ]; then
            check_policy || exit 1
            mode="$(workspace_mode)"
            if [ "$CHECK_ONLY" -eq 1 ]; then
                if [ "$mode" != "$WORKSPACE_MODE" ]; then
                    echo "setup-workspace: $MOUNTPOINT is the workspace filesystem," >&2
                    echo "setup-workspace: but its mode is $mode, not $WORKSPACE_MODE;" >&2
                    echo "setup-workspace: a root without capabilities cannot write" >&2
                    echo "setup-workspace: into a directory it does not own." >&2
                    exit 1
                fi
                echo "setup-workspace: $MOUNTPOINT is the workspace filesystem ($SIZE)."
                report
                exit 0
            fi
            # The mode is part of the scratchpad's contract, and anything
            # else may have changed it -- a chmod, a filesystem restored
            # from an image, another tool. Put it back before saying the
            # workspace is ready.
            if [ "$mode" != "$WORKSPACE_MODE" ]; then
                echo "setup-workspace: the mode of $MOUNTPOINT was $mode; setting it to $WORKSPACE_MODE."
                chmod "$WORKSPACE_MODE" "$MOUNTPOINT" || {
                    echo "setup-workspace: cannot set the mode of $MOUNTPOINT." >&2
                    echo "setup-workspace: it takes root, or the ownership of the mount point." >&2
                    exit 1
                }
            fi
            echo "setup-workspace: $MOUNTPOINT is the workspace filesystem ($SIZE)."
            report
            exit 0
        fi
        if [ "$CHECK_ONLY" -eq 1 ]; then
            echo "setup-workspace: $MOUNTPOINT holds a $have_bytes byte filesystem;" >&2
            echo "setup-workspace: the ceiling asked for is $want_bytes bytes." >&2
            exit 1
        fi
        echo "setup-workspace: the ceiling changed ($have_bytes -> $want_bytes bytes);" >&2
        echo "setup-workspace: the old workspace is wiped and a new one is made." >&2
        umount "$MOUNTPOINT"
        ;;
    unmounted)
        if [ "$CHECK_ONLY" -eq 1 ]; then
            echo "setup-workspace: $MOUNTPOINT is not mounted at all." >&2
            exit 1
        fi
        ;;
esac

if [ "$CHECK_ONLY" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    echo "setup-workspace: this mounts a filesystem; it needs root." >&2
    exit 1
fi

mkdir -p "$MOUNTPOINT"

if [ -e "$IMAGE" ]; then
    have_bytes="$(stat -c %s "$IMAGE")"
    if [ "$have_bytes" != "$want_bytes" ]; then
        echo "setup-workspace: the ceiling changed ($have_bytes -> $want_bytes bytes);" >&2
        echo "setup-workspace: the old workspace is wiped and a new one is made." >&2
        rm -f "$IMAGE"
    fi
fi

if [ ! -e "$IMAGE" ]; then
    echo "setup-workspace: making a $SIZE filesystem in $IMAGE"
    truncate -s "$SIZE" "$IMAGE"
    mkfs.ext4 -F -q -E root_owner=1000:1000 "$IMAGE"
fi

echo "setup-workspace: mounting $IMAGE on $MOUNTPOINT"
mount -o loop "$IMAGE" "$MOUNTPOINT"

# The mode the tmpfs used to have. The agent's user writes here, and so
# does the root the privilege mode hands out -- and a container that
# drops every capability cannot write into a directory owned by
# somebody else.
chmod "$WORKSPACE_MODE" "$MOUNTPOINT"

check_policy || exit 1
report
