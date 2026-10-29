#!/usr/bin/env bash
# Mutation check for the v2.14.9 tripwires.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The wave this script belongs to closed the documentation audit: eight
# claims the docs made about the code that were false, and one promise
# the template made that only held on the path the Makefile takes.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

RULE_DRIFT="tests/test_doc_drift_v2149.py"
RULE_TOKEN="tests/test_placeholder_token_v2149.py"

echo "== the configuration table advertises the timeout the proxy does not ship =="
mutate docs/CONFIGURATION.md \
  '| `AGENT_EXEC_TIMEOUT`                   | `60`  ' \
  '| `AGENT_EXEC_TIMEOUT`                   | `3600`' \
  "$RULE_DRIFT" \
  "the documented timeout is the shipped one"

echo "== the compose default and the code constant drift apart =="
mutate proxy_core.py \
  'EXEC_TIMEOUT_SECONDS = 60.0' \
  'EXEC_TIMEOUT_SECONDS = 3600.0' \
  "$RULE_DRIFT" \
  "the compose default and the code agree"

echo "== the required variable of the production stack is undocumented =="
mutate docs/CONFIGURATION.md \
  '`AGENT_STACK_TOKEN` is the production stack' \
  '`THE_STACK_TOKEN` was the production stack' \
  "$RULE_DRIFT" \
  "a required variable is documented in prose"

echo "== the handoff contract stops naming a status the door answers =="
mutate docs/HANDOFF-binary-transport.md \
  'that was replaced while it was being opened is a **409**' \
  'that was replaced while it was being opened is a **418**' \
  "$RULE_DRIFT" \
  "the contract names every status the door answers"

echo "== the handoff contract stops pinning a version =="
mutate docs/HANDOFF-binary-transport.md \
  '**Code against:** sandbox tag' \
  '**Code against (unpinned):**' \
  "$RULE_DRIFT" \
  "the contract pins the version that is deployed"

echo "== the tree's version stops matching the contract's =="
mutate agent_core.py \
  '__version__ = "' \
  '__version__ = "0.0.0' \
  "$RULE_DRIFT" \
  "the contract pins the version that is deployed"

echo "== the tree's version stops matching the changelog's newest section =="
mutate agent_core.py \
  '__version__ = "' \
  '__version__ = "0.0.0' \
  "tests/test_release_claims_v2149.py" \
  "the shipped version is the release the changelog describes"

echo "== the metrics table drops a metric the service exports =="
mutate docs/CONFIGURATION.md \
  '| `sandbox_agent_exec_refused_total`    | agent   | commands refused before they were typed, because they would stick the shell |' \
  '| `sandbox_agent_exec_refused_total_v2` | agent   | commands refused before they were typed, because they would stick the shell |' \
  "$RULE_DRIFT" \
  "the metrics table names every metric"

echo "== the metrics table lists a metric nobody exports =="
mutate docs/CONFIGURATION.md \
  '| `sandbox_agent_active_sessions`       | agent   | sessions held right now             |' \
  '| `sandbox_agent_active_sessions`       | agent   | sessions held right now             |
| `sandbox_agent_ghost_sessions`         | agent   | a metric no service exports         |' \
  "$RULE_DRIFT" \
  "the metrics table names no metric the code does not export"

echo "== the docs claim the compose file lacks the bind address =="
mutate README.md \
  "> the operator's choice, and the compose file spells it out as a parameter:" \
  "> the operator's choice, and is not written into the compose file. The" \
  "$RULE_DRIFT" \
  "the docs do not contradict the compose file"

echo "== the template offers a knob the compose pins to a literal =="
mutate docker-compose.yml \
  '      - AGENT_LOG_LEVEL=${AGENT_LOG_LEVEL:-INFO}' \
  '      - AGENT_LOG_LEVEL=INFO' \
  "$RULE_DRIFT" \
  "an offered knob is interpolated"

echo "== the template offers a knob no compose interpolates =="
mutate docker-compose.yml \
  '      - OPENAPI_PROXY_UPSTREAM_TOKEN=${OPENAPI_PROXY_UPSTREAM_TOKEN:-${AGENT_INBOUND_TOKEN:-${OPENAPI_PROXY_TOKEN}}}' \
  '      # (removed)' \
  "$RULE_DRIFT" \
  "every offered knob is a knob the stack honours"

echo "== the template stops offering the upstream token knob =="
mutate .env.example \
  '# OPENAPI_PROXY_UPSTREAM_TOKEN=' \
  '# (removed)' \
  "$RULE_DRIFT" \
  "the upstream token knob is offered"

echo "== the placeholder is not a placeholder any more =="
mutate token_rules.py \
  '    return token.startswith(PLACEHOLDER_PREFIX)' \
  '    return False' \
  "$RULE_TOKEN" \
  "a placeholder is recognised"

echo "== the placeholder rule is not shared any more =="
mutate token_rules.py \
  'PLACEHOLDER_PREFIX = "CHANGE_ME"' \
  'PLACEHOLDER_PREFIX = "never-matches-anything"' \
  "$RULE_TOKEN" \
  "the rule names the template's placeholder"

echo "== the proxy stops refusing a placeholder =="
mutate openapi_proxy.py \
  '    if is_placeholder(PROXY_TOKEN):' \
  '    if False:' \
  "$RULE_TOKEN" \
  "the proxy refuses a published token"

echo "== the agent stops refusing a placeholder =="
mutate agent_api.py \
  '    if is_placeholder(AGENT_TOKEN):' \
  '    if False:' \
  "$RULE_TOKEN" \
  "the agent refuses a published token"

echo "== the agent's MCP door stops refusing a placeholder =="
mutate agent_api.py \
  '    if is_placeholder(AGENT_TOKEN):
        return False' \
  '    if False:
        return False' \
  "$RULE_TOKEN" \
  "the MCP door refuses a published token"

echo "== the websocket door stops refusing a placeholder =="
mutate openapi_proxy.py \
  '    if not PROXY_TOKEN or is_placeholder(PROXY_TOKEN):' \
  '    if not PROXY_TOKEN:' \
  "$RULE_TOKEN" \
  "the websocket door refuses a published token"

echo "== an empty upstream token drops the Authorization header =="
mutate openapi_proxy.py \
  '    token = os.environ.get("OPENAPI_PROXY_UPSTREAM_TOKEN") or PROXY_TOKEN' \
  '    token = os.environ.get("OPENAPI_PROXY_UPSTREAM_TOKEN", PROXY_TOKEN)' \
  "$RULE_TOKEN" \
  "an empty upstream token falls back to the proxy token"

echo "== the shared module is not in the agent's image =="
mutate Dockerfile.sandbox \
  'COPY --chown=1000:1000 token_rules.py  /home/agent/token_rules.py' \
  '# (removed)' \
  "$RULE_TOKEN" \
  "the shared module is in the image that runs it"

echo "== the shared module is not in the proxy's image =="
mutate Dockerfile.openapi-proxy \
  'COPY --chown=1000:1000 token_rules.py     /app/token_rules.py' \
  '# (removed)' \
  "$RULE_TOKEN" \
  "the shared module is in the proxy's image"

echo "== the services stop sharing the rule =="
mutate agent_api.py \
  'from token_rules import is_placeholder' \
  'def is_placeholder(_token: str) -> bool:
    return False' \
  "$RULE_TOKEN" \
  "both services use the same rule"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
