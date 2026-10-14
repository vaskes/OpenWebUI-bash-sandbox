"""Tripwire for CodeQL alert #22 (workflow permissions).

The v2.4.4 release triggered CodeQL `actions/missing-workflow-permissions`
because the CI workflow had no explicit `permissions:` block, so the
GITHUB_TOKEN inherited the repo's default (read-write for repos
created before Feb 2023 — which this one is).

Fix: add `permissions: contents: read` at the workflow level. All
current jobs (lint, test, sast, secrets) only need read access.

This test asserts the fix is in place so a future contributor can't
remove the `permissions:` block without the test firing.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class TestWorkflowPermissions(unittest.TestCase):
    """Wave 3 (post-v2.4.4 follow-up): pin the CodeQL alert #22 fix."""

    def test_workflow_has_explicit_permissions_block(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        # The fix is a top-level `permissions: contents: read` block.
        # The regex matches a `permissions:` at column 0 (no indent,
        # so it's at the workflow level, not inside a job).
        match = re.search(r"^permissions:\s*\n\s+contents:\s*read", text, re.MULTILINE)
        self.assertIsNotNone(
            match,
            "ci.yml is missing the top-level `permissions: contents: read` "
            "block. Without it, the GITHUB_TOKEN inherits the repo's "
            "default permissions (read-write for repos created before "
            "Feb 2023), which violates least-privilege. CodeQL alert "
            "`actions/missing-workflow-permissions` will fire.",
        )

    def test_workflow_does_not_grant_unnecessary_write(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        # If any job needs write access, it should override explicitly
        # at the job level. The current workflow has no such need.
        # Check there's no `contents: write` (other than in comments).
        # Strip comments and check.
        no_comments = re.sub(r"#[^\n]*", "", text)
        self.assertNotIn(
            "contents: write",
            no_comments,
            "ci.yml has `contents: write` somewhere — this is the "
            "default we're trying to avoid. If a job genuinely needs "
            "write, scope it to that one job's `permissions:` block.",
        )


if __name__ == "__main__":
    unittest.main()
