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
# THE CEILING GROWS THE WORKSPACE; IT NEVER WIPES IT
#
# The ceiling is the size of the image file, and the image file is the
# disk of the filesystem inside it. A bigger ceiling is therefore not a
# new disk: it is the same disk with more room, and the filesystem on
# it can be grown to match (truncate the file, tell the loop device
# the file is bigger, resize2fs). Growing keeps the files.
#
# Making a new filesystem does not keep the files, and the workspace is
# the one place where the agent's work lives: a scratchpad that is
# empty when the agent comes back is not a scratchpad, it is a bug
# that eats the work. So a size change grows the workspace and says so.
#
# A smaller ceiling is REFUSED. Shrinking a filesystem means deciding
# what to throw away, and that decision belongs to a human, not to a
# deployment that runs unattended.
#
# The size is a parameter (AGENT_WORKSPACE_SIZE, or the second
# argument) and not a constant in the code: the host's disk is the
# narrow one, and a ceiling that is right on an empty disk is a bomb
# on a full one.
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
PLAN_ONLY=0
if [ "${1:-}" = "--check" ]; then
    CHECK_ONLY=1
    shift
fi
if [ "${1:-}" = "--plan" ]; then
    # --plan answers one question: what would this script do? It reads
    # the state and prints one word -- create, same, grow, refuse or
    # foreign -- and changes nothing. It needs no root, because the
    # decision is a decision: the plan and the doing are the same
    # function, so a test that asks the question is testing the thing
    # that acts.
    PLAN_ONLY=1
    shift
fi

MOUNTPOINT="${1:?usage: setup-workspace.sh [--check|--plan] <mountpoint> [size]}"
# The canonical form of the path: findmnt answers with canonical
# targets, and a path with a trailing slash or a symlink in it would
# never equal them.
MOUNTPOINT_CANON="$(realpath -m "$MOUNTPOINT" 2>/dev/null || echo "$MOUNTPOINT")"
SIZE="${2:-${AGENT_WORKSPACE_SIZE:-4g}}"
# The mode of the workspace's root directory. 1777, like the tmpfs this
# workspace replaced: see the header.
WORKSPACE_MODE=1777
IMAGE="${WORKSPACE_IMAGE:-${MOUNTPOINT%/}.img}"
POLICY="${AGENT_BINARY_LIMIT:-0}"

for tool in truncate mkfs.ext4 mount findmnt losetup df stat realpath resize2fs e2fsck dumpe2fs; do
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
        *.*)
            # A fractional size is refused, not rounded. The number the
            # plan compares is the number the file gets, and two numbers
            # that only look alike are how a ceiling that was asked for
            # quietly becomes a different one.
            echo "setup-workspace: the size '$1' is fractional; use a whole" >&2
            echo "setup-workspace: number of bytes (1500m instead of 1.5g)." >&2
            return 1
            ;;
    esac
    echo $(( s * unit ))
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
#
# findmnt answers with the nearest mount that CONTAINS the path, so a
# plain directory inside a mounted filesystem gets that filesystem too --
# and the workspace's directory is exactly that: it lives on the host's
# partition, and findmnt would have called it "already mounted from
# /dev/nvme0n1p4". The workspace is mounted only when the path IS the
# target of the mount.
mounted_source() {
    local target
    target="$(findmnt -no TARGET --target "$MOUNTPOINT" 2>/dev/null || true)"
    [ "$target" = "$MOUNTPOINT_CANON" ] || return 0
    findmnt -no SOURCE --target "$MOUNTPOINT" 2>/dev/null || true
}

# The mode of the workspace's root directory, as it is right now.
workspace_mode() {
    stat -c %a "$MOUNTPOINT" 2>/dev/null || echo 0
}

# The options the workspace is mounted with.
#
# nosuid and nodev are not decoration. The workspace is a 1777
# directory on the host's disk, and in the privilege mode the agent
# can become uid 0 inside the container -- which is uid 0 on the
# host, because no user namespace is remapping it. Without nosuid, a
# binary dropped in the workspace with the setuid bit set is a
# setuid-root file that lives on the host, and whoever runs it gets
# the host's root. The tmpfs the workspace used to be carried both
# flags; the filesystem on disk has to carry them too, or the move
# to the disk quietly took the boundary away with it.
WORKSPACE_MOUNT_OPTS="loop,nosuid,nodev"

# The options the mount must carry. Read as tokens, not as
# substrings: "nosuidfoo" is not "nosuid".
WORKSPACE_REQUIRED_OPTS="nosuid nodev"

# The options the mount carries right now.
mounted_options() {
    local target
    target="$(findmnt -no TARGET --target "$MOUNTPOINT" 2>/dev/null || true)"
    [ "$target" = "$MOUNTPOINT_CANON" ] || return 0
    findmnt -no OPTIONS --target "$MOUNTPOINT" 2>/dev/null || true
}

# Does the mount carry every option it has to carry?
workspace_mount_is_safe() {
    local opts flag
    # The options stay comma-separated: the separators are the token
    # boundaries. Splitting them and throwing the separators away
    # turns "rw,nosuid,nodev" into "rwnosuidnodev", which is neither
    # the options nor anything that can be compared with them.
    opts=",$(mounted_options | tr -d '[:space:]'),"
    for flag in $WORKSPACE_REQUIRED_OPTS; do
        case "$opts" in
            *",$flag,"*) ;;
            *) return 1 ;;
        esac
    done
    return 0
}

# The bytes the workspace can actually hold right now. A filesystem of
# N bytes holds a little less than N: ext4 keeps a reserve for root.
usable_bytes() {
    df --output=avail --block-size=1 "$1" 2>/dev/null | tail -1 | tr -d ' '
}

# Grow the workspace's filesystem in place.
#
# $1 -- the size the file has now, in bytes
# $2 -- the size it should have, in bytes
#
# The image file is the disk of the filesystem inside it, so a bigger
# ceiling is the same disk with more room. Three steps, in this order:
# the file grows, the loop device is told the file is bigger, and the
# filesystem is resized to fill the device.
#
# The filesystem is checked before it is resized -- but only when it is
# not in use. e2fsck refuses to check a mounted filesystem ("Cannot
# continue, aborting"), and a mounted filesystem is resized by
# resize2fs itself, which does its own consistency work. So the check
# runs on the offline path, and the online path -- the fs is mounted
# and in use right now -- goes straight to the resize.
#
# The files stay. That is the whole point: the workspace is where the
# agent's work lives, and a deployment that raises the ceiling must not
# be the thing that throws the work away.
grow_workspace() {
    local have="$1" want="$2" dev attached=0 rc
    echo "setup-workspace: the ceiling grows ($have -> $want bytes);" >&2
    echo "setup-workspace: the workspace is grown, not wiped."
    # Before the file is touched: is it a filesystem at all? A file
    # that is not one is not ours to grow -- growing it would change
    # a file that somebody put there, and the workspace would be
    # "repaired" by destroying something that is not the workspace.
    if ! dumpe2fs -h "$IMAGE" >/dev/null 2>&1; then
        echo "setup-workspace: $IMAGE is not a filesystem; it is left untouched." >&2
        echo "setup-workspace: a human decides whether it is to be replaced --" >&2
        echo "setup-workspace: this script does not format over a file it did" >&2
        echo "setup-workspace: not make." >&2
        exit 1
    fi
    truncate -s "$want" "$IMAGE" || {
        echo "setup-workspace: cannot grow $IMAGE to $want bytes." >&2
        exit 1
    }
    dev="$(loop_device_for_image)"
    if [ -z "$dev" ]; then
        dev="$(losetup --find --show "$IMAGE")" || {
            echo "setup-workspace: cannot attach $IMAGE to a loop device." >&2
            exit 1
        }
        attached=1
    else
        # The loop device carries the size the file had. Tell it the
        # file is bigger, or the device stays the old size and the
        # filesystem cannot grow past it.
        losetup -c "$dev" || {
            echo "setup-workspace: cannot tell $dev that its file grew." >&2
            exit 1
        }
    fi
    if [ "$attached" -eq 1 ]; then
        # An offline grow: the filesystem is not mounted, so it can be
        # checked. A check that finds errors it cannot repair is a
        # reason to stop -- resizing a filesystem with errors is how a
        # workspace is lost for good.
        #
        # e2fsck's exit code is not a yes/no: 1 means "errors were
        # corrected" and 2 means "corrected, reboot recommended", and
        # both are fine on a filesystem that is about to be resized.
        # Only 4 and above mean "errors left uncorrected". The code is
        # captured with `|| rc=$?` because the script runs under
        # `set -e`, and a bare call would abort on the benign code 1
        # before the check that matters.
        rc=0
        e2fsck -f -y "$dev" || rc=$?
        if [ "$rc" -ge 4 ]; then
            echo "setup-workspace: the filesystem has errors the check did not repair;" >&2
            echo "setup-workspace: the workspace is left as it is. A human decides." >&2
            losetup -d "$dev" || true
            exit 1
        fi
    fi
    resize2fs "$dev" || {
        echo "setup-workspace: cannot resize the filesystem on $dev." >&2
        exit 1
    }
    if [ "$attached" -eq 1 ]; then
        losetup -d "$dev" || true
    fi
    echo "setup-workspace: the workspace is now $want bytes."
}

# Refuse a smaller ceiling.
#
# Shrinking a filesystem means deciding what to throw away, and the
# decision belongs to a human. The workspace is left exactly as it is:
# the files, the filesystem and the mount all stay.
refuse_smaller_ceiling() {
    local have="$1" want="$2"
    echo "setup-workspace: the workspace holds a $have byte filesystem and the" >&2
    echo "setup-workspace: ceiling asked for is $want bytes -- that is smaller." >&2
    echo "setup-workspace: shrinking is not done here: it would mean throwing" >&2
    echo "setup-workspace: files away, and that is a human's decision. The" >&2
    echo "setup-workspace: workspace is left untouched."
    exit 1
}

report() {
    df -h --output=source,size,avail,target "$MOUNTPOINT" | tail -1
    # The inode limit matters as much as the byte limit: a workspace
    # with plenty of bytes and no inodes left is a workspace that
    # cannot hold a directory of small files, and the failure looks
    # like a full disk. df refuses -i together with --output, so this
    # one is asked for on its own.
    df -i "$MOUNTPOINT" | tail -1
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

# Everything that changes the host needs root: it mounts, it unmounts,
# it resizes a filesystem, it sets a mode. A check does not need root,
# and a refusal does not either -- refusing is reading the state and
# saying no, and the decision to refuse is exactly the one that has to
# be reachable without privileges.
need_root() {
    if [ "$CHECK_ONLY" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
        echo "setup-workspace: this mounts a filesystem; it needs root." >&2
        exit 1
    fi
}

# THE DECISION
#
# One function, one answer: what would this script do with the
# workspace? "create" (there is no file), "same" (the file is the size
# that was asked for), "grow" (the ceiling is bigger, and the workspace
# is grown), "refuse" (the ceiling is smaller, and that is not this
# script's decision to make) or "foreign" (something else is mounted
# there).
#
# It is one function because the plan and the doing must not disagree:
# a --plan that says "grow" and an implementation that wipes would be
# two answers to one question, and the second one is the one that eats
# the workspace.
plan_decision() {
    local have
    if [ "$kind" = "foreign" ]; then
        echo "foreign"
        return 0
    fi
    if [ -e "$IMAGE" ]; then
        have="$(stat -c %s "$IMAGE")"
    else
        have=0
    fi
    if [ "$have" -eq 0 ]; then
        echo "create"
    elif [ "$have" -eq "$want_bytes" ]; then
        echo "same"
    elif [ "$have" -gt "$want_bytes" ]; then
        echo "refuse"
    else
        echo "grow"
    fi
}

# The workspace is mounted, it is ours, and it is the size that was
# asked for. Make sure it is really ready -- the right mode, the
# options the boundary needs -- and say so.
ensure_ready() {
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
        # The mount is ours, but the mount may still be the one
        # an earlier version made -- and an earlier version did not
        # carry nosuid and nodev. The check reads the live mount, not
        # the script: a rule about the filesystem is only true of the
        # filesystem that is there.
        if ! workspace_mount_is_safe; then
            echo "setup-workspace: $MOUNTPOINT is mounted without ${WORKSPACE_REQUIRED_OPTS// /, };" >&2
            echo "setup-workspace: the workspace is a directory any host user can" >&2
            echo "setup-workspace: reach, and without nosuid a binary in it with the" >&2
            echo "setup-workspace: setuid bit is a setuid-root file on the host." >&2
            echo "setup-workspace: run setup-workspace.sh (without --check) to remount it." >&2
            exit 1
        fi
        echo "setup-workspace: $MOUNTPOINT is the workspace filesystem ($SIZE)."
        report
        exit 0
    fi
    need_root
    # The mount may be an older one -- the filesystem does not change
    # when the options do, and the options are what keep the setuid bit
    # from meaning anything. Remounting with the options keeps the
    # workspace and its contents; it is the mount that is put back, not
    # the data.
    if ! workspace_mount_is_safe; then
        echo "setup-workspace: $MOUNTPOINT is mounted without ${WORKSPACE_REQUIRED_OPTS// /, };"
        echo "setup-workspace: remounting it with them."
        mount -o "remount,${WORKSPACE_MOUNT_OPTS#loop,}" "$MOUNTPOINT" || {
            echo "setup-workspace: cannot remount $MOUNTPOINT." >&2
            exit 1
        }
        if ! workspace_mount_is_safe; then
            echo "setup-workspace: $MOUNTPOINT is still mounted without ${WORKSPACE_REQUIRED_OPTS// /, }." >&2
            exit 1
        fi
    fi
    # The mode is part of the scratchpad's contract, and anything else
    # may have changed it -- a chmod, a filesystem restored from an
    # image, another tool. Put it back before saying the workspace is
    # ready.
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
}

decision="$(plan_decision)"

if [ "$PLAN_ONLY" -eq 1 ]; then
    echo "$decision"
    exit 0
fi

case "$decision" in
    foreign)
        echo "setup-workspace: $MOUNTPOINT is already mounted from ${state#foreign:}," >&2
        echo "setup-workspace: which is not our image ($IMAGE);" >&2
        echo "setup-workspace: refusing to mount the workspace over it." >&2
        exit 1
        ;;
    refuse)
        # A smaller ceiling is refused, and the refusal needs no
        # privileges: it is the answer to the question, not a change
        # to the host.
        refuse_smaller_ceiling "$(stat -c %s "$IMAGE")" "$want_bytes"
        ;;
    grow)
        if [ "$CHECK_ONLY" -eq 1 ]; then
            echo "setup-workspace: $MOUNTPOINT holds a $(stat -c %s "$IMAGE") byte filesystem;" >&2
            echo "setup-workspace: the ceiling asked for is $want_bytes bytes." >&2
            exit 1
        fi
        need_root
        grow_workspace "$(stat -c %s "$IMAGE")" "$want_bytes"
        if [ "$kind" = "ours" ]; then
            # The workspace was mounted and in use, so the grow was
            # done online, under the mount that was already there.
            echo "setup-workspace: $MOUNTPOINT is the workspace filesystem ($SIZE)."
            report
            exit 0
        fi
        # Not mounted: the filesystem was grown offline, and the mount
        # is below.
        ;;
    same)
        if [ "$kind" = "ours" ]; then
            ensure_ready
        fi
        # Not mounted: the mount is below.
        ;;
    create)
        if [ "$CHECK_ONLY" -eq 1 ]; then
            echo "setup-workspace: $MOUNTPOINT is not mounted at all." >&2
            exit 1
        fi
        need_root
        ;;
esac

mkdir -p "$MOUNTPOINT"

if [ ! -e "$IMAGE" ]; then
    echo "setup-workspace: making a $SIZE filesystem in $IMAGE"
    # The size is written in bytes, not as the human string: the file
    # has to be exactly the size the plan compared against, or the next
    # run would decide it has to grow (or shrink) a file it made.
    truncate -s "$want_bytes" "$IMAGE"
    mkfs.ext4 -F -q -E root_owner=1000:1000 "$IMAGE"
fi

# The leftovers of an earlier workspace are in the way: the mount hides
# them, and they keep the host's disk busy while they are hidden. The
# workspace is a scratchpad, so they go -- and it is said out loud,
# because a directory that is emptied is worth a word.
if [ -n "$(ls -A "$MOUNTPOINT" 2>/dev/null)" ]; then
    echo "setup-workspace: the directory is not empty; the leftovers of an earlier workspace are removed."
    find "$MOUNTPOINT" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
fi

echo "setup-workspace: mounting $IMAGE on $MOUNTPOINT"
# A file that exists and does not mount is NOT reformatted. Formatting
# it would destroy the workspace without anyone deciding to: the file
# is the only copy of the agent's work, and "the mount failed" is not
# an answer to "is this file worth keeping". Say what happened and
# stop -- a human decides whether the file is to be replaced.
mount -o "$WORKSPACE_MOUNT_OPTS" "$IMAGE" "$MOUNTPOINT" || {
    echo "setup-workspace: $IMAGE exists but does not mount as a filesystem." >&2
    echo "setup-workspace: it is NOT formatted over -- that would destroy the" >&2
    echo "setup-workspace: workspace without anyone deciding to. Check the file" >&2
    echo "setup-workspace: (file -s, dumpe2fs -h) and decide by hand." >&2
    exit 1
}

# The mode the tmpfs used to have. The agent's user writes here, and so
# does the root the privilege mode hands out -- and a container that
# drops every capability cannot write into a directory owned by
# somebody else.
chmod "$WORKSPACE_MODE" "$MOUNTPOINT"

check_policy || exit 1
report
