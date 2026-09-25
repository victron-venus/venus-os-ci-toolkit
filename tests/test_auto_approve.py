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
BASE = "d" * 40


# API fixtures retain separate snapshots and each public test covers a distinct contract.
# pylint: disable-next=too-many-instance-attributes,too-many-public-methods
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
            "labels": [],
            "head": {"sha": HEAD},
            "base": {"sha": BASE, "ref": "stable", "repo": {"full_name": REPOSITORY}},
        }
        self.current = copy.deepcopy(self.pr)
        self.reviewer = "independent-reviewer"
        self.token_reviewers = {}
        self.pages = [[]]
        self.calls = []
        self.read_count = 0
        self.base_reads = [BASE, BASE]
        self.decisions = ["APPROVED", "APPROVED"]

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
        elif path == f"repos/{REPOSITORY}/git/ref/heads/stable":
            self.assertEqual(command[3:], [])
            result = {"object": {"sha": self.base_reads.pop(0)}}
        elif path == "graphql":
            self.assertEqual(command[3:], ["--method", "POST", "--input", "-"])
            payload = json.loads(kwargs["input"])
            self.assertIn("reviewDecision", payload["query"])
            self.assertEqual(
                payload["variables"],
                {"owner": "example", "name": "project", "number": 17},
            )
            result = {
                "data": {
                    "repository": {
                        "pullRequest": {"reviewDecision": self.decisions.pop(0)}
                    }
                }
            }
        elif path == "user":
            self.assertEqual(command[3:], [])
            result = {
                "login": self.token_reviewers.get(
                    kwargs["env"]["GH_TOKEN"], self.reviewer
                )
            }
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
                    "body": (
                        f"Approved the current head against base commit {BASE}.\n\n"
                        f"<!-- toolkit-approval-base:{BASE}:pr-base:"
                        f"{self.pr['base']['sha']} -->"
                    ),
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
        self.assertEqual(
            sum(command[2] == f"{ENDPOINT}/reviews" for command, _ in self.calls), count
        )

    def test_author_not_actor_controls_approval(self):
        """A trusted author remains eligible when a different actor sends the event."""
        self.assertNotIn("github.actor", self.job["if"])
        self.assertIn("github.event.pull_request.user.login", self.job["if"])
        self.execute()
        self.assert_posts(1)
        self.assertEqual(self.read_count, 2)

    def test_workflow_guards_do_not_require_labels(self):
        """Both caller paths and the reusable job admit trusted, ready unlabeled PRs."""
        root = WORKFLOW.parents[2]
        for workflow in (
            WORKFLOW,
            root / ".github/workflows/auto-approve.yml",
            root / "docs/examples/auto-approve.yml",
        ):
            with self.subTest(workflow=workflow):
                definition = yaml.load(workflow.read_text(), Loader=yaml.BaseLoader)
                condition = definition["jobs"]["auto-approve"]["if"]
                self.assertNotIn("labels", condition)
                self.assertIn("contains(fromJSON(", condition)
                self.assertIn("github.event.pull_request.user.login", condition)
                self.assertIn("!github.event.pull_request.draft", condition)

    def test_labels_do_not_control_approval(self):
        """Approve the current head with no labels or any unrelated or merge label."""
        for labels in (
            [],
            [{"name": "bug"}],
            [{"name": "auto-merge"}],
            [{"name": "automerge"}],
        ):
            with self.subTest(labels=labels):
                self.setUp()
                self.pr["labels"] = labels
                self.current = copy.deepcopy(self.pr)
                self.execute()
                self.assert_posts(1)
                self.assertEqual(self.read_count, 2)

    def test_label_changes_during_recheck_do_not_cancel_approval(self):
        """Adding, removing, or replacing labels leaves current-head approval eligible."""
        label_sets = ([], [{"name": "automerge"}], [{"name": "bug"}])
        for initial in label_sets:
            for current in label_sets:
                if initial == current:
                    continue
                with self.subTest(initial=initial, current=current):
                    self.setUp()
                    self.pr["labels"] = initial
                    self.current["labels"] = current
                    self.execute()
                    self.assert_posts(1)
                    self.assertEqual(self.read_count, 2)

    def test_ineligible_metadata_never_requests_a_review(self):
        """Trusted author, ready state, target repository and default branch are required."""
        variants = [
            {"user": {"login": "untrusted-author"}},
            {"draft": True},
            {"state": "closed"},
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

    def test_dependabot_uses_installation_token_without_user_endpoint(self):
        """Dependabot reviews use the Actions identity even when PATs are present."""
        self.environment.update(
            {
                "PR_AUTHOR": "dependabot[bot]",
                "GITHUB_TOKEN": "test-actions-token",
                "TRUSTED_AUTHORS": '["dependabot[bot]"]',
            }
        )
        self.pr["user"]["login"] = "dependabot[bot]"
        self.current = copy.deepcopy(self.pr)
        self.execute()
        self.assert_posts(1)
        self.assertNotIn("user", [command[2] for command, _ in self.calls])
        self.assertTrue(
            all(
                kwargs["env"]["GH_TOKEN"] == "test-actions-token"
                for _, kwargs in self.calls
            )
        )

    def test_dependabot_without_any_pat_can_approve(self):
        """No Dependabot secret is required for the repository installation token."""
        self.environment.pop("BOT_PAT")
        self.test_dependabot_uses_installation_token_without_user_endpoint()

    def test_dependabot_does_not_fall_back_to_pat(self):
        """A missing installation token must never select a supplied user PAT."""
        self.environment["PR_AUTHOR"] = "dependabot[bot]"
        with self.assertRaisesRegex(RuntimeError, "GITHUB_TOKEN is missing"):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_human_pr_keeps_pat_even_when_actions_token_exists(self):
        """The credential change must not alter owner PR approval or fallback."""
        self.environment["GITHUB_TOKEN"] = "test-actions-token"
        self.test_independent_bot_token_remains_primary()

    def test_dependabot_refreshes_only_its_own_review(self):
        """A same-head Actions approval is recognized without inspecting a PAT."""
        self.environment.update(
            {
                "PR_AUTHOR": "dependabot[bot]",
                "GITHUB_TOKEN": "test-actions-token",
                "TRUSTED_AUTHORS": '["dependabot[bot]"]',
            }
        )
        self.pr["user"]["login"] = "dependabot[bot]"
        self.current = copy.deepcopy(self.pr)
        self.pages = [[self.review(user="github-actions[bot]")]]
        self.execute()
        self.assert_posts(0)

    def test_event_author_cannot_select_actions_token_for_human_pr(self):
        """Live PR metadata must agree with the event's credential choice."""
        self.environment.update(
            {"PR_AUTHOR": "dependabot[bot]", "GITHUB_TOKEN": "test-actions-token"}
        )
        with self.assertRaisesRegex(RuntimeError, "author does not match"):
            self.execute()
        self.assert_posts(0)

    def test_self_reviewer_fails_case_insensitively(self):
        """Reject the PR author as reviewer even when login casing differs."""
        self.reviewer = "CALIFORNIANTIRAMISU"
        with self.assertRaisesRegex(RuntimeError, "cannot approve its own PR"):
            self.execute()
        self.assert_posts(0)

    def test_independent_bot_token_remains_primary(self):
        """A secondary owner token must not break ordinary owner-authored PRs."""
        self.pr["user"]["login"] = "4alvit"
        self.current = copy.deepcopy(self.pr)
        self.environment["APPROVAL_PAT"] = "test-independent-token"
        self.token_reviewers = {
            "test-bot-token": "californiantiramisu",
            "test-independent-token": "4alvit",
        }
        self.execute()
        self.assert_posts(1)
        self.assertTrue(
            all(
                kwargs["env"]["GH_TOKEN"] == "test-bot-token"
                for _, kwargs in self.calls
            )
        )

    def test_bot_author_uses_independent_fallback(self):
        """Query both identities and submit the review only with the fallback."""
        self.environment["APPROVAL_PAT"] = "test-independent-token"
        self.token_reviewers = {
            "test-bot-token": "CALIFORNIANTIRAMISU",
            "test-independent-token": "4alvit",
        }
        self.execute()
        self.assert_posts(1)
        self.assertEqual(
            [
                kwargs["env"]["GH_TOKEN"]
                for command, kwargs in self.calls
                if "POST" in command
            ],
            ["test-independent-token"],
        )

    def test_both_tokens_belonging_to_author_fail(self):
        """A second secret is not independent merely because its name differs."""
        self.environment["APPROVAL_PAT"] = "test-independent-token"
        self.reviewer = "CALIFORNIANTIRAMISU"
        with self.assertRaisesRegex(RuntimeError, "cannot approve its own PR"):
            self.execute()
        self.assert_posts(0)

    def test_invalid_primary_identity_does_not_fall_back(self):
        """A failing identity lookup must remain visible even with a valid fallback."""
        self.environment["APPROVAL_PAT"] = "test-independent-token"
        original_process = self.api_process

        def reject_primary_identity(command, **kwargs):
            if command[2] == "user":
                raise subprocess.CalledProcessError(
                    1, command, stderr="Bad credentials"
                )
            return original_process(command, **kwargs)

        with (
            patch.object(self, "api_process", side_effect=reject_primary_identity),
            self.assertRaisesRegex(RuntimeError, "Bad credentials"),
        ):
            self.execute()
        self.assert_posts(0)
        self.assertTrue(
            all(
                kwargs["env"]["GH_TOKEN"] == "test-bot-token"
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
            {
                "base": {
                    "sha": "e" * 40,
                    "ref": "stable",
                    "repo": {"full_name": REPOSITORY},
                }
            },
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

    def test_stale_same_head_approval_is_refreshed(self):
        """GitHub can require reapproval after the merge base changes without a push."""
        self.pages = [[self.review()]]
        self.decisions = ["REVIEW_REQUIRED", "REVIEW_REQUIRED"]
        self.execute()
        self.assert_posts(1)

    def test_other_required_review_does_not_repeat_our_fresh_approval(self):
        """Persisted base metadata prevents repeated reviews when more reviewers are needed."""
        review = self.review()
        review["body"] = f"<!-- toolkit-approval-base:{BASE}:pr-base:{BASE} -->"
        self.pages = [[review]]
        self.decisions = ["REVIEW_REQUIRED"]
        self.execute()
        self.assert_posts(0)

    def test_base_change_allows_one_more_required_review(self):
        """An approval marker for an older base cannot hide GitHub's stale decision."""
        review = self.review()
        review["body"] = "<!-- toolkit-approval-base:older-base -->"
        self.pages = [[review]]
        self.decisions = ["REVIEW_REQUIRED", "REVIEW_REQUIRED"]
        self.execute()
        self.assert_posts(1)

    def test_changes_requested_decision_does_not_repeat_same_approval(self):
        """Do not overwrite another reviewer's objection with a repeated approval."""
        self.pages = [[self.review()]]
        self.decisions = ["CHANGES_REQUESTED"]
        self.execute()
        self.assert_posts(0)

    def test_unknown_review_requirement_does_not_repeat_approval(self):
        """Repositories without a review decision do not need duplicate reviews."""
        self.pages = [[self.review()]]
        self.decisions = [None]
        self.execute()
        self.assert_posts(0)

    def test_concurrent_base_tip_change_prevents_review(self):
        """Read the actual branch tip twice because PR base metadata may lag main."""
        self.base_reads = [BASE, "e" * 40]
        self.execute()
        self.assert_posts(0)

    def test_concurrent_requirement_resolution_prevents_duplicate(self):
        """A review supplied during the recheck removes the need to reapprove."""
        self.pages = [[self.review()]]
        self.decisions = ["REVIEW_REQUIRED", "APPROVED"]
        self.execute()
        self.assert_posts(0)

    def test_lagging_pr_base_refreshes_even_when_default_tip_is_unchanged(self):
        """GitHub may update the PR merge base after its default-branch tip was observed."""
        review = self.review()
        review["body"] = f"<!-- toolkit-approval-base:{BASE}:pr-base:{BASE} -->"
        self.pages = [[review]]
        self.pr["base"]["sha"] = "e" * 40
        self.current = copy.deepcopy(self.pr)
        self.decisions = ["REVIEW_REQUIRED", "REVIEW_REQUIRED"]
        self.execute()
        self.assert_posts(1)


class ManualApprovalRecoveryTests(unittest.TestCase):
    """An explicit recovery dispatch cannot target a fork or a moving head."""

    def setUp(self):
        """Use the same API transport with reviewed dispatch inputs."""
        self.harness = AutoApproveContract()
        self.harness.setUp()
        self.environment = self.harness.environment
        self.pr = self.harness.pr
        self.environment.update(
            {
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "EXPLICIT_PR_NUMBER": "17",
                "EXPECTED_HEAD": HEAD,
            }
        )
        self.pr["head"]["repo"] = {"full_name": REPOSITORY}
        self.current = copy.deepcopy(self.pr)
        self.harness.current = self.current

    def execute(self):
        """Use the real reusable-workflow API fixture without inheriting event tests."""
        self.harness.execute()

    def assert_posts(self, count):
        """Keep the same strict review mutation assertion as event-mode tests."""
        self.harness.assert_posts(count)

    def test_exact_head_dispatch_approves_independently(self):
        """The explicit head is both validated and attached to the review."""
        self.execute()
        self.assert_posts(1)

    def test_recovery_requires_complete_dispatch_inputs(self):
        """No partial selector, shell payload or non-dispatch trigger is accepted."""
        for changes in (
            {"GITHUB_EVENT_NAME": "pull_request_target"},
            {"EXPLICIT_PR_NUMBER": ""},
            {"EXPLICIT_PR_NUMBER": "0"},
            {"EXPLICIT_PR_NUMBER": "17;true"},
            {"EXPECTED_HEAD": ""},
            {"EXPECTED_HEAD": "main"},
            {"PR_NUMBER": "18"},
        ):
            with self.subTest(changes=changes), patch.dict(self.environment, changes):
                with self.assertRaisesRegex(RuntimeError, "Manual recovery requires"):
                    self.execute()
        self.assert_posts(0)

    def test_recovery_rejects_initial_fork_and_unexpected_head(self):
        """Same-repository identity and the caller's expected commit are mandatory."""
        for changes in (
            {"repo": None},
            {"repo": {"full_name": "fork/project"}},
            {"sha": "b" * 40},
        ):
            with self.subTest(changes=changes), patch.dict(self.pr["head"], changes):
                with self.assertRaisesRegex(RuntimeError, "expected same-repository"):
                    self.execute()
                self.harness.read_count = 0
        self.assert_posts(0)

    def test_recovery_rechecks_repository_before_review(self):
        """The final refresh also verifies repository identity."""
        self.current["head"]["repo"] = {"full_name": "fork/project"}
        self.execute()
        self.assert_posts(0)

    def test_recovery_retains_trust_draft_and_base_checks(self):
        """An exact expected commit does not authorize untrusted or draft PRs."""
        original = copy.deepcopy(self.pr)
        for changes in (
            {"draft": True},
            {"state": "closed"},
            {"user": {"login": "stranger"}},
            {"base": {"sha": BASE, "ref": "other", "repo": {"full_name": REPOSITORY}}},
        ):
            with self.subTest(changes=changes):
                self.harness.pr = original | changes
                self.harness.read_count = 0
                self.execute()
        self.assert_posts(0)

    def test_recovery_rejects_self_review(self):
        """The configured token must belong to an independent reviewer."""
        self.harness.reviewer = self.pr["user"]["login"]
        with self.assertRaisesRegex(RuntimeError, "cannot approve its own"):
            self.execute()
        self.assert_posts(0)

    def test_recovery_rejects_head_change_before_review(self):
        """A commit pushed during metadata reads withdraws approval eligibility."""
        self.current["head"]["sha"] = "b" * 40
        self.execute()
        self.assert_posts(0)
