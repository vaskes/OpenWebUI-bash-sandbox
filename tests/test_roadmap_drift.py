"""The roadmap's claims about our own stack must match the stack.

The first version of ``docs/ROADMAP.md`` said that the jail has no way to
the internet. It was wrong, and it was wrong in the direction that costs
money: the jail's outbound access is **unrestricted**. The compose files put
the agent on a plain bridge network with no ``internal: true``, the image
ships ``curl`` and ``requests``/``httpx``, and ``docs/SECURITY.md`` records
both the policy and a measurement from inside the container on 2026-10-29
that reached the operator's SSH host, the Gitea instance and the router.

A reader of that document would have designed a browser wave around a
jail that is sealed off, and the fence they chose would have been the wrong
one: the jail could already reach everything, so what needed bounding was
never the network but the *use* of it.

So this tripwire does what ``tests/test_doc_drift_v2149.py`` does for the
configuration reference and what ``tests/test_drift_architecture_docs.py``
does for the architecture tables: it compares a document's claim with the
code, and it fails when the two disagree. Three sources have to agree:

1. the compose files -- whether the agent's network is ``internal``;
2. ``docs/SECURITY.md`` -- the posture the operator is told about;
3. ``docs/ROADMAP.md`` -- the posture the plan is built on.

Every verdict is derived from the files themselves, so the check cannot rot
into a tautology: tightening the network makes the documents wrong, and the
tests say so instead of letting the plan drift away from the deployment.

The second half of the file pins the other decision the operator made on
2026-11-01: the browser goes in. The wave is decided, not optional, it names
the engine it will run, and the wrappers around it are ours -- not the other
sandbox's ``browser_exec``/``browser_cdp``/``browser_dialog``/``browser_vault``.
A roadmap that quietly turns a decided wave back into "if it is wanted at
all" is a roadmap that will never be built.

And a check that cannot be made red is decoration: ``tools/mutation-check-roadmap.sh``
breaks each property on purpose and expects the named test to go red.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ROADMAP = ROOT / "docs" / "ROADMAP.md"
SECURITY = ROOT / "docs" / "SECURITY.md"

#: The two compose files a deployment can start. Both are shipped, so both
#: are held to the same standard.
COMPOSE_FILES = (ROOT / "docker-compose.yml", ROOT / "docker-compose.production.yml")

#: The service whose network decides what the jail can reach.
AGENT_SERVICE = "sandbox-agent"

#: The two postures a network can have, as the documents name them.
UNRESTRICTED = "unrestricted"
RESTRICTED = "restricted"


# ---------------------------------------------------------------------------
# Reading the compose files as text.
#
# PyYAML is not a declared dependency of this project, so the compose files
# are parsed as the text they are -- the same convention
# ``tests/test_doc_drift_v2149.py`` uses.
# ---------------------------------------------------------------------------


def _service_lines(text: str, service: str) -> list[str]:
    """The body of one service, by indentation.

    A service name sits at two spaces, its keys at four, so the next
    service is the first line that is indented by exactly two and carries
    something after them.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if re.match(rf"^  {re.escape(service)}:\s*$", line):
            start = index
            break
    if start is None:
        return []
    body: list[str] = []
    for line in lines[start + 1 :]:
        if re.match(r"^  \S", line):
            break
        body.append(line)
    return body


def _list_under(block: list[str], key: str) -> list[str]:
    """The items of the list under one key of a block."""
    items: list[str] = []
    inside = False
    for line in block:
        if re.match(rf"^\s*{re.escape(key)}:\s*$", line):
            inside = True
            continue
        if not inside:
            continue
        match = re.match(r"^\s*-\s*(\S+)\s*$", line)
        if match:
            items.append(match.group(1))
        elif not line.strip():
            continue
        else:
            break
    return items


def _agent_networks(text: str) -> list[str]:
    """The networks the agent service is attached to."""
    return _list_under(_service_lines(text, AGENT_SERVICE), "networks")


def _network_is_internal(text: str, name: str) -> bool:
    """Is one network declared ``internal: true``?

    An internal Docker network has no route off the host, so it is the one
    knob that decides whether the jail can reach anything at all.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if re.match(r"^networks:\s*$", line):
            start = index
            break
    if start is None:
        return False

    body: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        if not line.startswith("  "):
            break
        body.append(line)

    for index, line in enumerate(body):
        if not re.match(rf"^  {re.escape(name)}:\s*$", line):
            continue
        for sub in body[index + 1 :]:
            if not sub.startswith("    "):
                break
            if re.match(r"^\s*internal:\s*true\s*$", sub):
                return True
        return False
    return False


def _compose_verdict(path: Path) -> str:
    """What the compose file says about the jail's reach."""
    text = path.read_text(encoding="utf-8")
    networks = _agent_networks(text)
    if not networks:
        return RESTRICTED
    if any(_network_is_internal(text, network) for network in networks):
        return RESTRICTED
    return UNRESTRICTED


def _document_verdict(text: str, pattern: str) -> str:
    """The posture a document states, read out of its own table row."""
    match = re.search(pattern, text, re.MULTILINE)
    if match is None:
        return ""
    return match.group(1).strip().strip("*").lower()


#: The posture the security document states for the sandbox's reach.
SECURITY_PATTERN = r"^\|\s*External network access\s*\|\s*\**([A-Za-z]+)"

#: The posture the roadmap states, in the fenced block that describes our
#: own stack.
ROADMAP_PATTERN = r"^egress\s+([A-Za-z]+)"


class TestTheEgressClaimIsTheMeasuredOne(unittest.TestCase):
    """The plan is built on a posture; the posture has to be the real one."""

    def setUp(self) -> None:
        self.roadmap = ROADMAP.read_text(encoding="utf-8")
        self.security = SECURITY.read_text(encoding="utf-8")

    def test_the_compose_files_can_be_read(self) -> None:
        """A premise that cannot be parsed proves nothing."""
        for compose in COMPOSE_FILES:
            with self.subTest(compose=compose.name):
                text = compose.read_text(encoding="utf-8")
                self.assertTrue(
                    _agent_networks(text),
                    f"{compose.name}: the agent service's networks could not be read",
                )

    def test_the_roadmap_egress_claim_matches_the_compose_files(self) -> None:
        """The roadmap must state the reach the deployment actually gives."""
        stated = _document_verdict(self.roadmap, ROADMAP_PATTERN)
        self.assertTrue(
            stated,
            "docs/ROADMAP.md states no egress posture — the block that "
            "describes our own stack has to say what the jail can reach",
        )
        for compose in COMPOSE_FILES:
            with self.subTest(compose=compose.name):
                actual = _compose_verdict(compose)
                self.assertEqual(
                    stated,
                    actual,
                    f"docs/ROADMAP.md says the jail's reach is {stated!r}, but "
                    f"{compose.name} gives it {actual!r}. One of the two is a "
                    "lie, and the plan is built on the wrong one.",
                )

    def test_the_security_document_agrees_with_the_compose_files(self) -> None:
        """The operator is told one posture; the deployment gives another."""
        stated = _document_verdict(self.security, SECURITY_PATTERN)
        self.assertTrue(
            stated,
            "docs/SECURITY.md states no posture for external network access",
        )
        for compose in COMPOSE_FILES:
            with self.subTest(compose=compose.name):
                self.assertEqual(
                    stated,
                    _compose_verdict(compose),
                    f"docs/SECURITY.md tells the operator the sandbox's reach is "
                    f"{stated!r}, but {compose.name} gives it "
                    f"{_compose_verdict(compose)!r}.",
                )

    def test_the_roadmap_and_the_security_document_agree(self) -> None:
        """Two documents, one fact, two answers is one stale document."""
        self.assertEqual(
            _document_verdict(self.roadmap, ROADMAP_PATTERN),
            _document_verdict(self.security, SECURITY_PATTERN),
            "docs/ROADMAP.md and docs/SECURITY.md disagree about what the jail can reach",
        )


class TestTheSealedJailDoesNotComeBack(unittest.TestCase):
    """The claim that was wrong once must not be written a second time."""

    #: Phrases that say the jail is sealed off. They are the words the
    #: first draft used; the check exists so that the sentence cannot be
    #: pasted back in.
    SEALED = (
        "nothing in it reaches the internet",
        "no way to the open web",
    )

    def test_the_roadmap_does_not_claim_the_jail_is_sealed(self) -> None:
        text = ROADMAP.read_text(encoding="utf-8").lower()
        for phrase in self.SEALED:
            with self.subTest(phrase=phrase):
                self.assertNotIn(
                    phrase,
                    text,
                    "docs/ROADMAP.md claims the jail cannot reach the internet. "
                    "The jail's reach is unrestricted and was measured from "
                    "inside the container — a plan built on a sealed jail "
                    "fences the wrong thing.",
                )

    def test_the_check_would_notice_a_sealed_claim(self) -> None:
        """A scan that cannot see anything is a scan that proves nothing."""
        planted = "the agent has a network, but nothing in it reaches the internet"
        self.assertTrue(
            any(phrase in planted.lower() for phrase in self.SEALED),
            "the check cannot see the sentence it was written for",
        )


class TestTheBrowserWaveIsDecided(unittest.TestCase):
    """The operator decided on 2026-11-01 that the browser goes in."""

    #: Phrases that turn a decided wave back into a question. They are
    #: phrases, not single words: "maybe" and "perhaps" are ordinary
    #: English, and a tripwire that fires on ordinary English is a
    #: tripwire that gets switched off.
    CONDITIONAL = (
        "if it is wanted",
        "to be tested by the operator",
        "in, or not?",
        "to be decided",
        "undecided",
        "should we",
    )

    #: The other sandbox's browser tools. Ours are named differently, and
    #: the difference is the point: their surface is not ours to copy.
    THEIRS = ("browser_exec", "browser_cdp", "browser_dialog", "browser_vault")

    def _wave(self) -> tuple[str, str]:
        """The heading and the body of the browser wave.

        The body ends at the next section, not at the end of the file: a
        wave is what it says, and the sections after it say other things.

        It is the **planning** wave, under "## The waves" — not the
        shipped summary above it. The summary is a pointer to the wave
        and a list of what changed; the wave is what carries the shape
        and the measurements, and that is what this check reads.
        """
        text = ROADMAP.read_text(encoding="utf-8")
        waves_at = text.find("\n## The waves")
        if waves_at > 0:
            text = text[waves_at:]
        headings = [
            match.group(0) for match in re.finditer(r"^###\s+Wave\b.*$", text, re.MULTILINE)
        ]
        for heading in headings:
            if "browser" not in heading.lower():
                continue
            rest = text[text.index(heading) :]
            following = re.search(r"^#{2,3}\s+\S", rest[len(heading) :], re.MULTILINE)
            end = len(heading) + following.start() if following else len(rest)
            return heading, rest[:end]
        self.fail("docs/ROADMAP.md has no wave for the browser")

    def test_the_browser_wave_exists(self) -> None:
        """The wave is there; the rest of this class is about its shape."""
        heading, _ = self._wave()
        self.assertTrue(heading)

    def test_the_browser_wave_is_not_conditional(self) -> None:
        """A decided wave must not read like an open question."""
        heading, body = self._wave()
        for phrase in self.CONDITIONAL:
            with self.subTest(phrase=phrase):
                self.assertNotIn(
                    phrase,
                    heading.lower(),
                    f"the browser wave is still conditional: {heading!r}. The "
                    "operator decided on 2026-11-01 that it goes in.",
                )
                self.assertNotIn(
                    phrase,
                    body.lower(),
                    f"the browser wave is still conditional: {phrase!r}. The "
                    "operator decided on 2026-11-01 that it goes in.",
                )

    def test_the_browser_wave_names_the_engine(self) -> None:
        """A wave that does not say what it runs cannot be built."""
        _, body = self._wave()
        for engine in ("playwright", "chromium"):
            with self.subTest(engine=engine):
                self.assertIn(
                    engine,
                    body.lower(),
                    f"the browser wave does not say it runs {engine} — the wave "
                    "has to be buildable from the document alone",
                )
        # The sentence that names the engine, not just the words somewhere
        # in the body: the wave has to say what runs in the place where it
        # says what runs, and that is the sentence a reader acts on.
        self.assertIn(
            "Playwright driving Chromium",
            body,
            "the browser wave no longer names the engine in the sentence that "
            "declares it — the words may still appear elsewhere, but the wave "
            "has to say what it runs where it says what it runs",
        )

    def test_the_browser_wave_uses_wrappers_of_our_own(self) -> None:
        """Our wrappers, not theirs."""
        _, body = self._wave()
        for tool in self.THEIRS:
            with self.subTest(tool=tool):
                self.assertNotIn(
                    tool,
                    body.lower(),
                    f"the browser wave names {tool}, which is the other "
                    "sandbox's tool. The wrappers are ours, written here.",
                )
        ours = [
            name
            for name in re.findall(r"\bbrowser_[a-z_]+\b", body.lower())
            if name not in self.THEIRS
        ]
        self.assertTrue(
            ours,
            "the browser wave names no wrapper of our own — the browser has to "
            "be reachable through tools we wrote",
        )

    def test_the_check_would_notice_a_conditional_heading(self) -> None:
        """A check that cannot see the shape it was written for proves nothing."""
        heading = "### Wave F — the browser, if it is wanted at all"
        self.assertTrue(
            any(phrase in heading.lower() for phrase in self.CONDITIONAL),
            "the check cannot see a conditional heading",
        )


class TestTheOneContainerDecision(unittest.TestCase):
    """The operator decided the topology on 2026-11-02: one container.

    A decision that is not pinned becomes a suggestion. The previous
    revision of ``docs/ROADMAP.md`` argued for a separate executor
    service and a separate browser container; the operator closed both
    questions in favour of the simpler shape -- the sandbox is one
    atomic service of one Docker, and the browser goes into the
    container the model already lives in.

    The cost of that call is real and the document says so: the model
    and the agent share a uid, so the model can read the agent's own
    environment and attach to its processes. Those are measured facts,
    recorded in the document, and they are the reason the browser's
    fences live in our code rather than in a topology.

    These checks pin three things: the decision is on the record and is
    not conditional, wave B still reads as *not built*, and the browser
    wave still says the browser lives in the same container. Each of
    them can be made red by ``tools/mutation-check-roadmap.sh``.
    """

    #: Phrases that turn a decided wave back into a question. They are
    #: phrases, not single words: "maybe" and "perhaps" are ordinary
    #: English, and a tripwire that fires on ordinary English is a
    #: tripwire that gets switched off.
    CONDITIONAL = (
        "if it is wanted",
        "to be tested by the operator",
        "in, or not?",
        "to be decided",
        "undecided",
        "should we",
    )

    def setUp(self) -> None:
        self.roadmap = ROADMAP.read_text(encoding="utf-8")

    def _section(self, heading: str) -> str:
        """The body of one ``##`` section, up to the next one.

        A decision is recorded in a section, not scattered through the
        file: what the check reads is the section, so what it checks is
        the section.
        """
        pattern = rf"^{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)"
        match = re.search(pattern, self.roadmap, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, f"docs/ROADMAP.md has no {heading!r} section")
        return match.group(1)

    def _wave(self, marker: str) -> str:
        """The body of the wave whose heading contains ``marker``.

        The **planning** wave, under "## The waves" — not the shipped
        summary above it. The summary is a pointer and a list of what
        changed; the wave is what carries the shape and the measurements,
        and that is what this check reads.
        """
        roadmap = self.roadmap
        waves_at = roadmap.find("\n## The waves")
        if waves_at > 0:
            roadmap = roadmap[waves_at:]
        headings = [
            match.group(0) for match in re.finditer(r"^###\s+Wave\b.*$", roadmap, re.MULTILINE)
        ]
        for heading in headings:
            if marker.lower() not in heading.lower():
                continue
            rest = roadmap[roadmap.index(heading) :]
            following = re.search(r"^#{2,3}\s+\S", rest[len(heading) :], re.MULTILINE)
            end = len(heading) + following.start() if following else len(rest)
            return rest[:end]
        self.fail(f"docs/ROADMAP.md has no wave matching {marker!r}")

    def test_the_decision_is_on_the_record(self) -> None:
        """The one-container decision is recorded, and it is dated."""
        section = self._section("## Decisions on the record")
        self.assertIn(
            "one container",
            section.lower(),
            "the decision to keep the sandbox in one container is not on the "
            "record — the operator settled it on 2026-11-02 and the document "
            "has to say so",
        )
        self.assertIn(
            "2026-11-02",
            section,
            "the one-container decision carries no date — a decision with no date is a rumour",
        )

    def test_the_decision_is_not_conditional(self) -> None:
        """A settled decision must not read like an open question."""
        section = self._section("## Decisions on the record")
        for phrase in self.CONDITIONAL:
            with self.subTest(phrase=phrase):
                self.assertNotIn(
                    phrase,
                    section.lower(),
                    f"the decisions section is conditional again: {phrase!r}. The "
                    "operator settled the topology on 2026-11-02.",
                )

    def test_wave_b_is_not_built(self) -> None:
        """Wave B was dropped; it must not come back as a promise."""
        wave = self._wave("Wave B")
        self.assertIn(
            "not built",
            wave.lower(),
            "wave B reads as something to build again. The operator dropped it "
            "on 2026-11-02: the executor is the sandbox's own shell.",
        )
        self.assertIn(
            "was dropped",
            wave.lower(),
            "wave B no longer says it was dropped — the heading alone is not "
            "enough, because the body is what a reader acts on",
        )
        self.assertIn(
            "2026-11-02",
            wave,
            "wave B does not say when it was dropped",
        )

    def test_the_browser_wave_says_same_container(self) -> None:
        """The browser lives where the model lives."""
        wave = self._wave("Wave F")
        self.assertIn(
            "It goes into the sandbox's own container",
            wave,
            "the browser wave does not say the browser goes into the sandbox's "
            "own container. The operator decided that on 2026-11-02, and the "
            "wave has to be buildable from the document alone.",
        )

    def test_the_browser_wave_does_not_claim_a_debugging_port(self) -> None:
        """The old paragraph argued against a port we do not open.

        Playwright launches Chromium with ``--remote-debugging-pipe``, a
        socketpair, not a TCP port: measured on 2026-11-02, the browser
        opens zero listening sockets and carries no port flag. A reader
        who took the old text at face value would have built a boundary
        against a port that does not exist, so the measurement has to be
        here and the port must not creep back.
        """
        wave = self._wave("Wave F")
        self.assertIn(
            "the port flag is absent",
            wave,
            "the browser wave no longer states the measured fact that the port "
            "flag is absent. Playwright drives Chromium over a pipe "
            "(--remote-debugging-pipe), and the process opens no listening "
            "TCP socket at all — measured 2026-11-02.",
        )
        self.assertIn(
            "zero",
            wave,
            "the browser wave no longer states that the browser opens zero "
            "listening TCP sockets — that is the measurement the old, wrong "
            "paragraph was refuted with.",
        )
        self.assertNotIn(
            "9222",
            wave,
            "the browser wave names a debugging port. Playwright drives "
            "Chromium over a pipe (--remote-debugging-pipe), and the process "
            "opens no listening TCP socket at all — measured 2026-11-02.",
        )

    def test_the_browser_wave_carries_its_measurements(self) -> None:
        """The cost of the decision is written down, not implied."""
        wave = self._wave("Wave F")
        for fact in ("pidfd_getfd", "seccomp", "PSS"):
            with self.subTest(fact=fact):
                self.assertIn(
                    fact,
                    wave,
                    f"the browser wave does not mention {fact!r} — the "
                    "measurements are what make the wave's shape honest",
                )

    def test_the_check_would_notice_a_conditional_decision(self) -> None:
        """A check that cannot see anything is a check that proves nothing."""
        planted = "the sandbox may be one container, to be decided"
        self.assertTrue(
            any(phrase in planted.lower() for phrase in self.CONDITIONAL),
            "the check cannot see the sentence it was written for",
        )


class TestTheSingleAgentDecision(unittest.TestCase):
    """The operator closed wave E on 2026-11-03: one agent, no sub-agents.

    The roadmap had proposed two tools from the other sandbox: ``execute_code``
    (Python calling our own tools programmatically) and ``delegate_task`` (a
    sub-agent with its own context). The operator ruled both out of scope --
    this project is a single-operator, single-agent sandbox.

    A decision that is not pinned becomes a suggestion, and the next reader is
    free to design a second agent against this document. These checks pin
    three things: the decision is on the record and dated, wave E still reads
    as *not built*, and the gap list says the gap is closed by decision rather
    than open. Each of them can be made red by
    ``tools/mutation-check-roadmap.sh``.
    """

    # The document is read the same way in every decision test, so the reader
    # is reused instead of growing a third copy of it.
    _section = TestTheOneContainerDecision._section
    _wave = TestTheOneContainerDecision._wave

    def setUp(self) -> None:
        self.roadmap = ROADMAP.read_text(encoding="utf-8")

    def test_the_decision_is_on_the_record(self) -> None:
        """The single-agent decision is recorded, and it is dated."""
        section = self._section("## Decisions on the record")
        self.assertIn(
            "single agent",
            section.lower(),
            "the decision that this project is a single-agent sandbox is not on "
            "the record — the operator settled it on 2026-11-03 and the "
            "document has to say so",
        )
        self.assertIn(
            "2026-11-03",
            section,
            "the single-agent decision carries no date — a decision with no date is a rumour",
        )

    def test_wave_e_is_not_built(self) -> None:
        """Wave E was dropped; it must not come back as a promise."""
        wave = self._wave("Wave E")
        self.assertIn(
            "not built",
            wave.lower(),
            "wave E reads as something to build again. The operator dropped it "
            "on 2026-11-03: this is a single-operator, single-agent project.",
        )
        self.assertIn(
            "was dropped",
            wave.lower(),
            "wave E no longer says it was dropped — the heading alone is not "
            "enough, because the body is what a reader acts on",
        )
        self.assertIn(
            "2026-11-03",
            wave,
            "wave E does not say when it was dropped",
        )
        for tool in ("execute_code", "delegate_task"):
            with self.subTest(tool=tool):
                self.assertIn(
                    tool,
                    wave,
                    f"wave E no longer names {tool!r} — the decision is about "
                    "those two tools, and a reader must be able to see which",
                )

    def test_the_gap_is_closed_by_decision(self) -> None:
        """The gap list says the gap is closed, not merely missing."""
        gaps = self._section("## The gaps, ordered by how much they cost us")
        self.assertIn(
            "closed by decision",
            gaps.lower(),
            "the gap list no longer says gap 7 was closed by decision — a gap "
            "that reads as merely missing is a gap the next reader will "
            "schedule",
        )
        self.assertIn(
            "2026-11-03",
            gaps,
            "gap 7 does not carry the date of the decision that closed it",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
