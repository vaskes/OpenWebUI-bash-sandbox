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

**Two doors, not one.** The trailer is the door everyone remembers.
The author line is the second one, and it was open here too: a commit
whose `author=` is a model's address puts the model on the contributors
list with no trailer at all. Measured on another repository: 10 052
commits authored `noreply@anthropic.com`, and the panel showed `claude`
next to the human owner. So the author slot is checked as well, and the
address is read RAW -- `%ae`, not `%aE` -- because GitHub's own rule is
about "the email address used to make or co-author the commits", i.e.
the address as it is written in the commit. `.mailmap` rewrites the
author field for display; it does not un-credit the commit.

The committer is NOT a door, and that is measured rather than assumed:
on a repository with 0 model authors, 18 model committers and 37 model
trailers, the contributors total was 37 -- the trailers, and not 37+18.
So the committer is left alone on purpose, and saying so here keeps the
next reader from "fixing" it.

**The key is case-insensitive.** Git's own parser does not care:
`git interpret-trailers --parse` treats `co-authored-by:` as a trailer.
GitHub's documentation writes the key as `Co-authored-by:`, and this
repository's own history uses that spelling 13 times. A detector that
matches the key exactly does not see the spelling the documentation and
the house style use -- which is the spelling a new credit would be
written in.

**Only addresses that resolve to an account are forbidden.** An address
that resolves to no GitHub account cannot reach the contributors list:
there is no account to attribute it to. Forbidding such an address is
the list that rots -- the entries never appear in the repository, so
nobody notices they are there, and the first time somebody disables the
check "because it does not work anyway" the real door is open. So the
list below is exactly the set of addresses that resolve to an account,
with the account and its uid measured on 2026-10-31 through the commits
API. The control at the bottom compares the two lists, so a list that
rots is noticed instead of accumulating.

What it reads: the commit messages and the author addresses of whatever
history the checkout can see -- and it knows what that is, because
`tests/test_ci_history_depth.py` refuses to let a shallow checkout pass
as a full one.

This is written for `unittest`, because the count in the README is
counted with `unittest` -- a pytest-style file would run and not be
counted, and the count is a claim about the tree.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The addresses that resolve to an account belonging to a model, and the
#: account each of them resolves to. Measured on 2026-10-31 through the
#: commits API:
#:
#:     noreply@anthropic.com    -> claude        uid 81847
#:     noreply@openai.com       -> codex         uid 267193182
#:     cursoragent@cursor.com   -> cursoragent   uid 199161495
#:
#: The model NAME changes with the model; the address is what identifies
#: the account, so the addresses are pinned, not the names.
RESOLVES_TO_ACCOUNT = {
    "noreply@anthropic.com": ("claude", 81847),
    "noreply@openai.com": ("codex", 267193182),
    "cursoragent@cursor.com": ("cursoragent", 199161495),
}

#: The addresses the detector forbids. It is the measured set and
#: nothing else: an address that resolves to no account cannot reach the
#: contributors list, so forbidding it would only make the list rot.
FORBIDDEN = tuple(RESOLVES_TO_ACCOUNT)

#: The trailer key, in the spelling git itself uses. The comparison is
#: case-insensitive; see the module docstring.
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


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    # safe.directory: the checkout is often owned by another uid than the
    # one running the suite -- a containerised run against a host-mounted
    # clone, for instance. Without it git refuses to read the repository
    # and the check skips itself, which is the one outcome that must not
    # be silent.
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=cwd or ROOT,
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


def _is_a_trailer_key(line: str) -> bool:
    """Whether a line starts a trailer, in any spelling of the key.

    Git does not care about the case of a trailer key, and neither does
    GitHub's documentation, which writes it `Co-authored-by:`.
    """
    return line.lower().startswith(TRAILER_KEY.lower())


def _names_a_forbidden_address(text: str) -> bool:
    lowered = text.lower()
    return any(address in lowered for address in FORBIDDEN)


def _credits_a_model(message: str) -> list[str]:
    """The detector, separated so that the controls can call it."""
    return [
        line
        for line in _trailer_lines(message)
        if _is_a_trailer_key(line) and _names_a_forbidden_address(line)
    ]


def _author_credits_a_model(author_email: str) -> bool:
    """Whether the AUTHOR slot holds a model's address.

    The author is a door of its own: a commit whose author is a model
    puts that model on the contributors list with no trailer anywhere.
    """
    return author_email.strip().lower() in FORBIDDEN


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

    def test_no_commit_is_authored_by_a_model(self) -> None:
        """The property: no commit is AUTHORED by a model's address.

        The trailer is the door everyone remembers; this is the second
        one, and it puts the model on the same list on its own.
        """
        proc = _git("log", "--format=%ae%x00", *_refs())
        if proc.returncode != 0:
            self.skipTest(f"git history is not readable here: {proc.stderr.strip()}")
        hits = [
            address
            for address in proc.stdout.split("\x00")
            if address.strip() and _author_credits_a_model(address)
        ]
        self.assertEqual(
            hits,
            [],
            f"{len(hits)} commit(s) are authored by a model ({', '.join(FORBIDDEN)}). "
            "GitHub counts the author of a commit as a contributor, so the "
            "model shows up on the contributors list without having written "
            "a line of this repository. Rewrite the commit with the owner's "
            "identity -- an amend is enough.\n  " + "\n  ".join(hits[:5]),
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

    def test_the_detector_sees_a_trailer_in_any_case(self) -> None:
        """The key is case-insensitive, like git's own parser.

        GitHub's documentation writes the key `Co-authored-by:`, and this
        repository's own history uses that spelling. A detector that
        matches the key exactly does not see the spelling a new credit
        would be written in.
        """
        for key in ("Co-Authored-By:", "Co-authored-by:", "co-authored-by:", "CO-AUTHORED-BY:"):
            sample = f"fix(x): something\n\nbody\n\n{key} Claude <noreply@anthropic.com>\n"
            self.assertTrue(
                _credits_a_model(sample),
                f"the detector does not see the trailer written as {key!r}, and "
                "git's own parser does see it: `git interpret-trailers --parse` "
                "treats the key as a trailer in any case",
            )

    def test_the_detector_sees_a_model_in_the_author_slot(self) -> None:
        """The author is a door of its own."""
        self.assertTrue(
            _author_credits_a_model("noreply@anthropic.com"),
            "the detector does not see a commit authored by a model, so a "
            "model can be put on the contributors list with no trailer at all",
        )

    def test_the_detector_sees_every_forbidden_address(self) -> None:
        for address in FORBIDDEN:
            sample = f"fix(x): something\n\nbody\n\nCo-Authored-By: Some Model <{address}>\n"
            self.assertTrue(
                _credits_a_model(sample),
                f"the detector does not see {address}",
            )
            self.assertTrue(
                _author_credits_a_model(address),
                f"the detector does not see {address} in the author slot",
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

    def test_a_human_author_is_not_a_model(self) -> None:
        self.assertFalse(
            _author_credits_a_model("vaskes@mail.ru"),
            "the owner's own address was read as a model",
        )


class TheForbiddenListIsMeasured(unittest.TestCase):
    """The list of addresses is a measurement, and a measurement rots.

    The control used to compare the detector against the detector's own
    list, so the list could keep entries that never occur in the
    repository -- and nobody would notice, because nothing would change
    when they were removed. The list is now the set of addresses that
    resolve to an account, and this control compares the two, so a list
    that rots is a red build instead of a slow decay.
    """

    def test_the_forbidden_list_is_exactly_the_measured_one(self) -> None:
        self.assertEqual(
            set(FORBIDDEN),
            set(RESOLVES_TO_ACCOUNT),
            "the forbidden addresses and the measured addresses disagree: "
            f"forbidden={sorted(FORBIDDEN)} measured={sorted(RESOLVES_TO_ACCOUNT)}. "
            "An address that resolves to no account cannot reach the "
            "contributors list, and an address that resolves to an account "
            "must be forbidden.",
        )

    def test_the_measured_list_names_an_account_and_a_uid(self) -> None:
        """uid, not the login: a login can be renamed, a uid cannot."""
        for address, (login, uid) in RESOLVES_TO_ACCOUNT.items():
            self.assertTrue(
                login and isinstance(uid, int) and uid > 0,
                f"{address} has no account name and uid: {login!r}, {uid!r}",
            )

    def test_a_dead_address_would_not_be_forbidden(self) -> None:
        """The shape of the rule, stated as a control.

        An address that resolves to no account -- `mavis@kuduza.ai`,
        `agent@mavis.local` -- cannot reach the contributors list. It is
        in this repository's history, it is not forbidden, and the check
        stays green. That is the rule, not an oversight.
        """
        for address in ("mavis@kuduza.ai", "agent@mavis.local"):
            self.assertNotIn(address, RESOLVES_TO_ACCOUNT)
            self.assertFalse(
                _author_credits_a_model(address),
                f"{address} resolves to no account, so it cannot be on the "
                "contributors list, and forbidding it is how a list rots",
            )


class TheAuthorIsReadRaw(unittest.TestCase):
    """`.mailmap` rewrites the author for display; it does not un-credit
    the commit. GitHub's rule is about the address as it is written in the
    commit, so the check reads `%ae` and not `%aE`."""

    def test_mailmap_does_not_hide_the_author(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            run = lambda *args: _git(*args, cwd=repo)  # noqa: E731
            run("init", "-q", ".")
            run("config", "user.name", "Owner")
            run("config", "user.email", "owner@example.invalid")
            (repo / ".mailmap").write_text(
                "<owner@example.invalid> <noreply@anthropic.com>\n", encoding="utf-8"
            )
            (repo / "file.txt").write_text("x\n", encoding="utf-8")
            run("add", "-A")
            env = {
                **dict(__import__("os").environ),
                "GIT_AUTHOR_NAME": "Claude",
                "GIT_AUTHOR_EMAIL": "noreply@anthropic.com",
                "GIT_COMMITTER_NAME": "Owner",
                "GIT_COMMITTER_EMAIL": "owner@example.invalid",
            }
            subprocess.run(
                ["git", "-c", "safe.directory=*", "commit", "-q", "-m", "a commit"],
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            raw = run("log", "--format=%ae")
            mapped = run("log", "--format=%aE")
            self.assertEqual(
                raw.stdout.strip(),
                "noreply@anthropic.com",
                "git did not report the raw author address, so the check "
                "cannot be measuring what GitHub counts",
            )
            self.assertEqual(
                mapped.stdout.strip(),
                "owner@example.invalid",
                "git did not apply the mailmap, so this control is not measuring anything",
            )
            self.assertTrue(
                _author_credits_a_model(raw.stdout.strip()),
                "the raw author address was not recognised as a model's, so a "
                "mailmap could hide a model from the contributors list",
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
