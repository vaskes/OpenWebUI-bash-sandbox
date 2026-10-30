"""No commit may credit a model that wrote none of it.

The story, because it explains every choice in it. This repository's
GitHub page listed two contributors: the human owner, and `claude`.
Claude Opus was there because of exactly one thing -- a

    Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

trailer on commits the agent had written. GitHub counts a co-author
trailer as a contribution, so the model appeared as a contributor having
written nothing, holding no key and opening no pull request.

`.mailmap` cannot prevent this. Git applies the file to the AUTHOR
field, GitHub applies it to the author field, and neither one applies it
to a co-author trailer -- that line is parsed as written. The trailers
had to leave the history (they did, in v2.21.0), and this test keeps
them out.

**What counts as a trailer.** By git's own convention a trailer is a
`Key: value` line in the LAST paragraph of a commit message. A check
that searches the whole message fires on the repository's own CHANGELOG
quoting the trailer in prose -- and a check that fires on a document
describing the problem is a check that gets disabled by whoever writes
the next document about it. The boundary is explicit here, and pinned by
the controls at the bottom.

What it reads: the commit messages of whatever history the checkout can
see. In CI the checkout is shallow, so that is the HEAD commit alone --
which is the case that matters, because a new commit is where a trailer
would reappear. Locally it is the whole history.

This is written for `unittest`, because the count in the README is
counted with `unittest` -- a pytest-style file would run and not be
counted, and the count is a claim about the tree.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The addresses that resolve to an account belonging to a model. The
#: model NAME in the trailer changes with the model; the address is what
#: identifies the account, so pin the addresses, not the names.
FORBIDDEN = (
    "noreply@anthropic.com",  # Claude
    "noreply@mavis.local",  # Mavis
    "noreply@MiniMax",  # Mavis
)

TRAILER_KEY = "Co-Authored-By:"

# filter-branch's backup of the pre-rewrite history: a LOCAL artifact
# holding the commits you already removed. Reading it would keep the
# check red forever after a successful rewrite.
SKIP_REF_PREFIXES = (
    "refs/original/",
    "refs/filter-repo/",
    "refs/bisect/",
    "refs/worktree/",
)


def _git(*args: str) -> subprocess.CompletedProcess:
    # safe.directory: the checkout is often owned by another uid than the
    # one running the suite -- a containerised run against a host-mounted
    # clone, for instance. Without it git refuses to read the repository
    # and the check skips itself, which is the one outcome that must not
    # be silent.
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _refs() -> list[str]:
    """The refs whose history is the repository's history."""
    proc = _git("for-each-ref", "--format=%(refname)")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    refs = [
        r.strip()
        for r in proc.stdout.splitlines()
        if r.strip() and not r.startswith(SKIP_REF_PREFIXES)
    ]
    return refs or ["HEAD"]


def _trailer_lines(message: str) -> list[str]:
    """The trailer block of one commit message: its last paragraph."""
    blocks = [b for b in message.strip().split("\n\n") if b.strip()]
    if not blocks:
        return []
    return blocks[-1].split("\n")


def _credits_a_model(message: str) -> list[str]:
    """The detector, separated so that the controls can call it."""
    return [
        line
        for line in _trailer_lines(message)
        if line.startswith(TRAILER_KEY) and any(address in line for address in FORBIDDEN)
    ]


class NoCommitCreditsAModel(unittest.TestCase):
    def setUp(self) -> None:
        proc = _git("log", "--format=%B%x00", *_refs())
        if proc.returncode != 0:
            self.skipTest(f"git history is not readable here: {proc.stderr.strip()}")
        self.messages = proc.stdout

    def test_no_commit_credits_a_model(self) -> None:
        """The property: no commit message carries a model's trailer."""
        hits: list[str] = []
        for message in self.messages.split("\x00"):
            hits.extend(_credits_a_model(message))
        self.assertEqual(
            hits,
            [],
            f"{len(hits)} commit message(s) credit a model with one of "
            f"{', '.join(FORBIDDEN)}. GitHub counts a co-author trailer as a "
            "contribution, so the model shows up on the contributors list "
            "without having written a line of this repository. Remove the "
            "trailer from the commit.\n  " + "\n  ".join(hits[:5]),
        )

    def test_the_history_is_read_at_all(self) -> None:
        """Control: the check above must be looking at something."""
        self.assertTrue(
            self.messages.strip(),
            "git log returned nothing -- the check above is vacuous, and it "
            "would pass on a repository with every trailer still in place",
        )


class TheDetectorCanSeeWhatItWasWrittenFor(unittest.TestCase):
    """The controls. They are what makes the green run above mean
    something: a detector that cannot fire is not a check."""

    def test_the_detector_can_see_a_trailer(self) -> None:
        sample = (
            "fix(x): something\n\n"
            "The body says why.\n\n"
            "Co-Authored-By: Claude Opus 4.8 (1M context) "
            "<noreply@anthropic.com>\n"
        )
        self.assertTrue(
            _credits_a_model(sample),
            "the detector cannot see the thing it was written for, so every "
            "green run of the check above means nothing",
        )

    def test_the_detector_sees_every_forbidden_address(self) -> None:
        for address in FORBIDDEN:
            sample = f"fix(x): something\n\nbody\n\nCo-Authored-By: Some Model <{address}>\n"
            self.assertTrue(
                _credits_a_model(sample),
                f"the detector does not see {address}",
            )

    def test_a_mention_in_the_body_is_not_a_trailer(self) -> None:
        """The boundary. A document quoting the trailer is not a commit
        crediting the model -- and a check that cannot tell the two apart
        fires on every document describing the problem."""
        sample = (
            "fix(x): something\n\n"
            "The body quotes the old trailer:\n\n"
            "    Co-Authored-By: Claude Opus 4.8 (1M context) "
            "<noreply@anthropic.com>\n\n"
            "and says it was removed.\n"
        )
        self.assertFalse(
            _credits_a_model(sample),
            "a quoted trailer in the body was read as a real one -- the check "
            "would fire on any document describing the problem",
        )

    def test_a_human_co_author_is_not_a_model(self) -> None:
        sample = (
            "fix(x): something\n\nbody\n\nCo-Authored-By: Vasisualy Lohankin <vaskes@mail.ru>\n"
        )
        self.assertFalse(
            _credits_a_model(sample),
            "a human co-author was read as a model",
        )


class GitIsAvailableWhereTheSuiteRuns(unittest.TestCase):
    def test_git_can_read_this_checkout_here(self) -> None:
        """The suite image must be able to read the history.

        The check above skips itself where git is missing, and a skipped
        check is invisible: the suite stays green and the trailer comes
        back. This test fails LOUDLY instead, so the image gets git.
        Delete it only if the suite really never has a checkout."""
        proc = _git("rev-parse", "--git-dir")
        self.assertEqual(
            proc.returncode,
            0,
            "git cannot read this checkout here: "
            f"{proc.stderr.strip() or 'git is missing from the image'}. "
            "The trailer check skips itself in that case, so install git in "
            "the image that runs the suite, or the check protects nothing.",
        )


if __name__ == "__main__":
    unittest.main()
