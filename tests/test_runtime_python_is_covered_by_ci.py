"""The python version we SHIP must be one the suite is RUN against.

Context (2026-10-25). The base images moved from ``ubuntu:24.04`` +
``python:3.11-slim`` to ``ubuntu:26.04`` + ``python:3.14-slim``. Both
images now carry python 3.14 — the agent's ``python3`` comes from the
Ubuntu release's apt package (3.14.4), the proxy's from the slim image.

That creates a gap the suite cannot see on its own: the CI matrix
decides which python versions the *source* is exercised on, while the
base image decides which python version actually *ships*. Nothing
connects the two, so a base image bump can silently move the runtime to
a version the suite never runs on. The result is exactly the class of
defect this repository keeps paying for — the artifact and the check
drift apart, and the drift is only visible in production.

So this file connects them: the python version each image ships must be
in the CI matrix. Bumping a base image without extending the matrix
fails here, in the same commit that did the bump.

The Ubuntu -> python mapping below is measured, not remembered: the
values come from running the image and asking it (``apt-get install
python3 && python3 -V``), not from a release note. An unknown Ubuntu
tag fails the test instead of being skipped — a base image nobody
checked is precisely what this tripwire exists to catch.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX_DOCKERFILE = ROOT / "Dockerfile.sandbox"
PROXY_DOCKERFILE = ROOT / "Dockerfile.openapi-proxy"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# Ubuntu release -> the python3 its apt package provides.
#
#   24.04 -> 3.12   (the previous base, v2.5.0 .. v2.8.0)
#   26.04 -> 3.14.4 (measured 2026-10-25 in the image itself)
UBUNTU_PYTHON = {
    "24.04": "3.12",
    "26.04": "3.14",
}


def _runtime_base_image(dockerfile: Path) -> str:
    """The base image of the *runtime* stage (the FROM without ``AS``)."""
    body = dockerfile.read_text(encoding="utf-8")
    runtime: list[str] = []
    for line in body.splitlines():
        match = re.match(r"^FROM\s+(\S+)\s*(AS\s+\S+)?\s*$", line.strip(), re.IGNORECASE)
        if match and not match.group(2):
            runtime.append(match.group(1))
    if not runtime:
        raise AssertionError(f"{dockerfile.name} has no runtime stage")
    if len(runtime) != 1:
        raise AssertionError(f"{dockerfile.name} has {len(runtime)} runtime stages: {runtime}")
    return runtime[0]


def _ci_matrix() -> list[str]:
    body = CI_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"(?m)^\s*python-version:\s*\[(.*?)\]\s*$", body)
    if not match:
        raise AssertionError("the CI workflow has no python-version matrix")
    return re.findall(r'"([^"]+)"', match.group(1))


class TestShippedPythonIsTested(unittest.TestCase):
    def test_the_agent_base_image_is_a_known_ubuntu_release(self) -> None:
        base = _runtime_base_image(SANDBOX_DOCKERFILE)
        self.assertTrue(
            base.startswith("ubuntu:"),
            f"the agent image must build on ubuntu, got {base!r}",
        )
        tag = base.split(":", 1)[1]
        self.assertIn(
            tag,
            UBUNTU_PYTHON,
            f"ubuntu:{tag} is not in UBUNTU_PYTHON. Measure it "
            f"(docker run --rm ubuntu:{tag} apt-get install -y python3 && python3 -V) "
            f"and add the answer, or the runtime ships an untested python.",
        )

    def test_the_proxy_base_image_names_its_python(self) -> None:
        base = _runtime_base_image(PROXY_DOCKERFILE)
        match = re.match(r"^python:(\d+\.\d+)-slim$", base)
        self.assertIsNotNone(
            match,
            f"the proxy image must pin a python:<version>-slim base, got {base!r}",
        )

    def test_the_ci_matrix_is_not_empty(self) -> None:
        """A check over an empty collection proves nothing."""
        versions = _ci_matrix()
        self.assertTrue(versions, "the CI python matrix is empty")
        self.assertGreaterEqual(
            len(versions),
            2,
            f"the CI python matrix has {len(versions)} entries: {versions}",
        )

    def test_the_shipped_python_versions_are_in_the_ci_matrix(self) -> None:
        versions = _ci_matrix()

        agent_base = _runtime_base_image(SANDBOX_DOCKERFILE)
        agent_tag = agent_base.split(":", 1)[1]
        agent_python = UBUNTU_PYTHON[agent_tag]

        proxy_base = _runtime_base_image(PROXY_DOCKERFILE)
        proxy_match = re.match(r"^python:(\d+\.\d+)-slim$", proxy_base)
        assert proxy_match is not None  # guarded by the test above
        proxy_python = proxy_match.group(1)

        shipped = (("agent", agent_python, agent_base), ("proxy", proxy_python, proxy_base))
        for service, version, base in shipped:
            self.assertIn(
                version,
                versions,
                f"the {service} image ships python {version} (base image {base}), "
                f"but the CI matrix is {versions}. The runtime would ship untested.",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
