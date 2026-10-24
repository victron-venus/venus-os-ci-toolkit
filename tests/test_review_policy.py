"""Exercise the real metadata-only review gate against an adversarial offline API."""

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/review-policy-reusable.yml"
REPOSITORY = "example/project"
BASE = f"repos/{REPOSITORY}"
PR = f"{BASE}/pulls/17"
HEAD = "a" * 40
BOT = {"login": "coderabbitai[bot]", "id": 136622811, "type": "Bot"}
REVIEWER = {"login": "reviewer", "id": 42, "type": "User"}
AUTHOR = {"login": "author", "id": 43, "type": "User"}


def review(state="APPROVED", *, user=None, head=HEAD, number=1, time=None):
    """Create API evidence, including submission time distinct from ID order."""
    return {
        "id": number,
        "state": state,
        "user": user or BOT,
        "commit_id": head,
        "submitted_at": time or f"2026-10-24T21:{number:02d}:00Z",
    }


def status(description="Review rate limited", *, state="success", user=None, number=1):
    """Create a commit status returned for the requested exact head."""
    return {
        "id": number,
        "context": "CodeRabbit",
        "creator": user or BOT,
        "state": state,
        "description": description,
        "created_at": f"2026-10-24T21:{number:02d}:00Z",
    }


# Each test drives the actual workflow with sequential authoritative snapshots.
# pylint: disable=too-many-instance-attributes,too-many-public-methods
class ReviewPolicyContract(unittest.TestCase):
    """No real token, network, review, PR content, or shell execution is used."""

    def setUp(self):
        self.workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        self.job = self.workflow["jobs"]["review-policy"]
        self.code = next(step["run"] for step in self.job["steps"] if "run" in step)
        self.environment = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/review-policy.yml@refs/heads/main",
            "GITHUB_EVENT_NAME": "pull_request_target",
            "GITHUB_RUN_ID": "1234",
            "ALLOW_CODERABBIT_RATE_LIMIT": "true",
            "GH_TOKEN": "test-token",
            "PR_NUMBER": "",
        }
        self.pr = {
            "number": 17,
            "state": "open",
            "draft": False,
            "user": AUTHOR,
            "head": {"sha": HEAD, "repo": {"full_name": REPOSITORY}},
            "base": {"sha": "b" * 40, "ref": "main", "repo": {"full_name": REPOSITORY}},
            "title": "$(never execute)",
            "body": "`do not execute`",
        }
        self.current_pr = copy.deepcopy(self.pr)
        self.event = {"action": "opened", "pull_request": copy.deepcopy(self.pr)}
        self.reviews = [[]]
        self.current_reviews = None
        self.statuses = [[status()]]
        self.current_statuses = None
        self.permission = {"permission": "write", "user": REVIEWER}
        self.calls = []
        self.counts = {}
        self.posts = []
        self.post_shas = []
        self.failure_path = None
        self.extra_pr = None
        self.relay_changed = False

    def api_process(self, command, **kwargs):
        """Allow exactly the documented metadata reads and commit-status writes."""
        self.assertEqual(command[:2], ["gh", "api"])
        self.assertEqual(kwargs["env"]["GH_TOKEN"], "test-token")
        self.assertEqual(kwargs["env"]["GH_HOST"], "github.com")
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["check"])
        self.calls.append(command)
        path = command[2]
        self.counts[path] = self.counts.get(path, 0) + 1
        if self.failure_path == path:
            raise subprocess.CalledProcessError(1, command, stderr="secret test-token")
        if path == BASE:
            result = {
                "full_name": REPOSITORY,
                "default_branch": "main",
                "archived": False,
            }
        elif path == PR:
            result = self.pr if self.counts[path] == 1 else self.current_pr
        elif self.extra_pr and path == f"{BASE}/pulls/18":
            result = self.extra_pr
        elif self.extra_pr and path == f"{BASE}/pulls/18/reviews?per_page=100":
            self.assertEqual(command[3:], ["--paginate", "--slurp"])
            result = self.reviews
        elif path == f"{PR}/reviews?per_page=100":
            self.assertEqual(command[3:], ["--paginate", "--slurp"])
            result = (
                self.reviews
                if self.counts[path] == 1 or self.current_reviews is None
                else self.current_reviews
            )
        elif (
            self.extra_pr and path == f"{BASE}/commits/{'f' * 40}/statuses?per_page=100"
        ):
            self.assertEqual(command[3:], ["--paginate", "--slurp"])
            result = self.statuses
        elif path == f"{BASE}/commits/{HEAD}/statuses?per_page=100":
            self.assertEqual(command[3:], ["--paginate", "--slurp"])
            result = (
                self.statuses
                if self.counts[path] == 1 or self.current_statuses is None
                else self.current_statuses
            )
        elif path.startswith(
            f"{BASE}/contents/.github/workflows/review-policy-events.yml?ref="
        ):
            commit = path.rsplit("=", 1)[-1]
            self.assertIn(commit, {HEAD, "b" * 40, "f" * 40})
            blob = "c" * 40 if self.relay_changed and commit == HEAD else "d" * 40
            result = {
                "type": "file",
                "path": ".github/workflows/review-policy-events.yml",
                "sha": blob,
            }
        elif path == f"{BASE}/collaborators/reviewer/permission":
            result = self.permission
        elif path == f"{BASE}/pulls?state=open&base=main&per_page=100":
            self.assertEqual(command[3:], ["--paginate", "--slurp"])
            result = [[self.pr] + ([self.extra_pr] if self.extra_pr else [])]
        elif path in {f"{BASE}/statuses/{sha}" for sha in (HEAD, "e" * 40, "f" * 40)}:
            self.assertEqual(command[3:], ["--method", "POST", "--input", "-"])
            payload = json.loads(kwargs["input"])
            self.assertEqual(payload["context"], "Review policy")
            self.assertEqual(
                payload["target_url"],
                f"https://github.com/{REPOSITORY}/actions/runs/1234",
            )
            self.posts.append(payload)
            self.post_shas.append(path.rsplit("/", 1)[-1])
            result = {"id": 55}
        else:
            raise AssertionError(f"Unexpected API request: {command}")
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

    def execute(self):
        """Run the workflow entry point with every subprocess intercepted."""
        with tempfile.TemporaryDirectory() as directory:
            event_file = Path(directory) / "event.json"
            event_file.write_text(
                json.dumps(
                    {
                        "repository": {
                            "full_name": REPOSITORY,
                            "default_branch": "main",
                        },
                        **self.event,
                    }
                )
            )
            environment = {**self.environment, "GITHUB_EVENT_PATH": str(event_file)}
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("subprocess.run", side_effect=self.api_process),
            ):
                # pylint: disable-next=exec-used
                exec(
                    compile(self.code, str(WORKFLOW), "exec"), {"__name__": "__main__"}
                )  # noqa: S102

    def assert_result(self, result):
        """Every normal path invalidates the old decision before reading evidence."""
        self.assertEqual([post["state"] for post in self.posts], ["pending", result])
        status_position = next(
            index
            for index, call in enumerate(self.calls)
            if call[2] == f"{BASE}/statuses/{HEAD}"
        )
        review_position = next(
            index for index, call in enumerate(self.calls) if "/reviews?" in call[2]
        )
        self.assertLess(status_position, review_position)

    def test_verified_current_head_quota_can_pass(self):
        self.execute()
        self.assert_result("success")
        self.assertIn("rate limit", self.posts[-1]["description"])

    def test_quota_opt_in_is_required(self):
        self.environment["ALLOW_CODERABBIT_RATE_LIMIT"] = "false"
        self.execute()
        self.assert_result("pending")

    def test_only_exact_authenticated_quota_is_accepted(self):
        for evidence in (
            status("Review rate limited; please upgrade"),
            status(state="pending"),
            status("Review completed"),
            status(user={**BOT, "id": 1}),
            status(user={**BOT, "type": "User"}),
            status(user={**BOT, "login": "fake[bot]"}),
            {**status(), "context": "Other review"},
        ):
            with self.subTest(evidence=evidence):
                self.setUp()
                self.statuses = [[evidence]]
                self.execute()
                self.assert_result("pending")

    def test_latest_trusted_status_supersedes_old_quota_across_pages(self):
        self.statuses = [
            [status("Review in progress", state="pending", number=2)],
            [status()],
        ]
        self.execute()
        self.assert_result("pending")

    def test_untrusted_status_cannot_overwrite_authentic_quota(self):
        self.statuses = [[status(user={**BOT, "id": 1}, number=2)], [status()]]
        self.execute()
        self.assert_result("success")

    def test_authentic_current_head_approval_needs_no_quota(self):
        self.reviews = [[review()]]
        self.statuses = [[status("Review in progress", state="pending")]]
        self.environment["ALLOW_CODERABBIT_RATE_LIMIT"] = "false"
        self.execute()
        self.assert_result("success")
        self.assertFalse(any("/commits/" in call[2] for call in self.calls))

    def test_older_head_approval_is_not_enough(self):
        self.reviews = [[review(head="c" * 40)]]
        self.statuses = [[]]
        self.execute()
        self.assert_result("pending")

    def test_spoofed_bot_review_is_not_approval(self):
        self.reviews = [[review(user={**BOT, "id": 1})]]
        self.statuses = [[]]
        self.execute()
        self.assert_result("pending")

    def test_author_cannot_supply_approval(self):
        self.reviews = [[review(user=AUTHOR)]]
        self.statuses = [[]]
        self.execute()
        self.assert_result("pending")

    def test_collaborator_permission_is_live_and_required(self):
        for permission, result in (
            ("write", "success"),
            ("maintain", "success"),
            ("admin", "success"),
            ("read", "pending"),
            ("triage", "pending"),
        ):
            with self.subTest(permission=permission):
                self.setUp()
                self.reviews = [[review(user=REVIEWER)]]
                self.statuses = [[]]
                self.permission["permission"] = permission
                self.execute()
                self.assert_result(result)

    def test_permission_identity_mismatch_fails_closed(self):
        self.reviews = [[review(user=REVIEWER)]]
        self.permission["user"] = AUTHOR
        with self.assertRaisesRegex(RuntimeError, "failed closed"):
            self.execute()
        self.assert_result("failure")

    def test_old_head_change_request_blocks_quota(self):
        self.reviews = [[review("CHANGES_REQUESTED", head="c" * 40)]]
        self.execute()
        self.assert_result("failure")

    def test_comment_does_not_erase_change_request(self):
        self.reviews = [[review("CHANGES_REQUESTED"), review("COMMENTED", number=2)]]
        self.execute()
        self.assert_result("failure")

    def test_comment_does_not_erase_approval(self):
        self.reviews = [[review(), review("COMMENTED", number=2)]]
        self.statuses = [[]]
        self.execute()
        self.assert_result("success")

    def test_dismissal_or_approval_supersedes_change_request(self):
        for state in ("DISMISSED", "APPROVED"):
            with self.subTest(state=state):
                self.setUp()
                self.reviews = [[review("CHANGES_REQUESTED"), review(state, number=2)]]
                self.execute()
                self.assert_result("success")

    def test_review_submission_time_takes_precedence_over_creation_id(self):
        self.reviews = [
            [
                review("CHANGES_REQUESTED", number=1, time="2026-10-24T22:00:00Z"),
                review(number=2),
            ]
        ]
        self.execute()
        self.assert_result("failure")

    def test_change_request_from_other_trusted_reviewer_blocks_approval(self):
        self.reviews = [
            [review()],
            [review("CHANGES_REQUESTED", user=REVIEWER, number=2)],
        ]
        self.execute()
        self.assert_result("failure")

    def test_api_errors_fail_closed_without_token_disclosure(self):
        self.failure_path = f"{PR}/reviews?per_page=100"
        with self.assertRaisesRegex(RuntimeError, "failed closed") as failure:
            self.execute()
        self.assertNotIn("test-token", str(failure.exception))
        self.assert_result("failure")

    def test_evidence_must_still_qualify_immediately_before_publish(self):
        self.current_statuses = [
            [status("Review in progress", state="pending", number=2)],
            [status()],
        ]
        with self.assertRaisesRegex(RuntimeError, "failed closed"):
            self.execute()
        self.assert_result("failure")

    def test_new_change_request_blocks_previously_seen_approval(self):
        self.reviews = [[review()]]
        self.current_reviews = [[review(), review("CHANGES_REQUESTED", number=2)]]
        with self.assertRaisesRegex(RuntimeError, "failed closed"):
            self.execute()
        self.assert_result("failure")

    def test_head_base_author_or_open_state_change_fails_closed(self):
        for changed in ("head", "base", "author", "closed"):
            with self.subTest(changed=changed):
                self.setUp()
                if changed in {"head", "base"}:
                    self.current_pr[changed]["sha"] = "d" * 40
                elif changed == "author":
                    self.current_pr["user"] = REVIEWER
                else:
                    self.current_pr["state"] = "closed"
                with self.assertRaisesRegex(RuntimeError, "failed closed"):
                    self.execute()
                self.assert_result("failure")

    def test_merge_ref_and_branch_dispatch_cannot_evaluate(self):
        for ref in ("refs/pull/17/merge", "refs/heads/untrusted"):
            with self.subTest(ref=ref):
                self.setUp()
                self.environment["GITHUB_WORKFLOW_REF"] = (
                    f"{REPOSITORY}/.github/workflows/review-policy.yml@{ref}"
                )
                with self.assertRaisesRegex(RuntimeError, "trusted default branch"):
                    self.execute()
                self.assertEqual(self.posts, [])

    def test_direct_review_event_cannot_evaluate(self):
        self.environment["GITHUB_EVENT_NAME"] = "pull_request_review"
        with self.assertRaisesRegex(RuntimeError, "read-only relay"):
            self.execute()
        self.assertEqual(self.posts, [])

    def test_default_branch_manual_recovery(self):
        self.environment.update(GITHUB_EVENT_NAME="workflow_dispatch", PR_NUMBER="17")
        self.execute()
        self.assert_result("success")

    def test_authenticated_status_event_reconciles_live_heads(self):
        self.environment["GITHUB_EVENT_NAME"] = "status"
        self.event = {"context": "CodeRabbit", "sender": BOT, "sha": HEAD}
        self.execute()
        self.assert_result("success")

    def test_own_or_forged_status_event_is_ignored_without_loop(self):
        for event in (
            {"context": "Review policy", "sender": BOT, "sha": HEAD},
            {"context": "CodeRabbit", "sender": {**BOT, "id": 1}, "sha": HEAD},
            {"context": "Other", "sender": BOT, "sha": "e" * 40},
        ):
            with self.subTest(event=event):
                self.setUp()
                self.environment["GITHUB_EVENT_NAME"] = "status"
                self.event = event
                self.execute()
                self.assertEqual(self.posts, [])

    def test_status_for_another_head_still_reconciles_all_open_prs(self):
        self.environment["GITHUB_EVENT_NAME"] = "status"
        self.event = {"context": "CodeRabbit", "sender": BOT, "sha": "e" * 40}
        self.execute()
        self.assertEqual(
            [post["state"] for post in self.posts], ["pending", "pending", "success"]
        )
        self.assertEqual(self.post_shas, ["e" * 40, HEAD, HEAD])

    def test_unprivileged_review_event_relay_uses_live_metadata(self):
        self.environment["GITHUB_EVENT_NAME"] = "workflow_run"
        self.event = {
            "action": "completed",
            "workflow_run": {
                "name": "Review policy events",
                "event": "pull_request_review",
                "path": ".github/workflows/review-policy-events.yml",
                "conclusion": "failure",
                "pull_requests": [],
                "body": "malicious instructions ignored",
            },
        }
        self.execute()
        self.assert_result("success")

    def test_unrecognized_workflow_relay_is_rejected(self):
        self.environment["GITHUB_EVENT_NAME"] = "workflow_run"
        self.event = {"action": "completed", "workflow_run": {"name": "Other"}}
        with self.assertRaisesRegex(RuntimeError, "Unrecognized"):
            self.execute()
        self.assertEqual(self.posts, [])

    def test_draft_or_closed_pr_cannot_get_success(self):
        for state in ("draft", "closed"):
            with self.subTest(state=state):
                self.setUp()
                if state == "draft":
                    self.pr["draft"] = True
                else:
                    self.pr["state"] = "closed"
                self.execute()
                self.assertFalse(any(post["state"] == "success" for post in self.posts))

    def test_failed_pr_does_not_starve_other_open_pr(self):
        self.extra_pr = copy.deepcopy(self.pr)
        self.extra_pr["number"] = 18
        self.extra_pr["head"]["sha"] = "f" * 40
        self.failure_path = f"{PR}/reviews?per_page=100"
        with self.assertRaisesRegex(RuntimeError, "failed closed for 1"):
            self.execute()
        self.assertEqual(
            [post["state"] for post in self.posts],
            ["pending", "pending", "failure", "success"],
        )
        self.assertEqual(self.post_shas, [HEAD, "f" * 40, HEAD, "f" * 40])

    def test_event_head_is_invalidated_before_repository_lookup_failure(self):
        self.failure_path = BASE
        with self.assertRaisesRegex(RuntimeError, "API request failed"):
            self.execute()
        self.assertEqual([post["state"] for post in self.posts], ["pending"])

    def test_event_head_is_invalidated_before_pr_list_failure(self):
        self.failure_path = f"{BASE}/pulls?state=open&base=main&per_page=100"
        with self.assertRaisesRegex(RuntimeError, "API request failed"):
            self.execute()
        self.assertEqual([post["state"] for post in self.posts], ["pending"])

    def test_listed_head_is_invalidated_before_pr_read_failure(self):
        self.environment.update(GITHUB_EVENT_NAME="workflow_dispatch", PR_NUMBER="17")
        self.failure_path = PR
        with self.assertRaisesRegex(RuntimeError, "failed closed"):
            self.execute()
        self.assertEqual([post["state"] for post in self.posts], ["pending"])

    def test_relay_change_requires_real_approval(self):
        self.relay_changed = True
        self.execute()
        self.assert_result("failure")
        self.assertIn("relay changes", self.posts[-1]["description"])

    def test_real_approval_can_authorize_relay_maintenance(self):
        self.relay_changed = True
        self.reviews = [[review()]]
        self.execute()
        self.assert_result("success")
        self.assertFalse(any("/contents/" in call[2] for call in self.calls))

    def test_missing_relay_file_cannot_get_quota_exception(self):
        self.failure_path = (
            f"{BASE}/contents/.github/workflows/review-policy-events.yml?ref={HEAD}"
        )
        with self.assertRaisesRegex(RuntimeError, "failed closed"):
            self.execute()
        self.assert_result("failure")

    def test_scheduled_sweep_uses_authoritative_metadata(self):
        self.environment["GITHUB_EVENT_NAME"] = "schedule"
        self.event = {"schedule": "7,22,37,52 * * * *"}
        self.execute()
        self.assert_result("success")

    def test_shared_head_cannot_mix_two_pr_review_decisions(self):
        self.extra_pr = copy.deepcopy(self.pr)
        self.extra_pr["number"] = 18
        with self.assertRaisesRegex(RuntimeError, "failed closed"):
            self.execute()
        self.assertEqual([post["state"] for post in self.posts], ["pending", "failure"])
        self.assertIn("Multiple open PRs", self.posts[-1]["description"])
        self.assertFalse(any("/reviews?" in call[2] for call in self.calls))

    def test_workflow_and_callers_preserve_privilege_boundary(self):
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.job["runs-on"], "ubuntu-latest")
        self.assertEqual(
            self.job["permissions"],
            {"contents": "read", "pull-requests": "read", "statuses": "write"},
        )
        self.assertEqual(self.job["concurrency"]["cancel-in-progress"], "false")
        self.assertEqual(len(self.job["steps"]), 2)
        self.assertEqual(self.job["steps"][1]["shell"], "python3 -I {0}")
        self.assertNotIn("${{", self.code)
        self.assertNotIn(
            "checkout", " ".join(step.get("uses", "") for step in self.job["steps"])
        )
        caller = yaml.load(
            (ROOT / "docs/examples/review-policy.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        relay = yaml.load(
            (ROOT / "docs/examples/review-policy-events.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(relay["permissions"], {})
        self.assertEqual(set(relay["on"]), {"pull_request_review"})
        self.assertNotIn("pull_request_review", caller["on"])
        self.assertEqual(
            caller["on"]["workflow_run"]["workflows"], ["Review policy events"]
        )
        self.assertNotIn("secrets", relay)
        self.assertEqual(caller["on"]["schedule"], [{"cron": "7,22,37,52 * * * *"}])


if __name__ == "__main__":
    unittest.main()
