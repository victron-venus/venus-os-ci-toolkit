"""Execute the real approval workflow with a scripted, offline GitHub API."""

import copy
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github/workflows/auto-approve-reusable.yml"
)
REPOSITORY = "example/project"
ENDPOINT = f"repos/{REPOSITORY}/pulls/17"
HEAD = "a" * 40


# API fixtures deliberately retain separate initial/current snapshots and call history.
# pylint: disable-next=too-many-instance-attributes
class AutoApproveContract(unittest.TestCase):
    """Verify review decisions and exact API mutations, never submit live reviews."""

    def setUp(self):
        self.workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        self.job = self.workflow["jobs"]["auto-approve"]
        self.code = next(step["run"] for step in self.job["steps"] if "run" in step)
        self.environment = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "PR_NUMBER": "17",
            "TRUSTED_AUTHORS": '["4alvit", "californiantiramisu"]',
            "BOT_PAT": "test-bot-token",
            "GITHUB_ACTOR": "untrusted-event-sender",
        }
        self.pr = {
            "state": "open",
            "draft": False,
            "user": {"login": "californiantiramisu"},
            "labels": [{"name": "automerge"}],
            "head": {"sha": HEAD},
            "base": {"ref": "stable", "repo": {"full_name": REPOSITORY}},
        }
        self.current = copy.deepcopy(self.pr)
        self.reviewer = "independent-reviewer"
        self.pages = [[]]
        self.calls = []
        self.read_count = 0

    def review(self, state="APPROVED", head=HEAD, user=None):
        """Create API review metadata, independently of the workflow logic."""
        return {
            "user": {"login": user or self.reviewer},
            "state": state,
            "commit_id": head,
        }

    def api_process(self, command, **kwargs):
        """Reject unknown commands and return controlled JSON for known endpoints."""
        self.assertEqual(command[:2], ["gh", "api"])
        self.assertTrue(kwargs["check"])
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.calls.append((command, kwargs))
        path = command[2]
        if path == f"repos/{REPOSITORY}":
            self.assertEqual(command[3:], [])
            result = {"default_branch": "stable"}
        elif path == ENDPOINT:
            self.assertEqual(command[3:], [])
            self.read_count += 1
            result = self.pr if self.read_count == 1 else self.current
        elif path == "user":
            self.assertEqual(command[3:], [])
            result = {"login": self.reviewer}
        elif path == f"{ENDPOINT}/reviews?per_page=100":
            self.assertEqual(command[3:], ["--paginate", "--slurp"])
            result = self.pages
        elif path == f"{ENDPOINT}/reviews":
            self.assertEqual(command[3:], ["--method", "POST", "--input", "-"])
            self.assertEqual(
                json.loads(kwargs["input"]),
                {
                    "event": "APPROVE",
                    "commit_id": HEAD,
                },
            )
            result = {"id": 123}
        else:
            raise AssertionError(f"Unexpected API request: {command}")
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

    def execute(self):
        """Run the extracted workflow entrypoint; only the network boundary is mocked."""
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch("subprocess.run", side_effect=self.api_process),
        ):
            # Execute repository-owned workflow code with all subprocesses intercepted.
            # pylint: disable-next=exec-used
            exec(compile(self.code, str(WORKFLOW), "exec"), {"__name__": "__main__"})  # noqa: S102

    def assert_posts(self, count):
        """Assert the exact number of approval mutations sent to the API."""
        self.assertEqual(sum("POST" in command for command, _ in self.calls), count)

    def test_author_not_actor_controls_approval(self):
        """A trusted author remains eligible when a different actor sends the event."""
        self.assertNotIn("github.actor", self.job["if"])
        self.assertIn("github.event.pull_request.user.login", self.job["if"])
        self.execute()
        self.assert_posts(1)
        self.assertEqual(self.read_count, 2)

    def test_ineligible_metadata_never_requests_a_review(self):
        """Canonical label, state, target repository and actual default branch are required."""
        variants = [
            {"user": {"login": "untrusted-author"}},
            {"draft": True},
            {"state": "closed"},
            {"labels": [{"name": "auto-merge"}]},
            {"labels": []},
            {"base": {"ref": "main", "repo": {"full_name": REPOSITORY}}},
            {"base": {"ref": "stable", "repo": {"full_name": "other/project"}}},
        ]
        for changes in variants:
            with self.subTest(changes=changes):
                self.setUp()
                self.environment["GITHUB_ACTOR"] = "4alvit"
                self.pr.update(changes)
                self.execute()
                self.assert_posts(0)
                self.assertEqual(len(self.calls), 2)

    def test_missing_token_fails_before_api_access(self):
        """Missing credentials must fail before any API command is attempted."""
        self.environment.pop("BOT_PAT")
        with self.assertRaisesRegex(RuntimeError, "Configure BOT_PAT or APPROVAL_PAT"):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_self_reviewer_fails_case_insensitively(self):
        """Reject the PR author as reviewer even when login casing differs."""
        self.reviewer = "CALIFORNIANTIRAMISU"
        with self.assertRaisesRegex(RuntimeError, "cannot approve its own PR"):
            self.execute()
        self.assert_posts(0)

    def test_independent_approval_token_takes_precedence(self):
        """Use the independent token on every API request when configured."""
        self.environment["APPROVAL_PAT"] = "test-independent-token"
        self.execute()
        self.assert_posts(1)
        self.assertTrue(
            all(
                kwargs["env"]["GH_TOKEN"] == "test-independent-token"
                for _, kwargs in self.calls
            )
        )

    def test_approval_token_works_without_bot_token(self):
        """An independent reviewer token is sufficient without a bot token."""
        self.environment.pop("BOT_PAT")
        self.environment["APPROVAL_PAT"] = "test-independent-token"
        self.execute()
        self.assert_posts(1)

    def test_old_commit_approval_does_not_approve_new_commit(self):
        """An older commit approval must trigger a review of the current head."""
        self.pages = [[self.review(head="b" * 40)]]
        self.execute()
        self.assert_posts(1)

    def test_latest_same_head_approval_is_idempotent(self):
        """Avoid duplicate reviews when the latest review already approves this head."""
        self.pages = [[self.review()]]
        self.execute()
        self.assert_posts(0)
        self.assertEqual(self.read_count, 1)

    def test_changes_requested_after_approval_requires_fresh_review(self):
        """A later changes-requested review supersedes the previous approval."""
        self.pages = [[self.review(), self.review(state="CHANGES_REQUESTED")]]
        self.execute()
        self.assert_posts(1)

    def test_other_reviewer_cannot_suppress_approval(self):
        """Approval by another account does not substitute for this reviewer."""
        self.pages = [[self.review(user="another-reviewer")]]
        self.execute()
        self.assert_posts(1)

    def test_pending_review_does_not_suppress_approval(self):
        """An unfinished review does not count as an approval."""
        self.pages = [[self.review(state="PENDING")]]
        self.execute()
        self.assert_posts(1)

    def test_paginated_latest_review_prevents_duplicate(self):
        """An approval on the second API page still prevents a duplicate review."""
        self.pages = [[self.review(head="b" * 40)], [self.review()]]
        self.execute()
        self.assert_posts(0)

    def test_paginated_later_changes_requested_requires_review(self):
        """A second-page changes request supersedes an earlier-page approval."""
        self.pages = [[self.review()], [self.review(state="CHANGES_REQUESTED")]]
        self.execute()
        self.assert_posts(1)

    def test_concurrent_metadata_changes_never_post_stale_approval(self):
        """Recheck eligibility and head immediately before submitting approval."""
        for changes in (
            {"head": {"sha": "c" * 40}},
            {"labels": []},
            {"draft": True},
            {"state": "closed"},
        ):
            with self.subTest(changes=changes):
                self.setUp()
                self.current.update(changes)
                self.execute()
                self.assert_posts(0)
                self.assertEqual(self.read_count, 2)

    def test_api_failure_propagates_without_mutation(self):
        """A failed GitHub API command must stop approval with a clear error."""
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "gh")),
            self.assertRaisesRegex(RuntimeError, "GitHub API request failed"),
        ):
            # Execute the real entrypoint while every attempted API call fails locally.
            # pylint: disable-next=exec-used
            exec(compile(self.code, str(WORKFLOW), "exec"), {"__name__": "__main__"})  # noqa: S102
        self.assert_posts(0)
