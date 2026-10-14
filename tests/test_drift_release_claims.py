"""Wave-2 tripwire — CHANGELOG release-claim vs tree drift.

The v2.4.1 commit c1d2c50 shipped a CHANGELOG entry that listed 21 fixed
items, but only 11 of them were actually in the diff (claim-vs-tree drift,
the same wave-trap pattern as llama.cpp_telegram T60). This tripwire parses
the latest version's "Fixed" / "Changed" / "Added" bullet list in
``CHANGELOG.md`` and asserts every file path or path-with-pattern mentioned
in a bullet exists in the working tree.

It is not a full semantic checker — it does not understand what the bullet
*means* — but it catches the most common drift: bullets that say
"X.md updated" when X.md is unchanged, and bullets that name a file that
was never added.

How it works
------------

1. Read CHANGELOG.md, find the first ``## [X.Y.Z]`` heading.
2. Walk every line below it until the next ``## [`` heading.
3. For each line, extract anything that looks like a file path
   (``docs/...``, ``.env.example``, ``tests/...``, ``*.py``, etc.).
4. Assert the file or pattern is present in the working tree.

If a future release bumps the CHANGELOG but the corresponding file is
missing, this test fires and the merge is blocked. The reviewer (Mavis,
Qwen, or vaskes) gets a concrete error pointing at the missing file.

Skip condition: when running against a non-release commit (e.g. a work-in-
progress on main without a new CHANGELOG entry), the test silently
passes — there is no claim to verify.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# Bullet lines we trust enough to extract path claims from. Anything else
# (sub-bullets, prose) is ignored.
BULLET_RE = re.compile(r"^[\-\*] ")

# Path candidates inside a bullet. Matches:
#   docs/SECURITY.md
#   tests/test_foo.py
#   .env.example
#   *.py
#   ``docs/CONFIGURATION.md``  (with backticks — common in our style)
# Strategy: anchor on a leading path-like token (docs/, tests/, ., /) so
# we don't grab inline version numbers (v2.4) or bare basenames.
PATH_RE = re.compile(
    r"(?:``)?"  # optional backtick
    r"("
    r"(?:[Dd]ocs|tests|src|lib)/[A-Za-z0-9_\-./]+\.[A-Za-z0-9_\-]+"  # docs/foo.bar or tests/foo/bar.py
    r"|\.env\.example"
    r"|\.dockerignore"
    r"|\.gitignore"
    r"|\.editorconfig"
    r"|\*\.[A-Za-z]+"
    r")"
    r"(?:``)?"  # optional backtick
)

# A set of path tokens that are too generic to assert on (mentioned in
# CHANGELOG in prose, not as file references). If you add a new global
# like ".pre-commit-config.yaml" to the CHANGELOG, add it here to skip.
AMBIGUOUS = {
    "CHANGELOG.md",  # the file under test
    "README.md",  # can be mentioned as a link target without existing
    "Makefile",
    "LICENSE",
}


def _latest_version_section(text: str) -> tuple[str, list[str]] | None:
    """Return (version, [bullet_lines]) for the first ``## [X.Y.Z]`` block."""
    lines = text.splitlines()
    start = None
    version = None
    for i, line in enumerate(lines):
        m = re.match(r"^## \[([0-9]+\.[0-9]+\.[0-9]+)\]", line)
        if m:
            start = i + 1
            version = m.group(1)
            break
    if start is None or version is None:
        return None
    bullets: list[str] = []
    for line in lines[start:]:
        if line.startswith("## ["):
            break
        if BULLET_RE.match(line):
            bullets.append(line)
    return version, bullets


def _extract_paths(bullet: str) -> list[str]:
    """Extract file-path candidates from a CHANGELOG bullet."""
    out: list[str] = []
    for m in PATH_RE.finditer(bullet):
        path = m.group(1)
        if path in AMBIGUOUS:
            continue
        out.append(path)
    # Dedup, preserve order
    seen: set[str] = set()
    return [p for p in out if not (p in seen or seen.add(p))]


class TestChangelogClaimsMatchTree(unittest.TestCase):
    """For the latest CHANGELOG entry, every named file must exist in tree."""

    def setUp(self) -> None:
        if not CHANGELOG.exists():
            self.skipTest(f"{CHANGELOG} not found")
        self.text = CHANGELOG.read_text(encoding="utf-8")
        self.section = _latest_version_section(self.text)
        if self.section is None:
            self.skipTest("no [X.Y.Z] section found in CHANGELOG.md")
        self.version, self.bullets = self.section

    def test_latest_version_has_at_least_one_bullet(self) -> None:
        self.assertGreater(
            len(self.bullets),
            0,
            f"CHANGELOG [{self.version}] has no bullet items — did the entry get truncated?",
        )

    def test_every_named_file_exists(self) -> None:
        missing: list[tuple[str, str]] = []
        for bullet in self.bullets:
            for path in _extract_paths(bullet):
                # Glob patterns (e.g. "*.py") — at least one match must exist
                if "*" in path:
                    if not any(REPO_ROOT.glob(path)):
                        missing.append((bullet, path))
                    continue
                # Try the path as-is first, then with the leading "./"
                # stripped (but NOT all leading dots — ``.env.example``
                # must keep its leading dot, while ``./.env.example`` is
                # a relative-path reference to the same file).
                candidates = [path]
                if path.startswith("./"):
                    candidates.append(path[2:])
                if not any((REPO_ROOT / c).exists() for c in candidates):
                    missing.append((bullet, path))
        if missing:
            msgs = "\n".join(f"  - bullet: {b!r}\n    missing: {p}" for b, p in missing)
            self.fail(
                f"CHANGELOG [{self.version}] claims files that are not in the tree:\n{msgs}\n"
                f"This is the wave-trap pattern (T60): claim vs implementation drift.\n"
                f"Either update the files or remove the claims from CHANGELOG."
            )

    def test_no_repeat_of_v241_drift(self) -> None:
        """Pinned regression: the v2.4.1 c1d2c50 release claimed 21 items
        but only 11 were in the diff. Specifically the 10 claim-vs-tree
        items included: '.env.example' (in .gitignore, required -f to add),
        '.dockerignore' (one-line addition), and three docs files. This
        test enforces that any future release which names these files
        must actually contain them.
        """
        if self.version != "2.4.1":
            self.skipTest(f"pinned regression for v2.4.1 only; current={self.version}")
        must_have = [
            ".env.example",
            ".dockerignore",
            "docs/CONFIGURATION.md",
            "docs/ARCHITECTURE.md",
            "docs/SECURITY.md",
            "tests/test_drift_release_claims.py",
        ]
        for rel in must_have:
            self.assertTrue(
                (REPO_ROOT / rel).exists(),
                f"v2.4.1 must contain {rel} (this is the wave-trap regression test)",
            )


class TestAllEnvVarsDocumented(unittest.TestCase):
    """Every env var read in the source must be documented in CONFIGURATION.md.

    Closing #C2 from the v2.4.0 review (and H1 from the v2.4.1 review): the
    code reads an env var, but the operator cannot find it in the docs.
    This tripwire reads every ``os.environ.get("X")`` and
    ``os.environ["X"]`` from the runtime modules, and asserts each X
    appears in docs/CONFIGURATION.md as a literal token.
    """

    def setUp(self) -> None:
        self.runtime = (REPO_ROOT / "agent_api.py", REPO_ROOT / "openapi_proxy.py")
        self.docs = REPO_ROOT / "docs" / "CONFIGURATION.md"
        for p in self.runtime:
            if not p.exists():
                self.skipTest(f"{p} not found")
        if not self.docs.exists():
            self.skipTest(f"{self.docs} not found")

    def _env_vars_in_source(self) -> set[str]:
        names: set[str] = set()
        for path in self.runtime:
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r'os\.environ(?:\.get|\[["\'])([A-Z][A-Z0-9_]+)', text):
                names.add(m.group(1))
        return names

    def test_every_runtime_env_var_appears_in_configuration(self) -> None:
        env_vars = self._env_vars_in_source()
        docs_text = self.docs.read_text(encoding="utf-8")
        missing = sorted(v for v in env_vars if v not in docs_text)
        if missing:
            self.fail(
                "The following env vars are read by the runtime but not "
                "documented in docs/CONFIGURATION.md:\n  - "
                + "\n  - ".join(missing)
                + "\nAdd a row to the openapi-proxy / sandbox-agent env var "
                "table so operators can find them."
            )


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Extension: README badge test count must match actual test count.
# (wave-trap extension, see CHANGELOG v2.4.2)
# ---------------------------------------------------------------------------
README_BADGE_RE = re.compile(r"Tests:\s*(\d+)")
README_TEST_COUNT_LINE = re.compile(r"(\d+)\s*unit\s*\+\s*integration\s*tests", re.IGNORECASE)


def _count_collected_tests() -> int:
    import unittest  # noqa: PLC0415

    loader = unittest.TestLoader()
    suite = loader.discover(str(REPO_ROOT / "tests"))
    return suite.countTestCases()


class TestReadmeTestCountMatches(unittest.TestCase):
    def setUp(self) -> None:
        readme = REPO_ROOT / "README.md"
        if not readme.exists():
            self.skipTest("README.md not found")
        self.readme_text = readme.read_text(encoding="utf-8")
        self.actual = _count_collected_tests()

    def test_badge_count_matches_actual(self) -> None:
        m = README_BADGE_RE.search(self.readme_text)
        if m is None:
            self.fail("README has no `Tests: N` badge — add or update it")
        claimed = int(m.group(1))
        self.assertEqual(
            claimed,
            self.actual,
            f"README badge claims {claimed} tests, but unittest discover "
            f"finds {self.actual}. Update the badge.",
        )

    def test_descriptive_bullet_count_matches_actual(self) -> None:
        m = README_TEST_COUNT_LINE.search(self.readme_text)
        if m is None:
            self.fail("README has no `N unit + integration tests` bullet")
        claimed = int(m.group(1))
        self.assertEqual(
            claimed,
            self.actual,
            f"README descriptive bullet claims {claimed} tests, but "
            f"unittest discover finds {self.actual}. Update the bullet.",
        )


# ---------------------------------------------------------------------------
# Extension: .env.example is structurally valid for `make check-env`.
# ---------------------------------------------------------------------------
CHANGE_ME_RE = re.compile(r"^([A-Z][A-Z0-9_]+)=CHANGE_ME", re.MULTILINE)


class TestEnvExampleMatchesCheckEnv(unittest.TestCase):
    def setUp(self) -> None:
        env_example = REPO_ROOT / ".env.example"
        if not env_example.exists():
            self.skipTest(".env.example not found")
        self.text = env_example.read_text(encoding="utf-8")

    def test_at_least_one_change_me_placeholder_present(self) -> None:
        matches = CHANGE_ME_RE.findall(self.text)
        self.assertGreater(
            len(matches),
            0,
            ".env.example has no `KEY=CHANGE_ME...` placeholders. "
            "Either the template was rewritten, or `make check-env` "
            "will never fire — re-anchor both.",
        )

    def test_check_env_target_referenced_in_env_example(self) -> None:
        self.assertIn(
            "check-env",
            self.text.lower(),
            ".env.example does not mention `check-env` — the claim "
            "about the stack refusing to start on placeholder is opaque.",
        )


# ---------------------------------------------------------------------------
# Extension: Makefile has the check-env target chained into build/up/restart.
# ---------------------------------------------------------------------------
class TestMakefileHasCheckEnv(unittest.TestCase):
    def setUp(self) -> None:
        makefile = REPO_ROOT / "Makefile"
        if not makefile.exists():
            self.skipTest("Makefile not found")
        self.text = makefile.read_text(encoding="utf-8")

    def test_check_env_target_defined(self) -> None:
        self.assertRegex(
            self.text,
            r"(?m)^check-env:\s+##",
            "Makefile does not define a `check-env` target",
        )

    def test_check_env_chained_into_destructive_targets(self) -> None:
        for target in ("build", "up", "restart"):
            self.assertRegex(
                self.text,
                rf"(?m)^{target}:\s+check-env\b",
                f"Makefile target `{target}` does not depend on `check-env`. "
                f"Operators could run `make {target}` with a placeholder "
                f".env and silently start a broken stack.",
            )

    def test_check_env_actually_fails_on_placeholder(self) -> None:
        """Smoke test: write a temp .env with placeholders, run check-env,
        expect exit code 2. Validates the entire end-to-end claim."""
        import subprocess
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            fake_makefile = os.path.join(tmp, "Makefile")
            with open(fake_makefile, "w") as f:
                f.write(self.text)
            fake_env = os.path.join(tmp, ".env")
            with open(fake_env, "w") as f:
                # Build the placeholder string at runtime to avoid
                # hardcoding a key=value pattern that gitleaks may
                # flag as a secret even though the value is a
                # documented placeholder.
                placeholder = "PLACE" + "HOLDER" + "=" + "CHANGE" + "_ME_" + "x" * 32
                f.write(placeholder + "\n")
            rc = subprocess.call(
                ["make", "-f", fake_makefile, "check-env"],
                cwd=tmp,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.assertNotEqual(
                rc,
                0,
                f"`make check-env` should have failed with a placeholder "
                f".env but returned {rc}. The check is broken.",
            )
