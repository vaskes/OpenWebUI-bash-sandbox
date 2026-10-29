#!/usr/bin/env python3
"""The branch protection settings must match what the tree says they are.

The setting that matters most is the list of required status checks: it
lives in GitHub's own configuration, so the tree cannot see it, and a
required check that no run ever reports is a permanently closed gate.

Measured on 2026-10-29, on a throwaway branch with `main`'s rules: a
required context that CI never reports left every pull request at
`mergeable_state: blocked`, the merge refused with HTTP 405, and no way
out of it except a human in the branch settings. The trap waits for
`main` the moment a CI job is renamed — and the repository looks healthy
while every release is stuck.

So the list is kept in the tree (`docs/required-status-checks.txt`) and
this module checks it in both directions:

  * every context the list names is one the workflow really produces
    (after the matrix is expanded), or one of the two analyses GitHub
    itself reports;
  * every job the workflow produces is one the list names — a new job
    that nobody added to the list is a check that does not gate anything,
    which is the same drift in the other direction.

The live GitHub settings are compared too, but only when a token is
available: `GITHUB_TOKEN` in CI cannot read branch protection, and a
check that skips itself is not a check. The offline half of this module
is the part that runs everywhere, and it is the half that catches a
rename.
"""

from __future__ import annotations

import json
import os
import re
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIST_FILE = REPO_ROOT / "docs" / "required-status-checks.txt"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

REPO = "vaskes/OpenWebUI-bash-sandbox"
BRANCH = "main"

# GitHub reports these two from its own code-scanning workflow, not from
# this repository's `ci.yml`. They are required checks all the same, and
# they are listed — but they have no job in `ci.yml` to be checked
# against, so they are allowed to be there.
GITHUB_REPORTED = ("Analyze (python)", "Analyze (actions)")

# The workflow's `name:` keys, and the matrix that expands one of them.
JOB_NAME_RE = re.compile(r"^    name:\s*(.+?)\s*$")
JOB_KEY_RE = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
MATRIX_VERSIONS_RE = re.compile(r"python-version:\s*\[(.+?)\]")


def _strip_comment(line: str) -> str:
    """Drop a trailing YAML comment, keeping `#` inside quotes."""
    out = []
    quote = None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out)


def _declared_contexts() -> list[str]:
    """The contexts the repository's canonical list names.

    The file carries an explanatory header, so comments (`#`) are
    skipped. A comment line that reached the comparison would look like a
    check name and every check here would go red for the wrong reason —
    which is how a real drift gets ignored.
    """
    text = LIST_FILE.read_text(encoding="utf-8")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _workflow_push_branches() -> list[str]:
    """The branch patterns the workflow's `push:` trigger listens to.

    A text parser, not a YAML one: the repository does not depend on
    PyYAML, and a check that cannot run is a check that does not run.
    The `on:` block ends at the next top-level key, so `jobs:` never
    leaks into the answer.
    """
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    in_on = False
    in_push = False
    branches: list[str] = []
    for raw in lines:
        line = _strip_comment(raw)
        if not line.strip():
            continue
        if not in_on:
            if re.match(r"^on:\s*$", line):
                in_on = True
            continue
        if re.match(r"^\S", line):
            break
        if re.match(r"^  push:\s*$", line):
            in_push = True
            continue
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            in_push = False
            continue
        if in_push:
            found = re.match(r"^    branches:\s*\[(.+?)\]\s*$", line)
            if found:
                branches = [p.strip().strip("'\"") for p in found.group(1).split(",")]
    return branches


def _workflow_jobs() -> dict[str, list[str]]:
    """The job names `ci.yml` produces, with the matrix expanded.

    Returns a mapping of the raw `name:` template to the list of
    contexts it expands to. A job without a matrix yields one context.

    The matrix is read out of the job's own block, not from the lines
    around it: in this workflow the `name:` line comes *before* the
    matrix it interpolates, so a parser that looks backwards for the
    versions sees none and the four `Tests (Python 3.x)` contexts never
    appear — which would make every check below vacuously happy.
    """
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    blocks: list[list[str]] = []
    current: list[str] | None = None
    in_jobs = False

    for raw in lines:
        line = _strip_comment(raw)
        if not line.strip():
            continue
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if JOB_KEY_RE.match(line):
            current = []
            blocks.append(current)
            continue
        if current is not None:
            current.append(line)

    jobs: dict[str, list[str]] = {}
    for block in blocks:
        name = None
        versions: list[str] | None = None
        for line in block:
            found = JOB_NAME_RE.match(line)
            if found:
                name = found.group(1).strip().strip("'\"")
                continue
            matrix = MATRIX_VERSIONS_RE.search(line)
            if matrix and versions is None:
                versions = re.findall(r'"([^"]+)"', matrix.group(1))
        if name is None:
            continue
        jobs[name] = _expand(name, versions)
    return jobs


def _expand(template: str, versions: list[str] | None) -> list[str]:
    if versions and "${{ matrix.python-version }}" in template:
        return [template.replace("${{ matrix.python-version }}", v) for v in versions]
    return [template]


def _produced_contexts() -> set[str]:
    produced: set[str] = set()
    for contexts in _workflow_jobs().values():
        produced.update(contexts)
    return produced


class TestTheCanonicalListIsUsable(unittest.TestCase):
    """The list is the input to every other check here."""

    def test_the_list_file_exists(self) -> None:
        self.assertTrue(
            LIST_FILE.exists(),
            f"{LIST_FILE.name} is missing — it is the only place the tree "
            "knows what GitHub is told to require",
        )

    def test_the_list_is_not_empty(self) -> None:
        """A scan over an empty collection proves nothing."""
        contexts = _declared_contexts()
        self.assertGreaterEqual(
            len(contexts),
            5,
            "the canonical list is suspiciously short — did the file get truncated?",
        )

    def test_the_list_has_no_duplicates(self) -> None:
        contexts = _declared_contexts()
        seen: set[str] = set()
        dupes = sorted({c for c in contexts if c in seen or seen.add(c)})  # type: ignore[func-returns-value]
        self.assertEqual([], dupes, f"the list names a context twice: {dupes}")

    def test_every_line_is_a_context_and_not_prose(self) -> None:
        """A comment line is not a context; only the bare names are."""
        for context in _declared_contexts():
            self.assertFalse(
                context.startswith("#"),
                f"{context!r} looks like a comment, not a check name",
            )
            self.assertNotIn(
                "->",
                context,
                f"{context!r} looks like prose, not a check name",
            )


class TestTheListMatchesTheWorkflow(unittest.TestCase):
    """The workflow is the source of truth for the names."""

    def test_the_workflow_produces_at_least_one_job(self) -> None:
        jobs = _workflow_jobs()
        self.assertGreaterEqual(
            len(jobs),
            5,
            "no job names were parsed out of ci.yml — the parser is broken, "
            "and a parser that finds nothing makes every other check here vacuous",
        )

    def test_every_context_in_the_list_is_produced_by_the_workflow(self) -> None:
        produced = _produced_contexts()
        missing = [
            c for c in _declared_contexts() if c not in produced and c not in GITHUB_REPORTED
        ]
        self.assertEqual(
            [],
            missing,
            f"the list names checks the workflow never reports: {missing}. "
            "Either the job was renamed (fix the list) or the check was "
            "removed (fix the workflow) — a required check that never "
            "reports blocks every merge forever.",
        )

    def test_every_job_the_workflow_produces_is_in_the_list(self) -> None:
        """The other direction: a job nobody requires does not gate anything."""
        listed = set(_declared_contexts())
        unlisted = sorted(c for c in _produced_contexts() if c not in listed)
        self.assertEqual(
            [],
            unlisted,
            f"the workflow produces checks the list does not require: {unlisted}. "
            "A check that is not required is a decoration.",
        )

    def test_the_two_codeql_analyses_are_still_required(self) -> None:
        """GitHub reports these two itself; they are easy to forget."""
        listed = set(_declared_contexts())
        for context in GITHUB_REPORTED:
            with self.subTest(context=context):
                self.assertIn(context, listed)


class TestTheContributionGuidePointsAtTheList(unittest.TestCase):
    """The guide must not keep its own copy of the list.

    A count written in prose next to a table goes stale the moment the
    table changes, and nobody notices: the guide still reads correctly
    and the gate is somewhere else. So the guide points at the file, and
    this check makes sure it keeps pointing at it.
    """

    GUIDE = REPO_ROOT / "CONTRIBUTING.md"

    def test_the_guide_names_the_canonical_list(self) -> None:
        text = self.GUIDE.read_text(encoding="utf-8")
        self.assertIn(
            "docs/required-status-checks.txt",
            text,
            "the contribution guide does not say where the required checks live",
        )

    def test_the_guide_does_not_count_the_checks_in_prose(self) -> None:
        """`thirteen checks` is a number that rots; the file is the list."""
        text = self.GUIDE.read_text(encoding="utf-8")
        for phrase in ("thirteen checks", "fourteen checks", "twelve checks"):
            with self.subTest(phrase=phrase):
                self.assertNotIn(
                    phrase,
                    text,
                    "the guide counts the required checks in prose — that "
                    "number goes stale silently. Point at the list instead.",
                )


class TestTheGateCanRunOnABranch(unittest.TestCase):
    """The gate must be able to run on a branch, not only on a pull request.

    The workflow's `push:` trigger used to listen to `main` alone, so a
    branch push produced no check runs at all. The consequence was not
    cosmetic: the required checks could only ever be collected by a pull
    request, and opening one needs the GitHub API. With the API
    unreachable, the release was pushed, tagged, deployed and verified —
    and could not be landed, because the commits that carried it had no
    green runs to show. The gate is the checks, not the pull request: a
    branch that runs them on its own push can be landed as a
    fast-forward once they are green.

    So the trigger has to listen to every branch. If it stops, the
    landing path silently disappears and the next release gets stuck at
    "13 of 13 required status checks are expected" — with a green CI
    badge on the branch to mock it.
    """

    def test_the_gate_runs_on_every_branch_push(self) -> None:
        branches = _workflow_push_branches()
        self.assertTrue(
            branches,
            "the workflow's `push:` trigger names no branch — nothing runs on a push",
        )
        self.assertIn(
            "**",
            branches,
            "the gate listens to `main` only, so a branch push produces no check "
            "runs and a release cannot be landed without the GitHub API",
        )


class TestTheLiveSettingsMatchTheTree(unittest.TestCase):
    """The tree's list and GitHub's settings must say the same thing.

    A token is needed to read branch protection, and `GITHUB_TOKEN` in CI
    cannot. So this half runs where a token exists and skips where it
    does not — the offline half above is the part that always runs.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
        cls.live: dict | None = None
        if not cls.token:
            return
        request = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/branches/{BRANCH}/protection",
            headers={
                "Authorization": f"Bearer {cls.token}",
                "Accept": "application/vnd.github+json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                cls.live = json.load(response)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            cls.live = None

    def test_the_live_settings_have_the_same_checks(self) -> None:
        if self.live is None:
            self.skipTest("no token with permission to read branch protection")
        live = set(self.live["required_status_checks"]["contexts"])
        listed = set(_declared_contexts())
        self.assertEqual(
            listed,
            live,
            "the tree and GitHub disagree about the required checks: "
            f"only in the tree {sorted(listed - live)}, "
            f"only on GitHub {sorted(live - listed)}",
        )

    def test_the_live_settings_keep_the_forced_rules(self) -> None:
        """Force pushes and deletion stay refused; the history stays linear."""
        if self.live is None:
            self.skipTest("no token with permission to read branch protection")
        self.assertFalse(self.live["allow_force_pushes"]["enabled"])
        self.assertFalse(self.live["allow_deletions"]["enabled"])
        self.assertTrue(self.live["required_linear_history"]["enabled"])

    def test_the_live_settings_gate_administrators_too(self) -> None:
        """Measured: with this off, an admin merged a PR with zero green
        checks and pushed a merge commit into a protected branch. The gate
        existed on paper and nowhere else."""
        if self.live is None:
            self.skipTest("no token with permission to read branch protection")
        self.assertTrue(
            self.live["enforce_admins"]["enabled"],
            "administrators bypass the required checks — the gate is decoration",
        )

    def test_no_review_is_required(self) -> None:
        """The repository has one writer, so an approval gate would mean
        nobody could merge anything."""
        if self.live is None:
            self.skipTest("no token with permission to read branch protection")
        # GitHub omits the key entirely when no review is required.
        self.assertIsNone(
            self.live.get("required_pull_request_reviews"),
            "a review is required — with one writer that blocks every merge",
        )


if __name__ == "__main__":
    unittest.main()
