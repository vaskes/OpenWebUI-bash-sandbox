"""The gate has to be able to see the history it judges.

**Measured 2026-10-31 on this repository.** A pull-request checkout by
`actions/checkout` is, by default, a **depth-1** checkout of the MERGE
ref. `git log` in that checkout sees exactly one commit -- the merge
commit -- and the messages of the commits of the branch itself are not
in it at all.

The tripwire that keeps a model out of the contributors list
(`tests/test_no_model_coauthor_trailers.py`) reads the commit messages
of the history. On a depth-1 merge-ref checkout it therefore reads the
merge commit's message and nothing else, and it reports GREEN on a
branch that carries the credit. The check was not wrong about what it
saw; it was blind.

Both halves are measured here, and the A/B is the point:

```
  full history  -> RED    (the credit is found)
  depth-1       -> GREEN  (the credit is not there to be found)
```

This file has two halves:

* the **config half** -- the jobs that run the suite must check out the
  whole history, and this test says so in the same commit that changes
  it, so the blindness cannot come back unnoticed;
* the **self-check half** -- the tripwire that reads the history must
  notice that it cannot see it, rather than reporting a clean bill of
  health on a tree it never looked at.

The second half is the one that matters. The first half is a fact about
a YAML file, and it can be edited away by anyone; the second half is a
property of the check itself, and it is what turns "the gate was blind"
from a mystery into a red job.

**What counts as "blind".** `git rev-parse --is-shallow-repository`
answers it exactly: `true` in a shallow checkout, `false` in a full
one. It is the same answer for a `--depth 1` clone and for a merge-ref
checkout, and it needs no network and no assumptions about the server.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

#: The jobs that run the suite, or the harnesses that run it. A checkout
#: that feeds one of them has to carry the history the tripwires read:
#:
#: * `test` -- the suite itself, on four Python versions;
#: * `suite-without-tmux` -- the same suite with tmux taken away, which
#:   is where the isolation regressions show up;
#: * `mutation-checks` -- the mutation harnesses, and one of them breaks
#:   the model-attribution detector on purpose.
SUITE_JOBS = ("test", "suite-without-tmux", "mutation-checks")

#: `actions/checkout` with this input fetches the whole history.
FULL_HISTORY = "fetch-depth: 0"


def _git(*args: str) -> subprocess.CompletedProcess:
    # safe.directory: the checkout is often owned by another uid than the
    # one running the suite. Without it git refuses to read the
    # repository, and the check would report blindness that is not
    # there.
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _job_blocks(text: str) -> dict[str, list[str]]:
    """The body of every job in the workflow, by job name."""
    lines = text.splitlines()
    jobs: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        stripped = line.strip()
        if line.startswith("  ") and not line.startswith("   ") and stripped.endswith(":"):
            current = stripped[:-1]
            jobs[current] = []
            continue
        if current is not None and line.startswith("    "):
            jobs[current].append(line)
    return jobs


def _checkout_steps(body: list[str]) -> list[list[str]]:
    """Each `actions/checkout` step of a job, with its `with:` block.

    The step and its `with:` block are taken together, because the
    setting lives in the block: a checkout that fetches the whole
    history says so in the same few lines that name the action.

    A comment inside the block does not end it. That is not a detail:
    the first version of this parser stopped at the first line it did
    not recognise, so a `with:` block whose `fetch-depth:` sat under an
    explanatory comment was read as a block without it -- and this
    tripwire reported the workflow as shallow while the workflow was
    exactly right. A check that cannot read the thing it is checking
    fails in the direction that costs the reader a false alarm, so the
    comment lines are skipped rather than treated as the end.
    """
    steps: list[list[str]] = []
    for i, line in enumerate(body):
        if "actions/checkout" not in line:
            continue
        block = [line]
        for follow in body[i + 1 :]:
            if not follow.strip():
                break
            indent = len(follow) - len(follow.lstrip())
            if follow.strip().startswith("#"):
                block.append(follow)
                continue
            if indent <= 6:
                break
            block.append(follow)
        steps.append(block)
    return steps


class TheGateCanSeeTheHistory(unittest.TestCase):
    """The config half: the jobs that read the history must fetch it."""

    def setUp(self) -> None:
        self.assertTrue(
            WORKFLOW.is_file(),
            f"{WORKFLOW} does not exist, so there is nothing to check",
        )
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.jobs = _job_blocks(self.text)

    def test_the_workflow_has_the_jobs_we_think_it_has(self) -> None:
        """Control: the check above must be looking at something."""
        for job in SUITE_JOBS:
            self.assertIn(
                job,
                self.jobs,
                f"the workflow has no job named {job!r}; the jobs are "
                f"{sorted(self.jobs)}. A renamed job would make the check "
                "above pass without checking anything.",
            )

    def test_the_jobs_that_run_the_suite_fetch_the_whole_history(self) -> None:
        for job in SUITE_JOBS:
            steps = _checkout_steps(self.jobs[job])
            self.assertTrue(
                steps,
                f"the job {job!r} has no checkout step, so it cannot read any history at all",
            )
            for block in steps:
                self.assertTrue(
                    any(FULL_HISTORY in line for line in block),
                    f"the job {job!r} checks out a shallow history:\n"
                    + "\n".join(block)
                    + "\n\nThe tripwires read the commit messages of the "
                    "repository, and a depth-1 pull-request checkout is the "
                    "MERGE ref: one synthetic commit, and none of the "
                    "branch's own messages. The gate would be blind exactly "
                    f"where it matters. Add `{FULL_HISTORY}` to the checkout.",
                )


class TheCheckoutHereIsNotBlind(unittest.TestCase):
    """The self-check half: the check must know what it can see.

    This is the half that turns "the gate was blind" from a mystery into
    a red job. A shallow checkout is not a condition to skip on: a
    skipped check is an invisible check, and the whole point of the
    tripwire is that it cannot report green on a tree it never looked
    at.
    """

    def test_this_checkout_is_not_shallow(self) -> None:
        proc = _git("rev-parse", "--is-shallow-repository")
        self.assertEqual(
            proc.returncode,
            0,
            "git cannot read this checkout here: "
            f"{proc.stderr.strip() or 'git is missing from the image'}. "
            "The history-reading tripwires skip themselves in that case, "
            "so install git in the image that runs the suite, or the gate "
            "protects nothing.",
        )
        shallow = proc.stdout.strip()
        self.assertEqual(
            shallow,
            "false",
            "this checkout is SHALLOW, so the tripwires that read the "
            "history cannot see it, and their green means nothing. The "
            "checkout of every job that runs the suite has to say "
            f"`{FULL_HISTORY}`. In a pull request a depth-1 checkout is "
            "the MERGE ref: one synthetic commit, and none of the "
            "branch's own messages -- so a branch that credits a model "
            "passes the gate.",
        )

    def test_the_history_is_readable_here(self) -> None:
        """Control: the check above must have something to look at."""
        proc = _git("log", "--format=%H", "--all")
        self.assertEqual(
            proc.returncode,
            0,
            f"git log failed here: {proc.stderr.strip()}",
        )
        commits = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertTrue(
            commits,
            "git log --all returned nothing -- the history-reading "
            "tripwires would pass on a tree nobody looked at",
        )


if __name__ == "__main__":
    unittest.main()
