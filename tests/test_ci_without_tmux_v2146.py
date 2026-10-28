"""The CI job that runs the suite without tmux must really do that.

**What was wrong.** The v2.14.5 wave added a CI job called "the suite
is green without tmux" — a job whose whole point is to prove that the
suite does not need tmux.  It built a directory of symlinks without
tmux and then ran the suite as::

    PATH=/tmp/no-tmux:$PATH pytest -q tests/

A prepended PATH **keeps** the original one, /usr/bin included, tmux
included.  Measured the way CI runs it::

    $ PATH=/tmp/no-tmux:$PATH command -v tmux
    /usr/bin/tmux

So the job ran the ordinary suite — with tmux — and was green for the
wrong reason.  A check that cannot be made red is a decoration, and
this one wore the costume of a green job.

**What this file does.** It *runs* the job's own two steps: it builds
the farm the way the workflow builds it, takes the PATH the workflow
runs the suite with, and asks the question the job claims to answer —
is tmux findable there?  The tripwire reads the workflow, so a
workflow that goes back to prepending PATH goes red here.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _workflow() -> str:
    return CI_WORKFLOW.read_text(encoding="utf-8")


def _step(name: str) -> str:
    """The command of the step with this name.

    A step writes its command either as a block (``run: |``) or as one
    line (``run: PATH=... pytest``), and the workflow uses both.
    """
    text = _workflow()
    marker = f"- name: {name}\n"
    start = text.find(marker)
    assert start != -1, f"the workflow has no step named {name!r}"
    rest = text[start + len(marker) :]
    block = rest.find("run: |")
    one_line = rest.find("run: ")
    if one_line != -1 and (block == -1 or one_line < block):
        # A single-line command: it ends at the end of the line.
        line = rest[one_line + len("run: ") :].split("\n", 1)[0]
        return line.strip()
    assert block != -1, f"the step {name!r} has no run block"
    body = rest[block + len("run: |") :]
    lines = []
    for line in body.split("\n"):
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line[10:] if line.startswith("          ") else line)
    return "\n".join(lines).strip("\n")


class TestTheJobReallyRunsWithoutTmux(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="no-tmux-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def _run_step(self) -> str:
        return _step("Run the suite without tmux")

    def _build_step(self) -> str:
        return _step("Build a PATH without tmux")

    def test_the_job_replaces_the_path_instead_of_prepending_it(self) -> None:
        """A prepended PATH keeps the original one — and the original has tmux."""
        run_step = self._run_step()
        self.assertIn("PATH=", run_step, "the step does not set a PATH at all")
        assignment = run_step.split("PATH=", 1)[1].split()[0]
        self.assertNotIn(
            "$PATH",
            assignment,
            "the PATH is prepended, not replaced: the original PATH stays, "
            "and it has tmux — the job runs the ordinary suite",
        )

    def test_tmux_is_not_findable_in_the_path_the_job_runs_with(self) -> None:
        """The question the job claims to answer, asked the job's way."""
        if shutil.which("tmux") is None:
            self.skipTest("this machine has no tmux to hide")

        farm = os.path.join(self.tmpdir, "no-tmux")
        build = self._build_step().replace("/tmp/no-tmux", farm)
        result = subprocess.run(
            ["bash", "-c", build],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PATH": os.environ.get("PATH", "")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.isdir(farm), "the farm was not built")

        # The PATH the workflow runs the suite with, with the farm's
        # place substituted by this run's farm — and with the shell's
        # own `$PATH` expanded, because whether the original PATH
        # survives is the whole question.
        assignment = self._run_step().split("PATH=", 1)[1].split()[0]
        effective = assignment.replace("/tmp/no-tmux", farm).replace(
            "$PATH", os.environ.get("PATH", "")
        )

        found = shutil.which("tmux", path=effective)
        self.assertIsNone(
            found,
            f"tmux is still findable at {found} — the job runs the suite with tmux, "
            "so it proves nothing about a machine without it",
        )

    def test_the_farm_carries_the_tools_the_suite_needs(self) -> None:
        """A PATH that hides tmux must not hide the interpreter."""
        if shutil.which("tmux") is None:
            self.skipTest("this machine has no tmux to hide")
        farm = os.path.join(self.tmpdir, "no-tmux")
        build = self._build_step().replace("/tmp/no-tmux", farm)
        subprocess.run(["bash", "-c", build], capture_output=True, check=False)
        found = shutil.which("python3", path=farm)
        self.assertIsNotNone(found, "the farm has no python3 — the suite could not run")


if __name__ == "__main__":
    unittest.main()
