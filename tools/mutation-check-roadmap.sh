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
#     wrapped by tools of our own rather than the other sandbox's;
#   * the operator's decision of 2026-11-02 -- one container -- is on the
#     record, wave B still reads as NOT built, and the browser wave still
#     says the browser lives in the same container. A decision that is
#     not pinned becomes a suggestion, and the topology is exactly the
#     kind of thing that drifts while nobody is looking;
#   * the browser wave does not claim a localhost debugging port: our own
#     implementation drives Chromium over a pipe, measured 2026-11-02, so
#     a boundary built against that port would be built against nothing.
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
# The heading now appears twice — once in the shipped summary, once in
# the planning wave — and a mutation that is not unique changes the
# FIRST one. The context below pins the planning wave, which is the one
# the detector reads.
mutate docs/ROADMAP.md \
  '### Wave F — the browser, in the same container

**Decided: it goes in.**' \
  '### Wave F — the browser, if it is wanted at all

**Decided: it goes in.**' \
  "$TESTS::TestTheBrowserWaveIsDecided::test_the_browser_wave_is_not_conditional" \
  "the browser wave is conditional again"

echo "== the browser wave stops naming its engine =="
mutate docs/ROADMAP.md \
  'So: Playwright driving Chromium, in our own image,' \
  'So: A browser, in our own image,' \
  "$TESTS::TestTheBrowserWaveIsDecided::test_the_browser_wave_names_the_engine" \
  "the browser wave does not say what it runs"

echo "== the browser wave borrows the other sandbox's tools =="
mutate docs/ROADMAP.md \
  '* `browser_open` — navigate to a URL and return the rendered text, bounded.' \
  '* `browser_exec` — navigate to a URL and return the rendered text, bounded.' \
  "$TESTS::TestTheBrowserWaveIsDecided::test_the_browser_wave_uses_wrappers_of_our_own" \
  "the browser wave wraps the other sandbox's tool"

echo "== the browser wave is gone =="
# Same disambiguation as above: the shipped summary carries the same
# heading, and this mutation has to take the planning wave away.
mutate docs/ROADMAP.md \
  '### Wave F — the browser, in the same container

**Decided: it goes in.**' \
  '### Wave F — something else

**Decided: it goes in.**' \
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

# --- the topology decision of 2026-11-02 ---------------------------------
#
# The operator settled the shape: one container. These mutations break each
# part of that decision on purpose. If one of them stays green, the decision
# is a suggestion, and the next reader is free to design a second container
# against it.

echo "== the one-container decision is no longer on the record =="
mutate docs/ROADMAP.md \
  '* **The sandbox is one container.** 2026-11-02, operator.' \
  '* **The sandbox is one container.** (date forgotten), operator.' \
  "$TESTS::TestTheOneContainerDecision::test_the_decision_is_on_the_record" \
  "the one-container decision lost its date"

echo "== the one-container decision becomes conditional again =="
mutate docs/ROADMAP.md \
  '* **The sandbox is one container.** 2026-11-02, operator.' \
  '* **The sandbox may be one container.** 2026-11-02, operator, to be decided.' \
  "$TESTS::TestTheOneContainerDecision::test_the_decision_is_not_conditional" \
  "the one-container decision is conditional again"

echo "== wave B comes back as something to build =="
mutate docs/ROADMAP.md \
  '**This wave was dropped on 2026-11-02.**' \
  '**This wave was planned on 2026-11-02.**' \
  "$TESTS::TestTheOneContainerDecision::test_wave_b_is_not_built" \
  "wave B reads as a wave to build again"

echo "== the browser is back in a container of its own =="
mutate docs/ROADMAP.md \
  '**It goes into the sandbox'"'"'s own container — the same one the model lives in.**' \
  '**It goes into a container of its own, separate from the model.**' \
  "$TESTS::TestTheOneContainerDecision::test_the_browser_wave_says_same_container" \
  "the browser wave no longer says the browser shares the container"
echo "== the browser wave claims a debugging port we do not open =="
mutate docs/ROADMAP.md \
  'carries `--remote-debugging-pipe` in its argv, the port flag is absent, and' \
  'opens a devtools port on localhost, and' \
  "$TESTS::TestTheOneContainerDecision::test_the_browser_wave_does_not_claim_a_debugging_port" \
  "the browser wave no longer states the measured pipe-and-no-port fact"

echo "== the browser wave drops the measurements it stands on =="
mutate docs/ROADMAP.md \
  '(`pidfd_getfd` hands over the socket);' \
  '(the descriptor is not reachable);' \
  "$TESTS::TestTheOneContainerDecision::test_the_browser_wave_carries_its_measurements" \
  "the browser wave no longer carries its measurements"

echo "== the detector cannot see a conditional decision any more =="
mutate tests/test_roadmap_drift.py \
  '        "to be decided",
        "undecided",
        "should we",
    )

    def setUp(self) -> None:
        self.roadmap = ROADMAP.read_text(encoding="utf-8")' \
  '        "a phrase the detector can no longer see",
        "undecided",
        "should we",
    )

    def setUp(self) -> None:
        self.roadmap = ROADMAP.read_text(encoding="utf-8")' \
  "$TESTS::TestTheOneContainerDecision::test_the_check_would_notice_a_conditional_decision" \
  "the detector cannot see a conditional decision"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
