"""Exercise the real metadata-only review workflow with an offline GitHub API."""

import copy
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/coderabbit-review-reusable.yml"
REPOSITORY = "example/project"
REPO_ENDPOINT = f"repos/{REPOSITORY}"
PR_ENDPOINT = f"{REPO_ENDPOINT}/pulls/17"
COMMENTS_ENDPOINT = f"{REPO_ENDPOINT}/issues/17/comments"
BOT = "californiantiramisu"
BODY = "@coderabbitai review\n\n<!-- toolkit-coderabbit-review:v1 -->"


# Separate API snapshots exercise eligibility changes immediately before a write.
# pylint: disable-next=too-many-instance-attributes,too-many-public-methods
class CodeRabbitReviewContract(unittest.TestCase):
    """Verify actual workflow code and exact requests without live comments."""

    def setUp(self):
        self.workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        self.job = self.workflow["jobs"]["request-review"]
        self.code = self.job["steps"][0]["run"]
        self.environment = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_EVENT_NAME": "pull_request_target",
            "EVENT_ACTION": "opened",
            "EVENT_PR_NUMBER": "17",
            "PR_NUMBER": "17",
            "EXPLICIT_PR_NUMBER": "",
            "BOT_PAT": "test-bot-token",
        }
        self.repository = {
            "full_name": REPOSITORY,
            "private": False,
            "visibility": "public",
            "archived": False,
            "stargazers_count": 0,
        }
        self.current_repository = copy.deepcopy(self.repository)
        self.pr = {
            "number": 17,
            "state": "open",
            "draft": True,
            "user": {"login": "first-time-contributor"},
            "head": {"repo": {"full_name": "external/fork"}},
            "base": {"repo": {"full_name": REPOSITORY}},
            "title": "$(untrusted-command)",
            "body": "`untrusted-command`",
        }
        self.current_pr = copy.deepcopy(self.pr)
        self.identity = {"login": BOT, "id": 123}
        self.pages = [[]]
        self.calls = []
        self.read_counts = {REPO_ENDPOINT: 0, PR_ENDPOINT: 0}
        self.fail_post = False

    def api_process(self, command, **kwargs):
        """Reject unexpected commands and model paginated API responses."""
        self.assertEqual(command[:2], ["gh", "api"])
        self.assertTrue(kwargs["check"])
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["env"]["GH_TOKEN"], "test-bot-token")
        self.assertEqual(kwargs["env"]["GH_HOST"], "github.com")
        self.calls.append((command, kwargs))
        path = command[2]
        if path in self.read_counts:
            self.assertEqual(command[3:], [])
            self.read_counts[path] += 1
            if path == REPO_ENDPOINT:
                snapshots = (self.repository, self.current_repository)
            else:
                snapshots = (self.pr, self.current_pr)
            result = snapshots[min(self.read_counts[path] - 1, 1)]
        elif path == "user":
            self.assertEqual(command[3:], [])
            result = self.identity
        elif path == f"{COMMENTS_ENDPOINT}?per_page=100":
            self.assertEqual(command[3:], ["--paginate", "--slurp"])
            result = self.pages
        elif path == COMMENTS_ENDPOINT:
            self.assertEqual(command[3:], ["--method", "POST", "--input", "-"])
            self.assertEqual(json.loads(kwargs["input"]), {"body": BODY})
            if self.fail_post:
                raise subprocess.CalledProcessError(
                    1, command, stderr="ambiguous API failure: test-bot-token"
                )
            result = {"id": 456}
        else:
            raise AssertionError(f"Unexpected API request: {command}")
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

    def execute(self):
        """Execute the repository-owned workflow with every subprocess intercepted."""
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch("subprocess.run", side_effect=self.api_process),
        ):
            # pylint: disable-next=exec-used
            exec(compile(self.code, str(WORKFLOW), "exec"), {"__name__": "__main__"})  # noqa: S102

    def assert_posts(self, count):
        """Count writes, including failed attempts, rather than only API successes."""
        self.assertEqual(
            sum(command[2] == COMMENTS_ENDPOINT for command, _ in self.calls), count
        )

    def test_new_fork_draft_posts_fixed_body_once(self):
        """New contributors and drafts are eligible without using their content."""
        self.execute()
        self.assert_posts(1)
        self.assertEqual(self.read_counts, {REPO_ENDPOINT: 2, PR_ENDPOINT: 2})
        self.assertEqual(self.calls[-1][0][2], COMMENTS_ENDPOINT)

    def test_nine_stars_qualifies(self):
        """The highest eligible star count is nine."""
        self.repository["stargazers_count"] = 9
        self.current_repository["stargazers_count"] = 9
        self.execute()
        self.assert_posts(1)

    def test_ten_or_more_stars_skip(self):
        """Repositories eligible for service auto-review receive no manual trigger."""
        for stars in (10, 11, 1000):
            with self.subTest(stars=stars):
                self.setUp()
                self.repository["stargazers_count"] = stars
                self.execute()
                self.assert_posts(0)
                self.assertEqual(len(self.calls), 1)

    def test_repository_metadata_fails_closed(self):
        """Private, archived, renamed or malformed repositories cannot get comments."""
        for field, value in (
            ("private", True),
            ("private", "false"),
            ("visibility", "private"),
            ("visibility", "internal"),
            ("visibility", None),
            ("archived", True),
            ("full_name", "different/project"),
            ("stargazers_count", "0"),
            ("stargazers_count", False),
            ("stargazers_count", -1),
            ("stargazers_count", None),
        ):
            with self.subTest(field=field, value=value):
                self.setUp()
                self.repository[field] = value
                self.execute()
                self.assert_posts(0)

    def test_closed_or_mismatched_pr_skips(self):
        """The PR must be open and belong to this repository with the requested number."""
        changes = (
            {"state": "closed"},
            {"number": 18},
            {"base": {"repo": {"full_name": "different/project"}}},
            {"base": {"repo": None}},
        )
        for change in changes:
            with self.subTest(change=change):
                self.setUp()
                self.pr.update(change)
                self.execute()
                self.assert_posts(0)

    def test_wrong_or_missing_identity_fails(self):
        """A token for another account or an unverifiable account cannot post."""
        for identity in (
            {"login": "github-actions[bot]", "id": 123},
            {"login": "another-user", "id": 123},
            {"login": BOT},
            {"login": BOT, "id": True},
            {"login": BOT, "id": 0},
        ):
            with self.subTest(identity=identity):
                self.setUp()
                self.identity = identity
                with self.assertRaisesRegex(RuntimeError, "must belong"):
                    self.execute()
                self.assert_posts(0)

    def test_missing_token_fails_without_fallback(self):
        """The Actions token cannot silently replace the designated bot."""
        self.environment.pop("BOT_PAT")
        self.environment["GITHUB_TOKEN"] = "other-token"
        with self.assertRaisesRegex(RuntimeError, "Configure BOT_PAT"):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_existing_request_on_later_page_skips(self):
        """Pagination finds a matching request beyond the first hundred comments."""
        unrelated = {"user": {"id": 999, "login": "contributor"}, "body": "hello"}
        self.pages = [[unrelated] * 100, [{"user": self.identity, "body": BODY}]]
        self.execute()
        self.assert_posts(0)

    def test_spoofed_marker_does_not_suppress_request(self):
        """Both stable account ID and verified login must match, with the exact body."""
        self.pages = [
            [
                {"user": {"login": "contributor", "id": 999}, "body": BODY},
                {"user": {"login": BOT, "id": 999}, "body": BODY},
                {"user": {"login": "contributor", "id": 123}, "body": BODY},
                {"user": self.identity, "body": "An unrelated comment"},
            ]
        ]
        self.execute()
        self.assert_posts(1)

    def test_eligibility_change_before_write_skips(self):
        """The final read prevents comments after privacy, stars or PR state changes."""
        for change in ("private", "stars", "closed", "base"):
            with self.subTest(change=change):
                self.setUp()
                if change == "private":
                    self.current_repository["private"] = True
                elif change == "stars":
                    self.current_repository["stargazers_count"] = 10
                elif change == "closed":
                    self.current_pr["state"] = "closed"
                else:
                    self.current_pr["base"]["repo"]["full_name"] = "different/project"
                self.execute()
                self.assert_posts(0)

    def test_manual_dispatch_and_repeat_are_idempotent(self):
        """Recovery uses the same eligibility, identity and existing-comment checks."""
        self.environment.update(
            GITHUB_EVENT_NAME="workflow_dispatch",
            EXPLICIT_PR_NUMBER="17",
            EVENT_PR_NUMBER="",
            EVENT_ACTION="",
        )
        self.execute()
        self.assert_posts(1)
        self.pages = [[{"user": self.identity, "body": BODY}]]
        self.execute()
        self.assert_posts(1)

    def test_invalid_event_or_number_fails_before_api(self):
        """Ordinary pull_request, sync events and untrusted identifiers are rejected."""
        for change in (
            {"GITHUB_EVENT_NAME": "pull_request"},
            {"GITHUB_EVENT_NAME": "push"},
            {"EVENT_ACTION": "synchronize"},
            {"EXPLICIT_PR_NUMBER": "17"},
            {"EVENT_PR_NUMBER": "18"},
            {"GITHUB_EVENT_NAME": "workflow_dispatch", "EXPLICIT_PR_NUMBER": ""},
            {"PR_NUMBER": "0", "EVENT_PR_NUMBER": "0"},
            {"PR_NUMBER": "01", "EVENT_PR_NUMBER": "01"},
            {"PR_NUMBER": "17/../18", "EVENT_PR_NUMBER": "17/../18"},
        ):
            with self.subTest(change=change):
                self.setUp()
                self.environment.update(change)
                with self.assertRaisesRegex(RuntimeError, "newly opened"):
                    self.execute()
                self.assertEqual(self.calls, [])

    def test_ambiguous_post_is_not_retried_and_redacts_token(self):
        """A failed write is surfaced once without exposing its credential."""
        self.fail_post = True
        with self.assertRaisesRegex(
            RuntimeError, r"ambiguous API failure: \*\*\*"
        ) as error:
            self.execute()
        self.assertNotIn("test-bot-token", str(error.exception))
        self.assert_posts(1)

    def test_workflow_and_callers_preserve_trust_boundary(self):
        """No checkout or token inheritance; manual and opened events share one lock."""
        self.assertEqual(self.workflow["permissions"], {})
        self.assertIn("vars.CI_RUNNER_AUTOMATION_LABELS", self.job["runs-on"])
        self.assertEqual(self.job["timeout-minutes"], "5")
        self.assertEqual(len(self.job["steps"]), 1)
        self.assertNotIn("uses", self.job["steps"][0])
        self.assertEqual(self.job["steps"][0]["shell"], "python3 -I {0}")
        concurrency = self.job["concurrency"]
        self.assertEqual(concurrency["cancel-in-progress"], "false")
        self.assertIn("github.repository", concurrency["group"])
        self.assertIn("inputs.pull-request-number", concurrency["group"])
        self.assertIn("github.event.pull_request.number", concurrency["group"])
        self.assertNotIn("${{", self.code)
        for path in (
            ROOT / ".github/workflows/coderabbit-review.yml",
            ROOT / "docs/examples/coderabbit-review.yml",
        ):
            with self.subTest(path=path):
                caller = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
                self.assertEqual(caller["permissions"], {})
                self.assertEqual(
                    set(caller["on"]), {"pull_request_target", "workflow_dispatch"}
                )
                self.assertEqual(
                    caller["on"]["pull_request_target"]["types"], ["opened"]
                )
                job = caller["jobs"]["request-review"]
                self.assertEqual(job["if"], "github.event.repository.private == false")
                self.assertEqual(job["secrets"], {"BOT_PAT": "${{ secrets.BOT_PAT }}"})
                self.assertNotIn("concurrency", caller)
                self.assertNotIn("concurrency", job)


if __name__ == "__main__":
    unittest.main()
