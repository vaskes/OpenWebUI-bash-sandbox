"""A skipped test is not a green test — the harness has to say so.

Measured 2026-11-04: a sandbox whose tmux had been killed by a recreate
made the ``@requires_tmux`` tests **skip**. pytest exits 0 when every
selected test was skipped, and the mutation harness read that exit code
as "the test stayed green" — so three mutations reported a green test
for tests that never ran. The verdict was wrong in the direction that
matters: it looked like a property had been unpinned, when in fact the
environment had swallowed the test.

The harness now reads pytest's summary: a run that skipped is a run
that did not test anything, and it is INVALID — in the preflight (which
stops the sweep before it starts) and in the verdict of a mutation.

These tests run a real mutation script against a python that pretends
to be a python with pytest — one whose pytest reports a skip.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
LIB = TOOLS / "mutation-lib.sh"

#: A python with pytest whose pytest SKIPS everything and exits 0 —
#: which is what pytest does when every selected test is skipped.
SKIPS_EVERYTHING = """#!/usr/bin/env bash
case "${1:-}" in
    -c) exit 0 ;;
    -m) echo "1 skipped in 0.01s"; exit 0 ;;
    *) exec "__PYTHON__" "$@" ;;
esac
"""

#: A script WITH a preflight: it checks the tripwire is green before it
#: breaks anything, so a skipped tripwire stops the sweep there and
#: then.
WITH_PREFLIGHT = "mutation-check-v2280.sh"

#: A script WITHOUT a preflight (the older ones do not have one): every
#: mutation is classified on its own, and a skipped test is INVALID
#: there too.
WITHOUT_PREFLIGHT = "mutation-check-v2121.sh"


def _copy_of_the_tree(into: Path) -> Path:
    shutil.copytree(
        ROOT,
        into,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
    )
    return into


def _interpreter(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body.replace("__PYTHON__", sys.executable), encoding="utf-8")
    path.chmod(0o755)
    return path


class TestASkippedTestIsNotAGreenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"owbs-skip-{os.getpid()}"
        if self.tmp.exists():
            shutil.rmtree(self.tmp, ignore_errors=True)
        self.tree = _copy_of_the_tree(self.tmp)
        self.fake = _interpreter(self.tmp, "fake-python", SKIPS_EVERYTHING)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _run(self, script: str) -> tuple[subprocess.CompletedProcess[str], str]:
        env = dict(os.environ)
        env["PY"] = str(self.fake)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            ["bash", str(self.tree / "tools" / script)],
            cwd=str(self.tree),
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        return result, result.stdout + result.stderr

    def test_the_preflight_refuses_a_tripwire_that_skipped(self) -> None:
        result, output = self._run(WITH_PREFLIGHT)
        self.assertIn(
            "INVALID",
            output,
            "a tripwire that was SKIPPED must be INVALID, not green:\n" + output,
        )
        self.assertNotIn(
            "the tripwire is green on the clean tree",
            output,
            "the preflight called a skipped tripwire green:\n" + output,
        )
        self.assertNotIn(
            "OK ",
            output,
            "a mutation cannot be a catch when its test never ran:\n" + output,
        )
        self.assertEqual(
            result.returncode,
            2,
            "the sweep must stop before it starts, not count anything:\n" + output,
        )

    def test_a_skipped_test_is_never_a_catch(self) -> None:
        """Even a script without a preflight must not count a skip."""
        result, output = self._run(WITHOUT_PREFLIGHT)
        self.assertNotIn(
            "OK ",
            output,
            "a mutation whose test was SKIPPED was counted as a catch:\n" + output,
        )
        self.assertIn(
            "SKIPPED",
            output,
            "the verdict must say the test was skipped, not that it stayed green:\n"
            + output,
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "a sweep that measured nothing must not end green:\n" + output,
        )

    def test_the_harness_names_the_skip_outright(self) -> None:
        """The helper has to exist AND be called where it matters."""
        text = LIB.read_text(encoding="utf-8")
        self.assertIn(
            "mutation_run_skipped() {",
            text,
            "the harness has no helper that reads pytest's summary",
        )
        self.assertIn(
            'if mutation_run_skipped "$out"; then\n'
            '        echo "INVALID — the tripwire did not RUN',
            text,
            "the preflight does not consult the summary",
        )
        self.assertIn(
            'if mutation_run_skipped "$out"; then\n'
            '            echo "INVALID $label',
            text,
            "the verdict of a mutation does not consult the summary",
        )


if __name__ == "__main__":
    unittest.main()
