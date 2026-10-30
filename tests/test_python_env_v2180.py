"""The sandbox image can install python packages, and the model is told so.

The model asked for pip. It was right to ask: the image had **no pip at
runtime** -- ``python3-pip`` was installed in the *builder* stage only,
to build the agent's own dependencies, and the runtime stage never got
it. So every attempt to install a package cost a round trip through
apt-get and sudo, and the command's timeout paid for it.

Two things are pinned here, and they are two different things:

* the **image** has what an installation needs -- pip, ``venv`` (so
  ``ensurepip`` is there), a wheelhouse and a preinstalled tree;
* the **model** knows it. A capability the model has to discover by
  failing is a capability that costs a timeout to discover.

What is pinned here:

* the runtime stage installs ``python3-pip`` and ``python3-venv`` -- as
  commands, not as words in a comment;
* the preinstalled tree and the wheelhouse are copied into the image,
  and pip is pointed at the wheelhouse;
* the tree is built from the wheelhouse **offline** (``--no-index``),
  so the image does not depend on pypi being reachable twice;
* the tree lives in a directory of its own and is **not** on the
  service's PYTHONPATH: it carries its own pydantic and httpx, and the
  service's pinned dependencies must not be shadowed by them;
* the model's shell **does** get it -- that is the whole point;
* the tool description names every package the image ships, so the
  list in the Dockerfile cannot drift away from what the model is told.

The list of packages lives in one place -- the Dockerfile's build
argument. The description repeats it in prose, and the checks below
compare the copies instead of trusting either of them.
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Where the preinstalled tree lives. The Dockerfile and the code have
#: to agree; the check below compares them.
PREINSTALLED_PYTHON_PATH = "/opt/pylibs"

#: The wheelhouse that came with the image.
WHEELHOUSE_PATH = "/opt/wheelhouse"


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _code_lines(text: str) -> list[str]:
    """The lines that are code, not commentary.

    A check that looks for a word finds it in the comment above the
    command as easily as in the command itself, and a check that a
    comment can satisfy is a check that a mutation cannot turn red.
    """
    return [raw for raw in text.splitlines() if raw.strip() and not raw.lstrip().startswith("#")]


def _logical_lines(text: str) -> list[str]:
    """The commands, with their backslash continuations folded in.

    A Dockerfile command is one line with the continuations joined: the
    package names sit on their own lines but they belong to the apt
    command above them. A check that reads one physical line asks the
    wrong question -- the answer is on the line the command started on.
    """
    out: list[str] = []
    current = ""
    for raw in _code_lines(text):
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        out.append(current + stripped)
        current = ""
    if current:
        out.append(current)
    return out


def _stages(text: str) -> list[tuple[str, str]]:
    """Every build stage of the Dockerfile, as (name, body).

    ``FROM ubuntu:26.04 AS builder`` opens a stage with a name; a bare
    ``FROM ubuntu:26.04`` opens one without. The runtime stage of this
    image is the second kind, so a parser that only knows the first kind
    sees no runtime stage at all -- and every check that asks "what does
    the runtime stage install?" answers about nothing.
    """
    out: list[tuple[str, str]] = []
    name: str | None = None
    body: list[str] = []
    for raw in text.splitlines():
        match = re.match(r"^FROM\s+\S+(?:\s+AS\s+(\S+))?\s*$", raw)
        if match:
            if name is not None:
                out.append((name, "\n".join(body)))
            name = match.group(1) or f"stage{len(out)}"
            body = []
            continue
        if name is not None:
            body.append(raw)
    if name is not None:
        out.append((name, "\n".join(body)))
    return out


def _runtime_stage(text: str) -> str:
    """The body of the stage that is not the builder."""
    stages = _stages(text)
    assert any(name == "builder" for name, _ in stages), "the Dockerfile has no builder stage"
    runtime = [body for name, body in stages if name != "builder"]
    assert len(runtime) == 1, f"expected one runtime stage, got {len(runtime)}"
    return runtime[0]


def _builder_stage(text: str) -> str:
    for name, body in _stages(text):
        if name == "builder":
            return body
    raise AssertionError("the Dockerfile has no builder stage")


def _dockerfile() -> str:
    return _read("Dockerfile.sandbox")


def _installed_packages(text: str) -> list[str]:
    """The packages the image ships, read out of the Dockerfile.

    The build argument is the canonical list. It is read from the
    tree rather than written here: a literal in this file would be a
    second copy of a fact, and the second copy is the one that rots.
    """
    match = re.search(
        r"^ARG AGENT_PYTHON_PACKAGES=\"([^\"]*)\"$",
        text,
        re.MULTILINE,
    )
    assert match is not None, "the Dockerfile has no AGENT_PYTHON_PACKAGES build argument"
    packages = match.group(1).split()
    assert packages, "the build argument names no package"
    return packages


def _tool_description(function_name: str) -> str:
    """The description the MCP server gives the model for one tool.

    Read out of the AST, not out of the text: the description is a
    concatenated string literal, and a check that greps the file finds
    the words in the module docstring and in the comments too -- which
    is a check a mutation cannot turn red.
    """
    tree = ast.parse(_read("mcp_server.py"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != function_name:
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            for keyword in decorator.keywords:
                if keyword.arg == "description":
                    value = ast.literal_eval(keyword.value)
                    assert isinstance(value, str)
                    return " ".join(value.split())
    raise AssertionError(f"the tool {function_name} has no description")


class TestTheRuntimeImageHasAPackageManager(unittest.TestCase):
    def test_the_runtime_stage_installs_pip(self) -> None:
        commands = _logical_lines(_runtime_stage(_dockerfile()))
        apt = [line for line in commands if "apt-get install" in line]
        self.assertTrue(apt, "the runtime stage has no apt-get install command")
        self.assertIn(
            "python3-pip",
            apt[0].split(),
            "the runtime stage does not install python3-pip -- without pip in "
            "the image, installing a package costs a trip through apt and "
            "sudo, which is the timeout the model was complaining about",
        )

    def test_the_runtime_stage_installs_venv(self) -> None:
        commands = _logical_lines(_runtime_stage(_dockerfile()))
        apt = [line for line in commands if "apt-get install" in line]
        self.assertTrue(apt, "the runtime stage has no apt-get install command")
        self.assertIn(
            "python3-venv",
            apt[0].split(),
            "the runtime stage does not install python3-venv -- without it "
            "there is no ensurepip, so a virtual environment cannot be made "
            "without root",
        )

    def test_the_packages_are_installed_by_the_apt_command(self) -> None:
        """The packages have to be installed, not merely named.

        A check that looks for the word is satisfied by the comment
        above the command. This one reads the command.
        """
        commands = _logical_lines(_runtime_stage(_dockerfile()))
        apt = [line for line in commands if "apt-get install" in line]
        self.assertTrue(apt, "the runtime stage has no apt-get install command")
        self.assertIn("python3-pip", apt[0].split())
        self.assertIn("python3-venv", apt[0].split())


class TestTheImageCarriesTheTreeAndItsWheels(unittest.TestCase):
    def test_the_tree_is_copied_into_the_image(self) -> None:
        code = "\n".join(_code_lines(_runtime_stage(_dockerfile())))
        self.assertRegex(
            code,
            rf"(?m)^COPY --from=builder {re.escape(PREINSTALLED_PYTHON_PATH)} "
            rf"{re.escape(PREINSTALLED_PYTHON_PATH)}\s*$",
            "the preinstalled tree is not copied into the runtime image -- "
            "without it the packages the model keeps asking for are not there",
        )

    def test_the_wheelhouse_is_copied_into_the_image(self) -> None:
        code = "\n".join(_code_lines(_runtime_stage(_dockerfile())))
        self.assertRegex(
            code,
            rf"(?m)^COPY --from=builder {re.escape(WHEELHOUSE_PATH)} "
            rf"{re.escape(WHEELHOUSE_PATH)}\s*$",
            "the wheelhouse is not copied into the runtime image -- without it "
            "installing a preinstalled package is a download instead of a "
            "local install",
        )

    def test_pip_is_pointed_at_the_wheelhouse(self) -> None:
        code = "\n".join(_code_lines(_runtime_stage(_dockerfile())))
        self.assertRegex(
            code,
            rf"PIP_FIND_LINKS=\"{re.escape(WHEELHOUSE_PATH)}\"",
            "pip is not pointed at the wheelhouse -- the local wheels would "
            "never be found, and every install would go to the network",
        )

    def test_the_tree_is_built_from_the_wheelhouse_offline(self) -> None:
        commands = _logical_lines(_builder_stage(_dockerfile()))
        installs = [line for line in commands if "pip install" in line]
        self.assertTrue(installs, "the builder stage installs nothing")
        offline = [
            line
            for line in installs
            if "--no-index" in line and f"--find-links={WHEELHOUSE_PATH}" in line
        ]
        self.assertTrue(
            offline,
            "the tree is not installed from the local wheels -- a build that "
            "needs pypi twice is a build that fails when pypi is unreachable",
        )

    def test_the_wheels_are_fetched_once(self) -> None:
        commands = _logical_lines(_builder_stage(_dockerfile()))
        downloads = [line for line in commands if "pip download" in line]
        self.assertTrue(downloads, "the wheels are not downloaded into the wheelhouse")
        self.assertRegex(
            downloads[0],
            rf"--dest {re.escape(WHEELHOUSE_PATH)}(?![\w-])",
            "the wheels are downloaded into another directory",
        )

    def test_the_tree_is_installed_with_target(self) -> None:
        commands = _logical_lines(_builder_stage(_dockerfile()))
        installs = [line for line in commands if "pip install" in line]
        self.assertTrue(installs, "the builder stage installs nothing")
        targeted = [
            line
            for line in installs
            if re.search(
                rf"--target={re.escape(PREINSTALLED_PYTHON_PATH)}(?![\w-])",
                line,
            )
        ]
        self.assertTrue(
            targeted,
            "the tree is not installed with --target -- the interpreter is "
            "externally managed (PEP 668), and the tree has to stay out of "
            "the service's import path",
        )


class TestTheServiceDoesNotSeeThePreinstalledTree(unittest.TestCase):
    """The tree carries its own pydantic and httpx.

    The service runs with pinned dependencies. If the preinstalled tree
    were on the service's PYTHONPATH, those pins would be shadowed by
    whatever the tree happened to ship -- and the service would break
    for a reason that has nothing to do with the model.
    """

    def test_the_image_pythonpath_does_not_carry_the_tree(self) -> None:
        code = "\n".join(_code_lines(_runtime_stage(_dockerfile())))
        match = re.search(r"PYTHONPATH=\"([^\"]*)\"", code)
        assert match is not None, "the runtime stage sets no PYTHONPATH"
        self.assertNotIn(
            PREINSTALLED_PYTHON_PATH,
            match.group(1),
            "the service's PYTHONPATH carries the preinstalled tree -- its "
            "pydantic and httpx would shadow the service's pinned ones",
        )


class TestTheModelShellGetsTheTree(unittest.TestCase):
    def test_the_tmux_environment_carries_the_tree(self) -> None:
        import agent_api  # noqa: PLC0415

        env = agent_api._tmux_environment()
        self.assertIn(
            PREINSTALLED_PYTHON_PATH,
            env.get("PYTHONPATH", "").split(":"),
            "the model's shell does not get the preinstalled tree -- the "
            "packages are in the image, and the shell cannot see them",
        )

    def test_the_code_and_the_image_agree_on_the_path(self) -> None:
        import agent_config  # noqa: PLC0415

        from_code = agent_config.PREINSTALLED_PYTHON_PATH
        self.assertEqual(
            from_code,
            PREINSTALLED_PYTHON_PATH,
            "the code and the image disagree about where the preinstalled tree lives",
        )
        commands = _logical_lines(_builder_stage(_dockerfile()))
        self.assertTrue(
            any(re.search(rf"--target={re.escape(from_code)}(?![\w-])", line) for line in commands),
            "the image does not build the tree where the code looks for it",
        )


class TestTheModelIsToldWhatItCanInstall(unittest.TestCase):
    def test_the_tool_description_names_every_preinstalled_package(self) -> None:
        description = _tool_description("bash")
        for package in _installed_packages(_dockerfile()):
            self.assertRegex(
                description,
                rf"(?<![\w-]){re.escape(package)}(?![\w-])",
                f"the tool description does not mention {package} -- the "
                "model is told what is there, or it keeps asking",
            )

    def test_the_tool_description_says_how_to_add_a_package(self) -> None:
        description = _tool_description("bash")
        self.assertIn("pip", description)
        self.assertIn(
            "python3 -m venv",
            description,
            "the tool description does not say how to install a package -- a "
            "plain pip install is refused by PEP 668, and the model would "
            "spend its timeout finding that out",
        )

    def test_the_tool_description_names_the_way_out_of_the_path_order(self) -> None:
        # The tree is on PYTHONPATH, and PYTHONPATH comes before a
        # virtual environment's own site-packages. A version the model
        # installs into its own environment therefore does not override
        # the preinstalled one: pip says it installed, and the
        # preinstalled version is what runs. The model is the only one
        # who can hit that, so it is the one who has to be told.
        description = _tool_description("bash")
        self.assertIn(
            "env -u PYTHONPATH",
            description,
            "the tool description does not name the way out of the path "
            "order: the preinstalled tree wins over what the model "
            "installs, and pip reports the install as successful",
        )

    def test_the_documents_say_the_same_thing(self) -> None:
        configuration = _read("docs/CONFIGURATION.md")
        self.assertIn(
            "env -u PYTHONPATH",
            configuration,
            "the documents do not say how to get a version of a "
            "preinstalled package to win -- the rule the model is told "
            "about has to be written down for the humans too",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
