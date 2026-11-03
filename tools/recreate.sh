#!/usr/bin/env bash
# Recreate the stack (or one of its services) without losing the
# workspace's ceiling.
#
# WHY THIS SCRIPT EXISTS
#
# The workspace is a filesystem with a ceiling: an ext4 filesystem in a
# file, mounted on the host, handed to the container as a volume. When
# Docker removes the container, the container's mount namespace goes with
# it -- and so does the host bind that was inside it: the workspace stops
# being a filesystem with a ceiling and becomes a plain directory on the
# host's own partition. Measured on the production stand on 2026-11-03:
# the deploy was ordered "mount the filesystem, then start the stack",
# the agent container was recreated, the filesystem went away with it,
# and the new container came up on the host's disk and refused to start.
#
# `docker compose up --force-recreate` removes the old container and
# starts the new one in ONE command, and the mount cannot be re-made in
# between: the new container starts on a plain directory, and an agent
# that was promised a ceiling refuses to serve rather than write into a
# workspace with none.
#
# The order that works: remove the container, make the mount, start the
# container. This script is that order, in one place, so the Makefile and
# the CI cannot drift apart from it -- or from the rule
# docs/SECURITY.md section 4 already states.
#
#   usage: tools/recreate.sh <compose-file> [service ...]
#
# The workspace's path and size come from the environment
# (AGENT_WORKSPACE_PATH, AGENT_WORKSPACE_SIZE) and are REQUIRED: the
# ceiling belongs to the deployment, and a script that guessed it would
# be one more place where the guess can be wrong.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE_FILE="${1:?usage: recreate.sh <compose-file> [service ...]}"
shift

WORKSPACE_PATH="${AGENT_WORKSPACE_PATH:?AGENT_WORKSPACE_PATH must be set — the ceiling belongs to the deployment, not to this script}"
WORKSPACE_SIZE="${AGENT_WORKSPACE_SIZE:?AGENT_WORKSPACE_SIZE must be set — a workspace without a ceiling is not a workspace}"

# 1. Remove the container(s). The mount goes with them.
if [ "$#" -eq 0 ]; then
    docker compose -f "$COMPOSE_FILE" rm -sf
else
    docker compose -f "$COMPOSE_FILE" rm -sf "$@"
fi

# 2. Make the ceiling. The script is idempotent: a mount that survived
#    is checked and re-asserted, a mount that went away is made again.
sudo bash tools/setup-workspace.sh "$WORKSPACE_PATH" "$WORKSPACE_SIZE"

# 3. Start on the filesystem that has the ceiling.
if [ "$#" -eq 0 ]; then
    docker compose -f "$COMPOSE_FILE" up -d --build
else
    docker compose -f "$COMPOSE_FILE" up -d --build "$@"
fi
