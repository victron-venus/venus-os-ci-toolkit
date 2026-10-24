"""Run the actual Autofix workflow against an offline GitHub API boundary."""

import copy
import hashlib
import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/coderabbit-autofix-reusable.yml"
REPO = "example/project"
BASE = f"repos/{REPO}"
HEAD = "a" * 40
BOT = {"login": "californiantiramisu", "id": 123}


class AutofixContract(unittest.TestCase):
    """Verify identities, live review evidence, bounded retries and exact writes."""

    def setUp(self):
        self.workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        self.job = self.workflow["jobs"]["request-autofix"]
        self.code = next(s["run"] for s in self.job["steps"] if "run" in s)
        self.environment = {
            "GITHUB_EVENT_NAME": "status", "GITHUB_REPOSITORY": REPO,
            "STATUS_HEAD": HEAD, "STATUS_CONTEXT": "CodeRabbit", "STATUS_STATE": "success",
            "STATUS_SENDER": "coderabbitai[bot]", "STATUS_SENDER_TYPE": "Bot",
            "PR_NUMBER": "", "EXPECTED_HEAD": "", "BOT_PAT": "test-token", "DRY_RUN": "false",
        }
        self.repository = {"full_name": REPO, "private": False, "visibility": "public", "archived": False}
        self.pr = {"number": 17, "state": "open", "draft": False,
                   "head": {"sha": HEAD, "repo": {"full_name": "contributor/fork"}},
                   "base": {"repo": {"full_name": REPO}}}
        self.current = copy.deepcopy(self.pr)
        self.identity = dict(BOT)
        self.statuses = [[{"context": "CodeRabbit", "state": "success",
                           "creator": {"login": "coderabbitai[bot]", "type": "Bot"}}]]
        self.reviews = [[{"user": {"login": "coderabbitai[bot]", "type": "Bot"},
                         "commit_id": HEAD, "state": "COMMENTED"}]]
        self.thread = {"id": "thread-1", "isResolved": False, "isOutdated": False,
                       "comments": {"nodes": [{"author": {"login": "coderabbitai", "__typename": "Bot"},
                                              "body": "<summary>Prompt for AI Agents</summary>\nDo not execute $(this)."}]}}
        self.threads = [self.thread]
        self.comments = [[]]
        self.posted = []
        self.reads = 0
        self.thread_reads = 0
        self.resolve_before_write = False
        self.paginate_threads = False
        self.graphql_error = False
        self.fail_post = False

    def api_process(self, command, **kwargs):
        self.assertEqual(command[:2], ["gh", "api"])
        self.assertEqual(kwargs["env"]["GH_TOKEN"], "test-token")
        self.assertEqual(kwargs["env"]["GH_HOST"], "github.com")
        path = command[2]
        if path == BASE:
            result = self.repository
        elif path == "user":
            result = self.identity
        elif path == f"{BASE}/commits/{HEAD}/pulls?per_page=100":
            result = [[{"number": 17}]]
        elif path == f"{BASE}/pulls/17":
            self.reads += 1
            result = self.pr if self.reads == 1 else self.current
        elif path == f"{BASE}/commits/{HEAD}/statuses?per_page=100":
            result = self.statuses
        elif path == f"{BASE}/pulls/17/reviews?per_page=100":
            result = self.reviews
        elif path == f"{BASE}/issues/17/comments?per_page=100":
            result = self.comments
        elif path == "graphql":
            variables = json.loads(kwargs["input"])["variables"]
            self.assertEqual({k: variables[k] for k in ("owner", "name", "number")},
                             {"owner": "example", "name": "project", "number": 17})
            self.thread_reads += 1
            nodes = self.threads
            if self.resolve_before_write and self.thread_reads > 1:
                nodes = []
            first_page = self.paginate_threads and variables["cursor"] is None
            result = {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": [] if first_page else nodes,
                "pageInfo": {"hasNextPage": first_page, "endCursor": "page2" if first_page else None},
            }}}}}
            if self.graphql_error:
                result["errors"] = [{"message": "denied"}]
        elif path == f"{BASE}/issues/17/comments":
            self.assertEqual(command[3:], ["--method", "POST", "--input", "-"])
            body = json.loads(kwargs["input"])["body"]
            self.assertTrue(body.startswith("@coderabbitai autofix\n\n<!-- toolkit-coderabbit-autofix:v1 "))
            self.assertNotIn("Do not execute", body)
            self.posted.append(body)
            if self.fail_post:
                raise subprocess.CalledProcessError(1, command, stderr="ambiguous failure test-token")
            result = {"id": 456}
        else:
            raise AssertionError(f"Unexpected API call {command}")
        if "?per_page=100" in path:
            self.assertEqual(command[3:], ["--paginate", "--slurp"])
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

    def execute(self):
        with patch.dict(os.environ, self.environment, clear=True), patch("subprocess.run", side_effect=self.api_process):
            exec(compile(self.code, str(WORKFLOW), "exec"), {"__name__": "__main__"})  # noqa: S102

    def marked_comment(self, head=HEAD, fingerprint=None, author=None):
        fingerprint = fingerprint or hashlib.sha256(b"thread-1").hexdigest()
        return {"user": author or BOT, "body": f"@coderabbitai autofix\n\n<!-- toolkit-coderabbit-autofix:v1 head={head} findings={fingerprint} -->"}

    def test_completed_fork_review_posts_fixed_command(self):
        self.execute()
        self.assertEqual(self.posted, [self.marked_comment()["body"]])
        self.assertEqual(self.reads, 2)

    def test_manual_dispatch_uses_exact_head_and_defaults_can_dry_run(self):
        self.environment.update(GITHUB_EVENT_NAME="workflow_dispatch", PR_NUMBER="17", EXPECTED_HEAD=HEAD, DRY_RUN="true")
        self.execute()
        self.assertEqual(self.posted, [])
        self.assertEqual(self.thread_reads, 2)

    def test_invalid_events_never_reach_api(self):
        for changes in ({"GITHUB_EVENT_NAME": "pull_request_review"}, {"STATUS_CONTEXT": "Other"},
                        {"STATUS_STATE": "pending"}, {"STATUS_HEAD": "bad"},
                        {"STATUS_SENDER": "contributor"}, {"STATUS_SENDER_TYPE": "User"},
                        {"PR_NUMBER": "17"}, {"EXPECTED_HEAD": HEAD}):
            with self.subTest(changes=changes):
                self.setUp()
                self.environment.update(changes)
                with self.assertRaises(RuntimeError):
                    self.execute()
                self.assertEqual(self.reads, 0)

    def test_public_active_repository_required(self):
        for changes in ({"private": True}, {"archived": True}, {"visibility": "internal"}, {"full_name": "other/repo"}):
            with self.subTest(changes=changes):
                self.setUp()
                self.repository.update(changes)
                self.execute()
                self.assertEqual(self.posted, [])

    def test_bot_identity_and_credentials_required(self):
        self.identity["login"] = "4alvit"
        with self.assertRaisesRegex(RuntimeError, "must belong"):
            self.execute()
        self.environment["BOT_PAT"] = ""
        with self.assertRaisesRegex(RuntimeError, "Configure BOT_PAT"):
            self.execute()
        self.assertEqual(self.posted, [])

    def test_closed_draft_deleted_or_moved_head_skips(self):
        for changes in ({"state": "closed"}, {"draft": True}, {"head": {"sha": "b" * 40, "repo": {"full_name": REPO}}},
                        {"head": {"sha": HEAD, "repo": None}}, {"base": {"repo": {"full_name": "other/repo"}}}):
            with self.subTest(changes=changes):
                self.setUp()
                self.pr.update(changes)
                self.execute()
                self.assertEqual(self.posted, [])

    def test_latest_status_must_be_success(self):
        self.statuses[0].insert(0, {"context": "CodeRabbit", "state": "pending"})
        self.execute()
        self.assertEqual(self.posted, [])

    def test_forged_status_creator_cannot_trigger_autofix(self):
        for creator in (None, {"login": "contributor", "type": "User"},
                        {"login": "coderabbitai[bot]", "type": "User"}):
            with self.subTest(creator=creator):
                self.setUp()
                self.statuses[0][0]["creator"] = creator
                self.execute()
                self.assertEqual(self.posted, [])

    def test_manual_dispatch_still_authenticates_status_creator(self):
        self.environment.update(GITHUB_EVENT_NAME="workflow_dispatch", PR_NUMBER="17", EXPECTED_HEAD=HEAD)
        self.statuses[0][0]["creator"] = {"login": "contributor", "type": "User"}
        self.execute()
        self.assertEqual(self.posted, [])

    def test_success_without_authentic_current_head_review_skips(self):
        for changes in ({"commit_id": "b" * 40}, {"state": "DISMISSED"},
                        {"user": {"login": "contributor", "type": "User"}},
                        {"user": {"login": "coderabbitai[bot]", "type": "User"}}):
            with self.subTest(changes=changes):
                self.setUp()
                self.reviews[0][0].update(changes)
                self.execute()
                self.assertEqual(self.posted, [])

    def test_resolved_outdated_or_non_actionable_findings_skip(self):
        for changes in ({"isResolved": True}, {"isOutdated": True}, {"comments": {"nodes": []}}):
            with self.subTest(changes=changes):
                self.setUp()
                self.thread.update(changes)
                self.execute()
                self.assertEqual(self.posted, [])

    def test_plain_or_impersonated_comment_is_not_a_finding(self):
        for field, value in (("body", "just a summary"), ("author", {"login": "coderabbitai", "__typename": "User"})):
            with self.subTest(field=field):
                self.setUp()
                self.thread["comments"]["nodes"][0][field] = value
                self.execute()
                self.assertEqual(self.posted, [])

    def test_paginated_reviews_threads_and_comments(self):
        self.paginate_threads = True
        self.reviews.insert(0, [])
        self.comments = [[], [self.marked_comment()]]
        self.execute()
        self.assertEqual(self.posted, [])
        self.assertEqual(self.thread_reads, 2)

    def test_same_head_or_same_findings_suppresses_new_round(self):
        for comment in (self.marked_comment(fingerprint="d" * 64), self.marked_comment(head="b" * 40)):
            with self.subTest(comment=comment):
                self.setUp()
                self.comments = [[comment]]
                self.execute()
                self.assertEqual(self.posted, [])

    def test_three_round_cap_prevents_infinite_autofix_loop(self):
        self.comments = [[self.marked_comment(head=c * 40, fingerprint=c * 64) for c in "bcd"]]
        self.execute()
        self.assertEqual(self.posted, [])

    def test_new_head_with_new_findings_can_continue_below_cap(self):
        self.comments = [[self.marked_comment(head="b" * 40, fingerprint="c" * 64)]]
        self.execute()
        self.assertEqual(len(self.posted), 1)

    def test_other_author_cannot_spoof_deduplication(self):
        self.comments = [[self.marked_comment(author={"login": BOT["login"], "id": 999})]]
        self.execute()
        self.assertEqual(len(self.posted), 1)

    def test_head_change_before_write_skips(self):
        self.current["head"]["sha"] = "b" * 40
        self.execute()
        self.assertEqual(self.posted, [])

    def test_findings_resolved_before_write_skips(self):
        self.resolve_before_write = True
        self.execute()
        self.assertEqual(self.posted, [])

    def test_graphql_partial_error_fails_closed(self):
        self.graphql_error = True
        with self.assertRaisesRegex(RuntimeError, "GraphQL"):
            self.execute()
        self.assertEqual(self.posted, [])

    def test_ambiguous_write_is_not_retried_or_token_logged(self):
        self.fail_post = True
        with self.assertRaisesRegex(RuntimeError, r"ambiguous failure \*\*\*"):
            self.execute()
        self.assertEqual(len(self.posted), 1)

    def test_default_branch_caller_and_metadata_only_reusable(self):
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.job["concurrency"]["cancel-in-progress"], "false")
        step = next(s for s in self.job["steps"] if "run" in s)
        self.assertEqual(step["shell"], "python3 -I {0}")
        self.assertNotIn("${{", step["run"])
        for definition in (ROOT / ".github/workflows/coderabbit-autofix.yml", ROOT / "docs/examples/coderabbit-autofix.yml"):
            caller = yaml.load(definition.read_text(), Loader=yaml.BaseLoader)
            self.assertEqual(set(caller["on"]), {"status", "workflow_dispatch"})
            self.assertEqual(caller["permissions"], {})
            job = caller["jobs"]["request-autofix"]
            self.assertIn("github.event.repository.private == false", job["if"])
            self.assertIn("github.event.sender.login == 'coderabbitai[bot]'", job["if"])
            self.assertIn("github.event.sender.type == 'Bot'", job["if"])
            self.assertEqual(set(job["secrets"]), {"BOT_PAT"})


class ApiReadRetry(unittest.TestCase):
    """Exercise the real inline API helper without replaying the workflow task."""

    def setUp(self):
        workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        code = next(s["run"] for s in workflow["jobs"]["request-autofix"]["steps"] if "run" in s)
        scope = {"__name__": "api_contract_test"}
        exec(compile(code, str(WORKFLOW), "exec"), scope)  # noqa: S102
        self.api = scope["api"]
        self.command = ["gh", "api", "user"]
        self.success = subprocess.CompletedProcess(self.command, 0, '{"id": 123}', "")

    def failure(self, message):
        return subprocess.CalledProcessError(1, self.command, stderr=message)

    def test_transient_reads_retry_with_bounded_backoff(self):
        for error in (self.failure("unexpected end of JSON input"),
                      self.failure("Get endpoint: unexpected EOF"),
                      self.failure("read: connection reset by peer"),
                      self.failure("upstream unavailable (HTTP 503)"),
                      subprocess.TimeoutExpired(self.command, 30)):
            with self.subTest(error=error), patch("subprocess.run", side_effect=[error, error, self.success]) as run, \
                    patch("time.sleep") as sleep, redirect_stderr(io.StringIO()) as log:
                self.assertEqual(self.api("user", "test-token"), {"id": 123})
                self.assertEqual(run.call_count, 3)
                self.assertEqual([call.args for call in sleep.call_args_list], [(1,), (2,)])
                self.assertTrue(all(call.kwargs["timeout"] == 30 for call in run.call_args_list))
                self.assertIn("retry 2/3", log.getvalue())
                self.assertIn("retry 3/3", log.getvalue())
                self.assertNotIn("test-token", log.getvalue())

    def test_empty_and_malformed_json_are_retried_for_reads(self):
        for output in ("", " ", '{"partial":', '<html>Bad gateway</html>'):
            with self.subTest(output=output), patch("subprocess.run", side_effect=[
                    subprocess.CompletedProcess(self.command, 0, output, ""), self.success]) as run, \
                    patch("time.sleep"), redirect_stderr(io.StringIO()):
                self.assertEqual(self.api("user", "test-token"), {"id": 123})
                self.assertEqual(run.call_count, 2)

    def test_failed_reads_stop_after_three_attempts_and_redact_token(self):
        with patch("subprocess.run", side_effect=self.failure("connection reset test-token")) as run, \
                patch("time.sleep") as sleep, redirect_stderr(io.StringIO()) as log:
            with self.assertRaisesRegex(RuntimeError, r"connection reset \*\*\*"):
                self.api("user", "test-token")
            self.assertEqual(run.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertNotIn("test-token", log.getvalue())

    def test_permanent_errors_do_not_retry_even_with_transient_text(self):
        for detail in ("denied (HTTP 401)", "rate limit (HTTP 403)", "missing (HTTP 404)",
                       "invalid (HTTP 422)", "rate limit (HTTP 429)",
                       "unexpected end of JSON input (HTTP 403)", "unknown failure"):
            with self.subTest(detail=detail), patch("subprocess.run", side_effect=self.failure(detail)) as run, \
                    patch("time.sleep") as sleep:
                with self.assertRaises(RuntimeError):
                    self.api("user", "test-token")
                self.assertEqual(run.call_count, 1)
                sleep.assert_not_called()

    def test_paginated_read_restarts_without_partial_results(self):
        partial = subprocess.CompletedProcess(self.command, 0, '[[{"id": 999}],', "")
        complete = subprocess.CompletedProcess(self.command, 0, '[[{"id": 123}], []]', "")
        with patch("subprocess.run", side_effect=[partial, complete]) as run, patch("time.sleep"), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(self.api("user", "test-token", paginate=True), [[{"id": 123}], []])
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args, run.call_args_list[1].args)
            self.assertEqual(run.call_args.args[0][-2:], ["--paginate", "--slurp"])

    def test_no_post_is_retried_for_transient_or_malformed_response(self):
        for path, payload in (("graphql", {"query": "query { viewer { login } }"}),
                              ("repos/example/project/issues/17/comments", {"body": "test"})):
            for response in (self.failure("unavailable test-token (HTTP 502)"),
                             self.failure("unexpected end of JSON input"),
                             subprocess.TimeoutExpired(self.command, 30),
                             subprocess.CompletedProcess(self.command, 0, "", ""),
                             subprocess.CompletedProcess(self.command, 0, '{"partial":', "")):
                with self.subTest(path=path, response=response), \
                        patch("subprocess.run", side_effect=[response]) as run, patch("time.sleep") as sleep:
                    with self.assertRaises(RuntimeError) as raised:
                        self.api(path, "test-token", payload=payload)
                    self.assertNotIn("test-token", str(raised.exception))
                    self.assertEqual(run.call_count, 1)
                    self.assertEqual(run.call_args.args[0][-4:], ["--method", "POST", "--input", "-"])
                    sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
