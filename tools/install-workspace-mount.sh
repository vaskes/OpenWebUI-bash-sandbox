#!/usr/bin/env bash
# Make the workspace's filesystem survive a reboot of the host.
#
#   install-workspace-mount.sh [--check|--remove] <mountpoint> [image]
#
# WHY THIS EXISTS
#
# The workspace is an ext4 filesystem in a file, mounted on the host and
# handed to the container as a volume. The mount is a fact about the
# host's running kernel, not about the file: after a reboot the file is
# still there and the filesystem inside it is intact, but nothing is
# mounted, so the directory the volume binds is an ordinary directory
# on the host's partition again.
#
# That is the failure this script exists to prevent. It is quiet: the
# container starts, the volume binds a plain directory, and the
# workspace is empty -- and, worse, the ceiling is gone, because a
# directory on the host's disk has no ceiling. The agent would write
# until the host's disk is full, which is the attack the filesystem in
# a file was built to close.
#
# The fix is an entry in /etc/fstab: systemd mounts it during boot,
# before the containers come up (the mounts are part of local-fs.target
# and the containers start after it), so every container start finds
# the filesystem there.
#
# WHY nofail
#
# A missing image must not stop the host from booting. With `nofail`
# the mount is skipped and the boot continues -- and the workspace is
# then missing, which the deployment's own check reports loudly
# (tools/setup-workspace.sh --check) instead of the host refusing to
# come up at all. A host that does not boot helps nobody; a workspace
# that is missing is a loud, fixable problem.
#
# WHY nosuid AND nodev
#
# They are the same boundary the mount itself carries: the workspace
# root is mode 1777 on the host's disk, and in the privilege mode the
# agent can become uid 0 inside the container -- which is uid 0 on the
# host, because no user namespace is remapping it. A file in the
# workspace with the setuid bit is a setuid-root file on the host
# without nosuid, and a device node in it is a device node on the
# host without nodev. The entry in fstab has to carry them too: an
# entry that mounts the same filesystem without them is a different
# boundary, and it is the one that would be in force after a reboot.
set -euo pipefail

MODE=install
case "${1:-}" in
    --check) MODE=check; shift ;;
    --remove) MODE=remove; shift ;;
esac

MOUNTPOINT="${1:?usage: install-workspace-mount.sh [--check|--remove] <mountpoint> [image]}"
MOUNTPOINT="$(realpath -m "$MOUNTPOINT")"
IMAGE="${2:-${MOUNTPOINT%/}.img}"
# The file the entry is written to. It is a parameter so that a test
# can point it at a scratch file: writing to /etc/fstab is a root
# operation, and a check that needs root is a check nobody runs.
FSTAB="${FSTAB:-/etc/fstab}"

# The options the mount carries. The same ones tools/setup-workspace.sh
# mounts with, plus what systemd needs to mount a file as a filesystem
# at boot.
FSTAB_OPTIONS="loop,nosuid,nodev,nofail,x-systemd.device-timeout=30"

fstab_line() {
    printf '%s %s ext4 %s 0 0\n' "$IMAGE" "$MOUNTPOINT" "$FSTAB_OPTIONS"
}

# The entry for this mount point, if there is one. The mount point is
# the second field: the image is the first, and the options are the
# fourth.
entry_for() {
    awk -v mp="$MOUNTPOINT" '$2 == mp { print; exit }' "$FSTAB" 2>/dev/null || true
}

case "$MODE" in
    check)
        line="$(entry_for)"
        if [ -z "$line" ]; then
            echo "install-workspace-mount: $FSTAB has no entry for $MOUNTPOINT." >&2
            echo "install-workspace-mount: after a reboot the workspace would not be" >&2
            echo "install-workspace-mount: mounted, and the volume would bind a plain" >&2
            echo "install-workspace-mount: directory -- no ceiling. Run this script" >&2
            echo "install-workspace-mount: without --check to write the entry." >&2
            exit 1
        fi
        want="$(fstab_line)"
        if [ "$line" != "$want" ]; then
            echo "install-workspace-mount: the entry for $MOUNTPOINT is:" >&2
            echo "install-workspace-mount:   $line" >&2
            echo "install-workspace-mount: and it should be:" >&2
            echo "install-workspace-mount:   $want" >&2
            exit 1
        fi
        # The entry is what makes the mount happen at boot. The mount
        # being up right now is a second fact, and the deployment's
        # own check (setup-workspace.sh --check) reads it.
        echo "install-workspace-mount: $MOUNTPOINT is in $FSTAB: $line"
        exit 0
        ;;
    remove)
        line="$(entry_for)"
        if [ -z "$line" ]; then
            echo "install-workspace-mount: $FSTAB has no entry for $MOUNTPOINT; nothing to do."
            exit 0
        fi
        tmp="$(mktemp)"
        grep -v -F " $MOUNTPOINT " "$FSTAB" >"$tmp" 2>/dev/null || true
        cat "$tmp" >"$FSTAB"
        rm -f "$tmp"
        echo "install-workspace-mount: the entry for $MOUNTPOINT is removed from $FSTAB."
        exit 0
        ;;
esac

# install: one entry for this mount point, and nothing else touched.
line="$(entry_for)"
if [ "$line" = "$(fstab_line)" ]; then
    echo "install-workspace-mount: $MOUNTPOINT is already in $FSTAB."
    exit 0
fi

tmp="$(mktemp)"
if [ -f "$FSTAB" ]; then
    grep -v -F " $MOUNTPOINT " "$FSTAB" >"$tmp" 2>/dev/null || true
else
    : >"$tmp"
fi
printf '%s\n' "$(fstab_line)" >>"$tmp"
cat "$tmp" >"$FSTAB"
rm -f "$tmp"
echo "install-workspace-mount: $MOUNTPOINT is in $FSTAB now:"
echo "install-workspace-mount:   $(fstab_line)"
