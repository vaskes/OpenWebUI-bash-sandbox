#!/usr/bin/env bash
# Mutation check for the roadmap tripwire — the plan's claims about our
# own stack, and the decision the operator made about the browser.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties this wave pins:
#
#   * the reach the roadmap claims for the jail is the reach the compose
#     files actually give it — the jail's egress is UNRESTRICTED, and the
#     first version of the roadmap claimed the opposite, so a reader would
#     have designed a browser wave around a jail that is sealed off;
#   * the security document and the roadmap tell the operator the same
#     story about that reach;
#   * the claim that the jail is sealed off does not come back;
#   * the browser wave is decided, names the engine it runs, and is
#     wrapped by tools of our own rather than the other sandbox's.
#
# The mutations below also break the DETECTOR, and the control catches
# them. A verdict about "the property still holds" is a statement about
# the detector as much as about the property, and a detector that cannot
# see the thing it was written for would report a clean bill of health
# for a tree nobody broke.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

TESTS=tests/test_roadmap_drift.py

# The tree has to be green before anything is broken: a mutation sweep
# against a red tree reports catches for a tree nobody broke.
mutation_require_green "$TESTS"

echo "== the roadmap claims a reach the compose file does not give =="
mutate docs/ROADMAP.md \
  'egress   unrestricted — measured from inside the sandbox on 2026-10-29: the' \
  'egress   restricted — nothing in it reaches the internet by design, and the' \
  "$TESTS::TestTheEgressClaimIsTheMeasuredOne::test_the_roadmap_egress_claim_matches_the_compose_files" \
  "the roadmap states a reach the deployment does not give"

echo "== the jail is declared sealed off again =="
mutate docs/ROADMAP.md \
  'The gap is not the network. The gap is the' \
  'The gap is not the network. Nothing in it reaches the internet by design; the gap is the' \
  "$TESTS::TestTheSealedJailDoesNotComeBack::test_the_roadmap_does_not_claim_the_jail_is_sealed" \
  "the roadmap claims the jail cannot reach the internet"

echo "== the security document tells a different story from the roadmap =="
mutate docs/SECURITY.md \
  '| External network access              | **Unrestricted** (intentional)                  |' \
  '| External network access              | **Restricted** (intentional)                    |' \
  "$TESTS::TestTheEgressClaimIsTheMeasuredOne::test_the_security_document_agrees_with_the_compose_files" \
  "the security document states a reach the deployment does not give"

echo "== the compose file tightens the network and the documents do not follow =="
mutate docker-compose.yml \
  'networks:
  agent-net:
    driver: bridge' \
  'networks:
  agent-net:
    driver: bridge
    internal: true' \
  "$TESTS::TestTheEgressClaimIsTheMeasuredOne::test_the_roadmap_egress_claim_matches_the_compose_files" \
  "the network is internal and the roadmap still claims an open reach"

echo "== the browser wave becomes optional again =="
mutate docs/ROADMAP.md \
  '### Wave F — the browser, in the sandbox' \
  '### Wave F — the browser, if it is wanted at all' \
  "$TESTS::TestTheBrowserWaveIsDecided::test_the_browser_wave_is_not_conditional" \
  "the browser wave is conditional again"

echo "== the browser wave stops naming its engine =="
mutate docs/ROADMAP.md \
  'Playwright driving Chromium, in our own image,' \
  'A browser, in our own image,' \
  "$TESTS::TestTheBrowserWaveIsDecided::test_the_browser_wave_names_the_engine" \
  "the browser wave does not say what it runs"

echo "== the browser wave borrows the other sandbox's tools =="
mutate docs/ROADMAP.md \
  '* `browser_open` — navigate to a URL and return the rendered text, bounded.' \
  '* `browser_exec` — navigate to a URL and return the rendered text, bounded.' \
  "$TESTS::TestTheBrowserWaveIsDecided::test_the_browser_wave_uses_wrappers_of_our_own" \
  "the browser wave wraps the other sandbox's tool"

echo "== the browser wave is gone =="
mutate docs/ROADMAP.md \
  '### Wave F — the browser, in the sandbox' \
  '### Wave F — something else' \
  "$TESTS::TestTheBrowserWaveIsDecided::test_the_browser_wave_exists" \
  "the browser wave does not exist"

# The two mutations below break the DETECTOR, and the control catches them.
echo "== the detector cannot see a sealed claim any more =="
mutate tests/test_roadmap_drift.py \
  '        "nothing in it reaches the internet",' \
  '        "a phrase the detector can no longer see",' \
  "$TESTS::TestTheSealedJailDoesNotComeBack::test_the_check_would_notice_a_sealed_claim" \
  "the detector cannot see the sentence it was written for"

echo "== the detector cannot see a conditional wave any more =="
mutate tests/test_roadmap_drift.py \
  '        "if it is wanted",' \
  '        "a phrase the detector can no longer see",' \
  "$TESTS::TestTheBrowserWaveIsDecided::test_the_check_would_notice_a_conditional_heading" \
  "the detector cannot see a conditional heading"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
