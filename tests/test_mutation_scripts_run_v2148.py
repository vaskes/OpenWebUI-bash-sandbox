"""The mutation checks must not lie about what they caught.

The v2.14.7 tree shipped ten mutation scripts that could report a catch for
a test that never ran:

* two of them ran pytest through a hardcoded interpreter path that does not
  exist in CI. The shell answered 127 ("command not found"), the
  ``if pytest; then FAIL; else PASS`` shape read that as "the test went
  red", and the scripts printed ten and eleven catches for tests that never
  started. Twenty-one of the 113 mutations of that release never ran a
  test, and the release notes counted them as caught.
* a selector that matches no test makes pytest exit 5 ("no tests ran"),
  which is not a failure either — and the scripts counted that as a catch
  as well.

These tests run the scripts for real, in a copy of the tree, against a
python that pretends to be a python with pytest:

* one that collects nothing => the script must say INVALID;
* a python that cannot import pytest => the script must refuse to run
  (exit 2) instead of counting catches.

The scripts are run in a copy because they break files on purpose: a copy
keeps the tree they run in out of the way of the tree the suite runs in.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
LIB = TOOLS / "mutation-lib.sh"

# A line that starts with "OK" is a catch: the test ran and went red.
A_CATCH = re.compile(r"^OK\s", re.MULTILINE)

# An interpreter path that names a machine instead of the repository.
A_MACHINE_PATH = re.compile(r"/(?:workspace|opt|home|Users)/[^\s\"']*python")

# A python that has pytest and collects nothing.
COLLECTS_NOTHING = """#!/usr/bin/env bash
case "${1:-}" in
    -c) exit 0 ;;
    -m) echo "no tests ran in 0.01s"; exit 5 ;;
    *) exec "__PYTHON__" "$@" ;;
esac
"""

# A python without pytest: it cannot even import it.
NO_PYTEST = """#!/usr/bin/env bash
if [ "${1:-}" = "-c" ]; then
    exit 1
fi
if [ "${1:-}" = "-m" ]; then
    # A python without pytest cannot run a test either.  It must not
    # hand the call to a real python: the point of this stub is to be
    # fast, and a real suite would be slow.
    exit 1
fi
exec "__PYTHON__" "$@"
"""


def _scripts() -> list[Path]:
    return sorted(TOOLS.glob("mutation-check-*.sh"))


def _scripts_to_run() -> list[Path]:
    """The scripts this test may run.

    A script that breaks *this* test on purpose cannot be run by this
    test: the script would run the test, which would run the script,
    which would run the test.  Those scripts are checked by the wave's
    own mutation script — that is where their "the tripwire can still go
    red" proof lives.
    """
    here = f"{Path(__file__).name}"
    return [script for script in _scripts() if here not in script.read_text(encoding="utf-8")]


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


def _run(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)],
        cwd=str(script.parent.parent),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


class TestTheMutationScriptsDoNotLie(unittest.TestCase):
    def test_a_run_that_collected_nothing_is_not_a_catch(self) -> None:
        """A test that never ran is not a caught mutation."""
        import tempfile  # noqa: PLC0415

        scripts = _scripts_to_run()
        self.assertTrue(scripts, "the repository has no mutation scripts")
        for script in scripts:
            with self.subTest(script=script.name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    repo = _copy_of_the_tree(root / "tree")
                    stub_dir = root / "bin"
                    stub_dir.mkdir()
                    stub = _interpreter(stub_dir, "python-with-pytest", COLLECTS_NOTHING)
                    env = dict(os.environ)
                    env["PY"] = str(stub)
                    result = _run(repo / "tools" / script.name, env)
                self.assertNotEqual(
                    result.returncode,
                    0,
                    f"{script.name} reported success for tests that never ran",
                )
                self.assertIn(
                    "INVALID",
                    result.stdout,
                    f"{script.name} did not say that the test did not run: {result.stdout}",
                )
                self.assertIsNone(
                    A_CATCH.search(result.stdout),
                    f"{script.name} counted a catch for a test that never ran",
                )

    def test_a_python_without_pytest_is_refused_loudly(self) -> None:
        """No interpreter with pytest => the script refuses, it does not count."""
        import tempfile  # noqa: PLC0415

        scripts = _scripts_to_run()
        self.assertTrue(scripts, "the repository has no mutation scripts")
        for script in scripts:
            with self.subTest(script=script.name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    repo = _copy_of_the_tree(root / "tree")
                    stub_dir = root / "bin"
                    stub_dir.mkdir()
                    _interpreter(stub_dir, "python3", NO_PYTEST)
                    _interpreter(stub_dir, "python", NO_PYTEST)
                    env = dict(os.environ)
                    env.pop("PY", None)
                    env["PATH"] = f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}"
                    result = _run(repo / "tools" / script.name, env)
                self.assertEqual(
                    result.returncode,
                    2,
                    f"{script.name} did not refuse to run without pytest: {result.stdout}",
                )
                self.assertIn(
                    "no python with pytest found",
                    result.stderr,
                    f"{script.name} refused silently: {result.stderr}",
                )
                self.assertIsNone(
                    A_CATCH.search(result.stdout),
                    f"{script.name} counted a catch without an interpreter",
                )

    def test_a_python_set_but_without_pytest_is_refused_loudly(self) -> None:
        """PY is honoured only when it can import pytest."""
        import tempfile  # noqa: PLC0415

        scripts = _scripts_to_run()
        self.assertTrue(scripts, "the repository has no mutation scripts")
        for script in scripts:
            with self.subTest(script=script.name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    repo = _copy_of_the_tree(root / "tree")
                    stub_dir = root / "bin"
                    stub_dir.mkdir()
                    stub = _interpreter(stub_dir, "python-without-pytest", NO_PYTEST)
                    env = dict(os.environ)
                    env["PY"] = str(stub)
                    result = _run(repo / "tools" / script.name, env)
                self.assertEqual(
                    result.returncode,
                    2,
                    f"{script.name} did not refuse a PY without pytest: {result.stdout}",
                )
                self.assertIn(
                    "cannot import pytest",
                    result.stderr,
                    f"{script.name} refused silently: {result.stderr}",
                )

    def test_no_mutation_script_names_a_machine_interpreter(self) -> None:
        """The interpreter is the caller's business, not the script's."""
        # A script that breaks this test on purpose may name the thing it
        # breaks — that is what the mutation is.  The others may not.
        paths = _scripts_to_run() + [LIB]
        self.assertTrue(paths, "the repository has no mutation scripts")
        for path in paths:
            with self.subTest(script=path.name):
                text = path.read_text(encoding="utf-8")
                found = A_MACHINE_PATH.search(text)
                self.assertIsNone(
                    found,
                    f"{path.name} hardcodes the interpreter of a machine: {found}",
                )


class TestTheVerdictComesFromTheExitCode(unittest.TestCase):
    """A run that failed is a catch, whatever its output happens to say.

    Measured on 2026-10-29: the library decided whether a test had run by
    grepping pytest's whole output for "no tests ran", "ERROR: not found"
    and friends. A failing test prints the assertion's message, and that
    message may quote a file that contains one of those phrases. The
    library quotes itself — so the mutation that renamed a function made
    the test print the library's own source, with "ERROR: not found"
    inside it, and the script called a caught mutation INVALID. The three
    real failures in that run were reported as "the test did not run".

    pytest's exit code is not quotable: 0 means everything passed, 1 means
    a test ran and failed, and every other code means it did not run.
    """

    def _run_the_library(self, body: str) -> str:
        """Run the library's verdict function over a fake pytest."""
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "case.sh"
            script.write_text(body, encoding="utf-8")
            result = subprocess.run(
                ["bash", str(script)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def _case(self, exit_code: int, output: str = "") -> str:
        return (
            "set -u\n"
            f'. "{LIB}"\n'
            "PASS=0\n"
            "FAIL=0\n"
            f"rc={exit_code}\n"
            f"out=$(mktemp)\n"
            f"printf '%s' {output!r} > \"$out\"\n"
            'if mutation_run_was_invalid "$rc"; then\n'
            "  echo INVALID\n"
            'elif [ "$rc" -eq 1 ]; then\n'
            "  echo CAUGHT\n"
            "else\n"
            "  echo GREEN\n"
            "fi\n"
        )

    def test_a_failure_is_a_catch(self) -> None:
        self.assertEqual(self._run_the_library(self._case(1)), "CAUGHT")

    def test_a_failure_whose_output_quotes_the_phrase_is_still_a_catch(self) -> None:
        """The exact case that was measured: the output says "ERROR: not found"."""
        verdict = self._run_the_library(self._case(1, "ERROR: not found\nno tests ran in 0.01s\n"))
        self.assertEqual(
            verdict,
            "CAUGHT",
            "a run that failed was called INVALID because its output quoted a phrase",
        )

    def test_a_pass_is_green(self) -> None:
        self.assertEqual(self._run_the_library(self._case(0)), "GREEN")

    def test_nothing_collected_is_invalid(self) -> None:
        self.assertEqual(self._run_the_library(self._case(5)), "INVALID")

    def test_a_usage_error_is_invalid(self) -> None:
        self.assertEqual(self._run_the_library(self._case(4)), "INVALID")

    def test_a_collection_error_is_invalid(self) -> None:
        self.assertEqual(self._run_the_library(self._case(2)), "INVALID")

    def test_an_internal_error_is_invalid(self) -> None:
        self.assertEqual(self._run_the_library(self._case(3)), "INVALID")


if __name__ == "__main__":
    unittest.main()
