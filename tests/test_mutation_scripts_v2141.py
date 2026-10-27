"""Every mutation in every mutation script still applies to the code.

A mutation script is a tripwire's proof: it breaks a property on purpose
and expects the named test to go red. If the pattern it breaks no longer
exists in the code, the mutation does not apply — the script prints
"SKIP" and the property is unpinned. The script counts a skip as a
failure, but nobody reads the output: the scripts are run by hand, and
two of them had gone stale that way (a file door that moved in v2.13.2,
a limits table that was reworded in v2.13.0), so the properties they
guard were quietly unpinned.

This test reads the scripts and checks that every pattern they break is
still in the code they break. A stale pattern is a red test from now on.

The parser reads the shell the way a shell reads it — single quotes,
double quotes, and the ``'"'"'`` dance for an apostrophe inside a
single-quoted word — because a pattern is a *word* in a shell script,
not a line of text. And the number of parsed mutations is compared with
the number of calls: a parser that quietly extracts nothing must not
look like a file whose patterns are all fine.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOOLS = ROOT / "tools"

#: A mutation call starts a line: ``mutate <file> <old> <new> …``.
#: The function definition (``mutate() {``) and the python variant
#: (``mutate_py``) are not calls.
CALL_RE = re.compile(r"^mutate\s+(?=\S)", re.MULTILINE)


def _scripts() -> list[Path]:
    return sorted(TOOLS.glob("mutation-check-*.sh"))


def _words(text: str, pos: int) -> tuple[list[str], int]:
    """Read shell words from ``pos``; return them and the new position."""
    words: list[str] = []
    length = len(text)
    while pos < length:
        while pos < length and text[pos] in " \t\\\n":
            pos += 1
        if pos >= length:
            break
        char = text[pos]
        if char == "'":
            pos += 1
            buf: list[str] = []
            while pos < length:
                if text.startswith("'\"'\"'", pos):
                    # The escape is five characters: a quote, a double
                    # quote, a quote, a double quote, a quote.
                    buf.append("'")
                    pos += 5
                    continue
                if text[pos] == "'":
                    pos += 1
                    break
                buf.append(text[pos])
                pos += 1
            words.append("".join(buf))
            continue
        if char == '"':
            pos += 1
            buf = []
            while pos < length and text[pos] != '"':
                if text[pos] == "\\" and pos + 1 < length:
                    pos += 1
                buf.append(text[pos])
                pos += 1
            pos += 1
            words.append("".join(buf))
            continue
        buf = []
        while pos < length and text[pos] not in " \t\\\n":
            buf.append(text[pos])
            pos += 1
        words.append("".join(buf))
    return words, pos


def _mutations(text: str) -> list[tuple[str, str]]:
    """The (file, old) pairs of every mutation call in the script."""
    found: list[tuple[str, str]] = []
    for match in CALL_RE.finditer(text):
        words, _ = _words(text, match.start() + len("mutate"))
        if len(words) < 3:
            continue
        found.append((words[0], words[1]))
    return found


class TestTheMutationScriptsStillApply(unittest.TestCase):
    def test_there_are_mutation_scripts(self) -> None:
        self.assertTrue(_scripts(), "no mutation scripts in tools/")

    def test_every_mutation_pattern_is_in_the_code(self) -> None:
        stale: list[str] = []
        for script in _scripts():
            text = script.read_text(encoding="utf-8")
            for name, old in _mutations(text):
                target = ROOT / name
                if not target.exists():
                    stale.append(f"{script.name}: {name} does not exist")
                    continue
                if old not in target.read_text(encoding="utf-8"):
                    stale.append(f"{script.name}: {old!r} is not in {name}")
        self.assertEqual(stale, [], "stale mutation patterns:\n" + "\n".join(stale))

    def test_the_parser_finds_every_mutation(self) -> None:
        # A parser that quietly extracts nothing must not look like a
        # file whose patterns are all fine.
        for script in _scripts():
            with self.subTest(script=script.name):
                text = script.read_text(encoding="utf-8")
                calls = len(CALL_RE.findall(text))
                found = len(_mutations(text))
                self.assertEqual(
                    found,
                    calls,
                    f"{script.name}: {calls} mutations, {found} parsed — "
                    "the shape of a mutation call changed",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
