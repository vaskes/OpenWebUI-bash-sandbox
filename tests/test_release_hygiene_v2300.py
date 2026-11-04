"""The release hygiene of v2.30.0: three leftovers, pinned.

Three findings came out of the review of v2.29.0, and all three were
about the same kind of thing -- a number or a rule that lives in
prose, in a comment, or in somebody's shell, where nothing checks it:

* **N1** -- the release notes counted the sweep and the number was
  wrong: the notes said 32 scripts and the disk held 33, because a
  script had been added after the sentence was written. Nothing
  compared the two, so the number was free to rot.
* **N2** -- three places named three different workspace ceilings. The
  template said 1g, the Makefile and the setup script said 4g, and a
  deployment that copied the template got a declared ceiling that did
  not match the filesystem it was given. The agent refuses to serve
  against a mismatch, and nothing said so where the number is written.
* **MINOR** -- the sweep runner lived in the operator's shell. The
  rules that keep a mutation sweep from eating uncommitted work were
  not in the repository, so they could not run in CI, and the runner
  was one `git checkout -- .` away from throwing work away.

Every check here has a counter-test: the matching mutation in
``tools/mutation-check-v2300.sh`` breaks the property on purpose, and
the named test has to go red. A check that cannot be made red is a
decoration, and decoration rots silently.
"""

from __future__ import annotations

import fcntl
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _scripts() -> list[pathlib.Path]:
    """The mutation scripts that are on disk."""
    return sorted((ROOT / "tools").glob("mutation-check-*.sh"))


def _git(repo: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _section(text: str, heading: str) -> str:
    """The part of the document under one heading.

    A claim that lives in a section has to be checked in that section:
    the same words somewhere else in the file are a different claim.
    """
    parts = text.split(f"### {heading}")
    if len(parts) < 2:
        return ""
    return parts[1].split("\n### ", 1)[0]


class TestTheReleaseNotesCountTheSweep(unittest.TestCase):
    """The newest section names the sweep's size, and the size is true."""

    CLAIM = re.compile(r"the\s+sweep\s+is\s+(\d+)\s+scripts", re.IGNORECASE)

    def _newest_section(self) -> str:
        text = _read("CHANGELOG.md")
        head = re.search(r"^## \[(\d+\.\d+\.\d+)\][^\n]*$", text, re.MULTILINE)
        if not head:
            return ""
        rest = text[head.end() :]
        following = re.search(r"^## \[", rest, re.MULTILINE)
        return rest[: following.start()] if following else rest

    def test_the_newest_section_names_the_sweep_size(self) -> None:
        section = self._newest_section()
        self.assertTrue(section, "the changelog has no version section")
        claims = self.CLAIM.findall(section)
        self.assertEqual(
            len(claims),
            1,
            f"the newest section must name the size of the sweep exactly once, found {claims}",
        )

    def test_the_number_it_names_is_the_number_on_disk(self) -> None:
        section = self._newest_section()
        claims = self.CLAIM.findall(section)
        self.assertEqual(len(claims), 1, "the newest section does not name the sweep's size")
        self.assertEqual(
            int(claims[0]),
            len(_scripts()),
            f"the release notes say the sweep is {claims[0]} scripts, "
            f"and there are {len(_scripts())} on disk",
        )


class TestTheCeilingIsOneProfile(unittest.TestCase):
    """One deployment profile, one number.

    The agent compares the size of the filesystem it is looking at with
    the ceiling it was given, and refuses to serve when they disagree.
    So the number that is declared and the filesystem that is made have
    to be the same number -- and there is one profile that says what
    that number is, not three that each say their own.
    """

    def _template(self) -> str:
        match = re.search(r"(?m)^AGENT_WORKSPACE_SIZE=(\S+)$", _read(".env.example"))
        self.assertIsNotNone(match, "the template declares no workspace ceiling")
        return match.group(1)  # type: ignore[union-attr]

    def _makefile(self) -> str:
        match = re.search(r"(?m)^WORKSPACE_SIZE \?= (\S+)$", _read("Makefile"))
        self.assertIsNotNone(match, "the Makefile has no default workspace ceiling")
        return match.group(1)  # type: ignore[union-attr]

    def _setup_script(self) -> str:
        match = re.search(r"AGENT_WORKSPACE_SIZE:-([^\}\"']+)", _read("tools/setup-workspace.sh"))
        self.assertIsNotNone(match, "the setup script has no default workspace ceiling")
        return match.group(1)  # type: ignore[union-attr]

    def _documented(self) -> str:
        section = _section(_read("docs/CONFIGURATION.md"), "`AGENT_WORKSPACE_SIZE`")
        self.assertTrue(section, "the configuration reference has no AGENT_WORKSPACE_SIZE section")
        match = re.search(r"Default: `([^`]+)`", section)
        self.assertIsNotNone(match, "the configuration reference names no default ceiling")
        return match.group(1)  # type: ignore[union-attr]

    def test_the_product_profile_names_one_size(self) -> None:
        sizes = {
            "the template": self._template(),
            "the Makefile": self._makefile(),
            "the setup script": self._setup_script(),
            "the configuration reference": self._documented(),
        }
        self.assertEqual(
            len(set(sizes.values())),
            1,
            "the product profile names more than one ceiling: "
            + ", ".join(f"{where} says {size}" for where, size in sizes.items()),
        )
        self.assertEqual(
            sizes["the template"],
            "4g",
            "the product profile is 4g -- that is the ceiling the operator's "
            f"stands run with, and the template says {sizes['the template']}",
        )

    def test_the_ci_profile_names_its_own_size(self) -> None:
        ci_compose = re.findall(
            r"\$\{AGENT_WORKSPACE_SIZE:-([^\}]*)\}", _read("docker-compose.ci.yml")
        )
        workflow = _read(".github/workflows/ci.yml")
        ci_setup = re.findall(r"\$\{AGENT_WORKSPACE_SIZE:-([^\}]*)\}", workflow)
        ci_env = re.findall(r"(?m)^\s*AGENT_WORKSPACE_SIZE:\s*(\S+)\s*$", workflow)
        literals = ci_compose + ci_setup + ci_env
        self.assertTrue(literals, "the CI profile names no ceiling at all")
        self.assertEqual(
            sorted(set(literals)),
            ["1g"],
            "the CI profile is deliberately 1g -- it is a small filesystem on a "
            f"throwaway runner -- and it must say so everywhere: {literals}",
        )

    def test_the_product_compose_declares_no_ceiling(self) -> None:
        for name in ("docker-compose.yml", "docker-compose.production.yml"):
            self.assertIn(
                "AGENT_WORKSPACE_SIZE=${AGENT_WORKSPACE_SIZE:-}",
                _read(name),
                f"{name} carries a literal ceiling: the ceiling of a "
                "deployment is the operator's number, not a constant in the "
                "compose file",
            )

    def test_the_deploy_rule_is_written_down(self) -> None:
        section = _section(_read("docs/CONFIGURATION.md"), "`AGENT_WORKSPACE_SIZE`")
        self.assertRegex(
            section,
            r"(?i)the declared ceiling must equal the filesystem that was made",
            "the configuration reference does not say that the declared "
            "ceiling has to be the ceiling the setup script made -- that is "
            "the rule a deployment gets wrong",
        )


class TestTheSweepRunnerLivesInTheRepo(unittest.TestCase):
    """The sweep runner is in the repository, and it keeps its promises."""

    def _repo(self, root: pathlib.Path) -> pathlib.Path:
        """A repository with the runner in it and one committed file."""
        repo = root / "repo"
        (repo / "tools").mkdir(parents=True)
        shutil.copy2(ROOT / "tools" / "run-sweep.sh", repo / "tools" / "run-sweep.sh")
        (repo / "tracked.txt").write_text("original\n", encoding="utf-8")
        _git(repo, "init", "-q", ".")
        _git(repo, "add", "-A")
        _git(
            repo,
            "-c",
            "user.email=ci@example.invalid",
            "-c",
            "user.name=ci",
            "commit",
            "-qm",
            "base",
        )
        return repo

    def _stub(self, repo: pathlib.Path, name: str, body: str) -> None:
        (repo / "tools" / name).write_text(f"#!/usr/bin/env bash\n{body}", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(
            repo,
            "-c",
            "user.email=ci@example.invalid",
            "-c",
            "user.name=ci",
            "commit",
            "-qm",
            f"stub {name}",
        )

    def _sweep(self, repo: pathlib.Path, *patterns: str) -> subprocess.CompletedProcess[str]:
        environ = dict(os.environ)
        # The logs go outside the repository: a sweep that logs into the
        # tree it is checking is a sweep that dirties it.
        environ["OWBS_SWEEP_LOG_DIR"] = str(repo.parent / "logs")
        return subprocess.run(
            ["bash", "tools/run-sweep.sh", *patterns],
            cwd=str(repo),
            env=environ,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def test_a_clean_sweep_runs_the_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(pathlib.Path(tmp))
            self._stub(repo, "mutation-check-stub.sh", 'echo "the stub ran"\n')
            result = self._sweep(repo, "tools/mutation-check-stub.sh")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("the stub ran", result.stdout)
            self.assertEqual(
                _git(repo, "status", "--porcelain"),
                "",
                "the sweep left the tree dirty",
            )
            self.assertTrue(
                (repo.parent / "logs").is_dir(),
                "the sweep wrote no logs outside the repository",
            )

    def test_a_dirty_tree_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(pathlib.Path(tmp))
            self._stub(repo, "mutation-check-stub.sh", 'touch "ran"\n')
            (repo / "tracked.txt").write_text("uncommitted work\n", encoding="utf-8")
            result = self._sweep(repo, "tools/mutation-check-stub.sh")
            self.assertEqual(
                result.returncode,
                3,
                f"a dirty tree must be refused, not swept: {result.stdout}{result.stderr}",
            )
            self.assertFalse(
                (repo / "ran").exists(),
                "the sweep ran a script against a tree that held uncommitted work",
            )

    def test_a_leftover_mutation_fails_the_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(pathlib.Path(tmp))
            self._stub(repo, "mutation-check-stub.sh", 'echo "leaked" >> tracked.txt\n')
            result = self._sweep(repo, "tools/mutation-check-stub.sh")
            self.assertNotEqual(
                result.returncode,
                0,
                "a script that left the tree dirty must fail the sweep",
            )
            self.assertIn("left the tree dirty", result.stdout + result.stderr)

    def test_the_runner_does_not_restore_the_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(pathlib.Path(tmp))
            self._stub(repo, "mutation-check-stub.sh", 'echo "leaked" >> tracked.txt\n')
            result = self._sweep(repo, "tools/mutation-check-stub.sh")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "leaked",
                (repo / "tracked.txt").read_text(encoding="utf-8"),
                "the runner restored the tree: a leftover mutation is a "
                "finding, and restoring it here throws away work that was "
                "never committed",
            )

    def test_the_scripts_run_one_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(pathlib.Path(tmp))
            lock = repo.parent / "slow.lock"
            self._stub(
                repo,
                "mutation-check-a-slow.sh",
                f'touch "{lock}"\nsleep 1\nrm -f "{lock}"\n',
            )
            self._stub(
                repo,
                "mutation-check-b-fast.sh",
                f"for _ in $(seq 1 40); do\n"
                f'  if [ -e "{lock}" ]; then\n'
                f'    echo "the scripts ran at the same time" >&2\n'
                f"    exit 1\n"
                f"  fi\n"
                f"  sleep 0.05\n"
                f"done\n"
                f'echo "the fast stub ran alone"\n',
            )
            result = self._sweep(repo, "tools/mutation-check-*.sh")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("the fast stub ran alone", result.stdout)

    def test_a_second_runner_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(pathlib.Path(tmp))
            self._stub(repo, "mutation-check-stub.sh", 'echo "the stub ran"\n')
            lock = repo / ".git" / "owbs-sweep.lock"
            handle = open(lock, "w", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                result = self._sweep(repo, "tools/mutation-check-stub.sh")
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            self.assertEqual(
                result.returncode,
                4,
                f"a second runner in the same tree must be refused: {result.stdout}{result.stderr}",
            )
            self.assertNotIn("the stub ran", result.stdout)

    def test_the_workflow_and_the_makefile_call_the_runner(self) -> None:
        workflow = _read(".github/workflows/ci.yml")
        self.assertIn(
            "tools/run-sweep.sh",
            workflow,
            "the CI does not run the sweep through the runner in the repository",
        )
        self.assertIn("tools/mutation-check-*.sh", workflow)
        makefile = _read("Makefile")
        self.assertIn(
            "tools/run-sweep.sh",
            makefile,
            "`make mutation` does not run the sweep through the runner",
        )
        self.assertIn("tools/mutation-check-*.sh", makefile)
        self.assertIn(
            "tools/run-sweep.sh",
            _read("CONTRIBUTING.md"),
            "the contribution guide does not point at the runner",
        )
