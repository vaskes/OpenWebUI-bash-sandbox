#!/usr/bin/env bash
# Mutation check for the v2.14.11 tripwires.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# What this wave is about: the agent's token reached the model's shell
# through the tmux server's environment. Measured on the production
# deployment on 2026-10-29: a session's `env` printed it in plaintext,
# `tmux show-environment -g` printed it too, and the sandbox has
# unrestricted egress, so a token in the model's hands is a token that
# can leave the host.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

RULE="tests/test_agent_token_v21411.py"

echo "== the tmux client gets the agent's full environment again =="
mutate agent_api.py \
  '        env=_tmux_environment(),' \
  '' \
  "$RULE" \
  "a session's shell does not see the token"

echo "== the environment builder stops dropping the credentials =="
mutate agent_api.py \
  '    return {name: value for name, value in os.environ.items() if name not in _secret_env_names()}' \
  '    return dict(os.environ)' \
  "$RULE" \
  "the credentials are dropped from the tmux environment"

echo "== a server started with the token is not cleaned up =="
mutate agent_api.py \
  '        await _tmux("set-environment", "-g", "-u", name)' \
  '        await _tmux("display-message", "-p", name)' \
  "$RULE" \
  "the tmux server's own environment is cleaned"

echo "== the agent's inbound token cannot be separated =="
mutate docker-compose.yml \
  'AGENT_TOKEN=${AGENT_INBOUND_TOKEN:-${OPENAPI_PROXY_TOKEN' \
  'AGENT_TOKEN=${OPENAPI_PROXY_TOKEN' \
  "$RULE" \
  "the deployment can separate the tokens"

echo "== the production agent's inbound token cannot be separated =="
mutate docker-compose.production.yml \
  'AGENT_TOKEN=${AGENT_INBOUND_TOKEN:-${AGENT_STACK_TOKEN' \
  'AGENT_TOKEN=${AGENT_STACK_TOKEN' \
  "$RULE" \
  "the production deployment can separate the tokens"

echo "== the proxy does not forward the agent's own token =="
mutate docker-compose.yml \
  'OPENAPI_PROXY_UPSTREAM_TOKEN=${OPENAPI_PROXY_UPSTREAM_TOKEN:-${AGENT_INBOUND_TOKEN:-${OPENAPI_PROXY_TOKEN}}}' \
  'OPENAPI_PROXY_UPSTREAM_TOKEN=${OPENAPI_PROXY_UPSTREAM_TOKEN:-}' \
  "$RULE" \
  "the proxy authenticates upstream with the agent's own token"

echo "== the production proxy does not forward the agent's own token =="
mutate docker-compose.production.yml \
  'OPENAPI_PROXY_UPSTREAM_TOKEN=${OPENAPI_PROXY_UPSTREAM_TOKEN:-${AGENT_INBOUND_TOKEN:-${AGENT_STACK_TOKEN}}}' \
  'OPENAPI_PROXY_UPSTREAM_TOKEN=${OPENAPI_PROXY_UPSTREAM_TOKEN:-}' \
  "$RULE" \
  "the production proxy authenticates upstream with the agent's own token"

echo "== the security document stops saying what the sandbox is =="
mutate docs/SECURITY.md \
  '## The sandbox is not a boundary against the model' \
  '## Something else entirely' \
  "$RULE" \
  "the security document states the boundary"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
