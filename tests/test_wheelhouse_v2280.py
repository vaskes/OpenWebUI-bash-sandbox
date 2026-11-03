"""Tripwires for the wheelhouse: it has to carry the build backends.

Context. The builder stage downloads the agent's python packages once
into a local wheelhouse and then assembles the tree from that directory
alone — ``pip install --no-index --find-links=/opt/wheelhouse`` — so
the build needs pypi to be reachable exactly once.

That is fine while every package has a wheel for the builder's python,
and it is not fine the moment one of them is served as a source
distribution: pip then builds it, and the build runs **offline, against
the same wheelhouse** — so it has to find its build backend there.

Measured in the CI on 2026-11-03: pypi published SQLAlchemy 2.1.7 (two
hours earlier it served 2.1.6, a cp314 wheel), there is no 2.1.7 wheel
for the builder's python, and the build died with

    ERROR: Could not find a version that satisfies the requirement
    setuptools>=77.0.3 (from versions: none)

Nothing in the repository changed that day; the upstream release did.
The wheelhouse had never carried a build backend, because it had never
needed one.

These tests are the cheap half: they parse the Dockerfile we ship. The
expensive half is the CI's deploy job, which builds the image for real.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX_DOCKERFILE = ROOT / "Dockerfile.sandbox"


class TestTheWheelhouseCarriesTheBuildBackends(unittest.TestCase):
    def setUp(self) -> None:
        self.body = SANDBOX_DOCKERFILE.read_text(encoding="utf-8")

    def test_the_build_backends_are_downloaded(self) -> None:
        """A source distribution is built with setuptools, offline."""
        self.assertIn(
            "pip download --dest /opt/wheelhouse setuptools wheel",
            self.body,
            "the wheelhouse must carry the build backends: an sdist is "
            "built offline, against the wheelhouse",
        )

    def test_the_install_really_is_offline(self) -> None:
        """The property above only matters while the install is offline.

        If the install could reach pypi, the backend would come from
        there and the wheelhouse would not need it. This pins the
        assumption the other test rests on.
        """
        self.assertIn(
            "--no-index --find-links=/opt/wheelhouse",
            self.body,
            "the tree is assembled from the wheelhouse alone",
        )

    def test_the_measured_failure_is_recorded(self) -> None:
        """A rule without the measurement behind it is a superstition."""
        self.assertIn(
            "SQLAlchemy 2.1.7",
            self.body,
            "the Dockerfile must name the measured failure the backends are there for",
        )
        self.assertIn(
            "setuptools>=77.0.3",
            self.body,
            "the Dockerfile must name the requirement that could not be found",
        )


if __name__ == "__main__":
    unittest.main()
