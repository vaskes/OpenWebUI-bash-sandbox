"""The release claims of v2.14.4, pinned by tests.

A release note is a claim about the code. A claim that is not pinned by
a test is a claim that rots: it stays true until somebody edits the
code and forgets the sentence. These tests are the pin.

What v2.14.4 claims:

* the two services answer with this version;
* the changelog has the section, and the changelog is newest-first;
* a command that would stick the shell is refused *before* it is typed;
* the refusal is precise -- bash's own noise is not a trap;
* a shell that waits for the rest of a command is interrupted, and a
  session that is busy running something is not;
* the busy refusal names the way out;
* the exec limit is the deployment's: the shipped default is what the
  operator decided, the ceiling is the agent's cap, and the paths that
  start the stack name the deployment's env file.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _prose(name: str) -> str:
    """The document with its line breaks collapsed.

    A claim about prose must not depend on where the line happened to
    wrap: the sentence is the claim, not the line it sits on.
    """
    return " ".join(_source(name).split())


class TestTheVersionIsTheRelease(unittest.TestCase):
    def test_the_services_answer_with_this_version(self) -> None:
        import agent_core  # noqa: PLC0415
        import proxy_core  # noqa: PLC0415

        self.assertEqual(agent_core.__version__, proxy_core.__version__)

    def test_the_changelog_has_the_section(self) -> None:
        self.assertIn("## [2.14.4]", _source("CHANGELOG.md"))

    def test_the_changelog_is_newest_first(self) -> None:
        headings = re.findall(r"^## \[(\d+)\.(\d+)\.(\d+)\]", _source("CHANGELOG.md"), re.MULTILINE)
        self.assertTrue(headings, "the changelog has no version sections")
        versions = [(int(a), int(b), int(c)) for a, b, c in headings]
        self.assertEqual(versions, sorted(versions, reverse=True))


class TestTheStuckCommandIsTheClaim(unittest.TestCase):
    def test_the_command_is_refused_before_it_is_typed(self) -> None:
        source = _source("agent_api.py")
        block = source.split("async def exec_cmd(", 1)[1].split("\n@app", 1)[0]
        self.assertIn("command_would_stick", block)
        # The refusal comes before the session is created and before
        # anything is sent to the shell: a command that is typed and only
        # then recognised as broken has already poisoned the session.
        self.assertLess(
            block.index("command_would_stick"),
            block.index("marker = f"),
            "the command is typed before it is checked",
        )

    def test_the_refusal_says_the_command_did_not_run(self) -> None:
        source = " ".join(_source("agent_api.py").split())
        self.assertIn("the command was not run", source)

    def test_the_refusal_is_precise(self) -> None:
        """Bash's own noise must not be read as a trap.

        With a working directory that was removed underneath the process
        `bash -n` says "shell-init: error retrieving current directory"
        on stderr.  The first version of the gate read any stderr as a
        trap and refused a valid `git commit -m 'Press Enter to
        continue'`.
        """
        source = _source("agent_core.py")
        self.assertIn('cwd="/"', source)
        self.assertIn("here-document", source)

    def test_the_parse_is_a_hardening_not_a_wall(self) -> None:
        source = _source("agent_core.py")
        self.assertIn("except (OSError, ValueError, subprocess.SubprocessError):", source)
        self.assertIn("return None", source)


class TestTheStuckShellIsTheClaim(unittest.TestCase):
    def test_a_stuck_shell_is_interrupted(self) -> None:
        source = _source("agent_api.py")
        self.assertIn('_tmux("send-keys", "-t", name, "C-c")', source)

    def test_a_busy_session_is_not_interrupted(self) -> None:
        """The interrupt is only safe when the shell is *waiting*.

        The check comes first: the shell must be the foreground process
        and the pane must end with the shell's continuation prompt --
        the marker the protocol installs, not "a line that looks like a
        prompt" (a program's output satisfies that, and a working
        command was interrupted because of it).
        """
        source = _source("agent_api.py")
        block = source.split("async def _reset_a_stuck_shell", 1)[1].split("\nasync def ", 1)[0]
        self.assertIn("_pane_is_waiting_for_input", block)
        self.assertLess(
            block.index("_pane_is_waiting_for_input"),
            block.index('"send-keys"'),
            "the shell is interrupted before it is checked",
        )

    def test_the_waiting_state_is_observable(self) -> None:
        """The protocol installs a marker nothing else can print.

        That is what makes "the shell is waiting" something one can look
        at instead of something one guesses from the shape of a line.
        """
        core = _source("agent_core.py")
        self.assertIn('CONTINUATION_PROMPT = "__AGENT_CONT__"', core)
        self.assertIn('PS2=\\"{CONTINUATION_PROMPT} \\"', core)

    def test_the_answer_says_the_shell_was_reset(self) -> None:
        source = " ".join(_source("agent_api.py").split())
        self.assertIn("has been reset", source)

    def test_the_busy_refusal_names_the_way_out(self) -> None:
        source = " ".join(_source("agent_api.py").split())
        self.assertIn("use a different session_id", source)


class TestTheLimitIsTheClaim(unittest.TestCase):
    def test_the_shipped_default_is_the_operators(self) -> None:
        from proxy_core import EXEC_TIMEOUT_SECONDS  # noqa: PLC0415

        self.assertEqual(EXEC_TIMEOUT_SECONDS, 60.0)

    def test_the_ceiling_is_the_agents_cap(self) -> None:
        from agent_api import MAX_TIMEOUT  # noqa: PLC0415
        from proxy_core import EXEC_TIMEOUT_MAX_SECONDS  # noqa: PLC0415

        self.assertEqual(MAX_TIMEOUT, EXEC_TIMEOUT_MAX_SECONDS)

    def test_the_unit_names_the_deployments_env_file(self) -> None:
        self.assertIn("COMPOSE_ENV_FILE", _source("agent-stack.service.example"))

    def test_the_documentation_names_the_trap(self) -> None:
        text = _prose("docs/CONFIGURATION.md")
        self.assertIn("the session is busy", text)
        self.assertIn("env file", text)


class TestTheTripwiresAreTheClaim(unittest.TestCase):
    def test_the_wave_has_its_own_tests(self) -> None:
        self.assertTrue((ROOT / "tests/test_stuck_session_v2144.py").exists())

    def test_the_wave_has_its_own_mutation_check(self) -> None:
        self.assertTrue((ROOT / "tools/mutation-check-v2144.sh").exists())

    def test_the_mutation_check_watches_the_code_it_claims(self) -> None:
        source = _source("tools/mutation-check-v2144.sh")
        self.assertIn("command_would_stick", source)
        self.assertIn("_pane_is_waiting_for_input", source)
        self.assertIn("COMPOSE_ENV_FILE", source)
