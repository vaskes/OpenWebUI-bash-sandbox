#!/usr/bin/env bash
# Is the deployment's privilege posture coherent?
#
#   check-posture.sh
#
# It reads AGENT_ALLOW_SUDO and AGENT_READ_ONLY from the environment and
# decides one thing: is the root the agent can become able to write?
#
# THE RULE
#
# The privilege mode grants sudo. A root that cannot write is a
# half-root: the model gets "Read-only file system" from apt-get and
# from every install step, and calls the sandbox broken. So where sudo
# is on, the root filesystem has to be writable — and the two knobs are
# allowed to contradict each other only in one direction: sudo off with
# a read-only root is the *default* deployment and is exactly right.
#
# The check is a refusal, not a fixer. It does not rewrite the
# deployment's configuration behind the operator's back: it says what
# is wrong and stops, so that the decision stays the operator's.
#
#   sudo off, root read-only   -> the default deployment. Fine.
#   sudo off, root writable     -> unusual, harmless. Fine.
#   sudo on,  root writable     -> the privilege mode. Fine.
#   sudo on,  root read-only    -> a half-root. Refused.
#   sudo on,  root undecided    -> a half-root (the default is
#                                 read-only). Refused.
set -euo pipefail

ALLOW_SUDO="${AGENT_ALLOW_SUDO:-0}"
READ_ONLY="${AGENT_READ_ONLY:-true}"

case "$ALLOW_SUDO" in
    1 | true | yes | on) SUDO_ON=1 ;;
    0 | false | no | off | "") SUDO_ON=0 ;;
    *)
        echo "check-posture: cannot read AGENT_ALLOW_SUDO='$ALLOW_SUDO'." >&2
        exit 1
        ;;
esac

case "$READ_ONLY" in
    true | True | TRUE | 1 | yes | on) ROOT_RO=1 ;;
    false | False | FALSE | 0 | no | off) ROOT_RO=0 ;;
    *)
        echo "check-posture: cannot read AGENT_READ_ONLY='$READ_ONLY'." >&2
        exit 1
        ;;
esac

if [ "$SUDO_ON" -eq 1 ] && [ "$ROOT_RO" -eq 1 ]; then
    echo "check-posture: the agent may become root, but the root filesystem" >&2
    echo "check-posture: is read-only. That is a half-root: the privileged" >&2
    echo "check-posture: shell cannot install anything, and 'Read-only file" >&2
    echo "check-posture: system' is what the model sees. Set" >&2
    echo "check-posture: AGENT_READ_ONLY=false (a real root can write), or" >&2
    echo "check-posture: AGENT_ALLOW_SUDO=0 (no root at all)." >&2
    exit 1
fi

if [ "$SUDO_ON" -eq 1 ]; then
    echo "check-posture: privilege mode on — the agent may become root, and the root may write."
else
    echo "check-posture: no sudo — the agent's shell cannot become root."
fi

exit 0
