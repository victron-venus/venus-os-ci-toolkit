"""Exercise the reusable workflow's actual embedded implementation without GitHub calls."""

import ast
import copy
import json
import os
import re
import tempfile
import textwrap
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/auto-merge.yml"
SOURCE = textwrap.dedent(
    WORKFLOW.read_text()
    .split("python3 -I - <<'PY'\n", 1)[1]
    .rsplit("\n          PY", 1)[0]
)


def own_check():
    """Represent the currently registered merger waiting on other checks."""
    return {
        "name": "Auto Merge",
        "workflowName": "Auto Merge PRs",
        "status": "IN_PROGRESS",
        "startedAt": "2026-09-12T01:23:55Z",
        "detailsUrl": "https://github.com/example/repo/actions/runs/123/job/4",
    }


def pr(author="californiantiramisu", head="abc", state="SUCCESS"):
    """Create a PR response containing a genuine independent check."""
    return {
        "number": 7,
        "headRefName": "renovate/ci-workflows",
        "headRepository": {"id": "R_example", "nameWithOwner": "example/repo"},
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": author},
        "baseRefName": "main",
        "baseRefOid": "base",
        "isCrossRepository": False,
        "labels": [{"name": "automerge"}],
        "headRefOid": head,
        "statusCheckRollup": [
            {"name": "CI", "status": "COMPLETED", "conclusion": state},
            own_check(),
        ],
    }


def attempt(name, workflow, run, started, conclusion):
    """Model the timestamped GitHub attempts observed on bootstrap PR 23."""
    return {
        "name": name,
        "workflowName": workflow,
        "detailsUrl": f"https://github.com/example/repo/actions/runs/{run}/job/4",
        "startedAt": f"2026-09-12T01:23:{started}Z",
        "status": "COMPLETED" if conclusion else "IN_PROGRESS",
        "conclusion": conclusion,
    }


class AutoMergeTests(unittest.TestCase):
    """Verify identity, real checks, and mutation race protections."""

    def setUp(self):
        """Load trusted workflow definitions; API transport is mocked before main runs."""
        self.code = {"__name__": "workflow_under_test"}
        # Load repository-owned definitions; main runs only after API mocks are installed.
        # pylint: disable-next=exec-used
        exec(compile(SOURCE, str(WORKFLOW), "exec"), self.code)  # noqa: S102
        # Existing decision/race cases mock this new account metadata boundary;
        # MergeAuthorIdentityTests separately execute the actual identity reader.
        self.real_merge_author_email = self.code["merge_author_email"]
        self.code["merge_author_email"] = unittest.mock.Mock(
            return_value="123456+merge-account@users.noreply.github.com"
        )
        self.environment = {
            "GH_TOKEN": "test-token",
            "GITHUB_REPOSITORY": "example/repo",
            "PR_NUMBER": "7",
            "PR_AUTHORS": "4alvit,californiantiramisu",
            "MERGE_METHOD": "squash",
            "REQUIRED_CHECKS": "",
            "GITHUB_RUN_ID": "123",
            "GITHUB_ACTOR": "unrelated-event-actor",
        }

    def run_workflow(self, snapshots, environment=None):
        """Run the actual main function with only transport and clock replaced."""
        env = self.environment | (environment or {})
        with (
            patch.dict(os.environ, env),
            patch.dict(self.code),
            patch.dict(
                self.code,
                {
                    "snapshot": unittest.mock.Mock(side_effect=snapshots),
                    "gh": unittest.mock.Mock(side_effect=lambda *args: "true" if args[-1] == ".allow_auto_merge" else "main"),
                },
            ),
        ):
            transport = self.code["gh"]
            with patch("time.sleep"), patch("time.monotonic", return_value=0):
                self.code["main"]()
            return transport.call_args_list

    def test_actual_author_not_event_actor_controls_merge(self):
        """A trusted author may merge even when an unrelated actor triggers the event."""
        calls = self.run_workflow([pr(), pr(), pr()])
        self.assertIn("--auto", calls[-1].args)
        self.assertEqual(calls[-1].args[-2:], ("--match-head-commit", "abc"))
        self.assertNotIn("--admin", calls[-1].args)

    def test_untrusted_author_never_merges(self):
        """A trusted event actor cannot make an untrusted PR author eligible."""
        calls = self.run_workflow([pr("stranger")], {"GITHUB_ACTOR": "4alvit"})
        self.assertEqual(len(calls), 1)

    def test_draft_label_and_target_gate(self):
        """Drafts, wrong labels, wrong targets, and closed PRs never request merge."""
        for changes in (
            {"isDraft": True},
            {"labels": [{"name": "auto-merge"}]},
            {"baseRefName": "other"},
            {"state": "CLOSED"},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(len(self.run_workflow([pr() | changes])), 1)

    def test_pending_waits_until_stable_success(self):
        """Pending checks require subsequent stable green observations."""
        pending = pr(state="")
        pending["statusCheckRollup"][0]["status"] = "IN_PROGRESS"
        calls = self.run_workflow([pending, pr(), pr(), pr()])
        self.assertEqual(calls[-1].args[0:2], ("pr", "merge"))

    def test_failed_check_blocks_mutation(self):
        """Failed or cancelled checks stop the workflow before a merge request."""
        for state in ["FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "ERROR"]:
            pull_request = pr(state=state)
            with (
                self.subTest(state=state),
                self.assertRaisesRegex(RuntimeError, "check failed"),
            ):
                self.run_workflow([pull_request])

    def test_changed_head_is_revalidated(self):
        """A concurrent push requires fresh validation and binds merge to the new head."""
        calls = self.run_workflow(
            [pr(), pr(), pr(head="new"), pr(head="new"), pr(head="new"), pr(head="new")]
        )
        self.assertEqual(calls[-1].args[-1], "new")

    def test_label_removed_before_mutation_stops_merge(self):
        """Removing authorization before the final read prevents merge."""
        changed = pr() | {"labels": []}
        self.assertFalse(any(call.args[:2] == ("pr", "merge") for call in self.run_workflow([pr(), pr(), changed, changed])))

    def test_missing_token_is_actionable(self):
        """An absent BOT_PAT raises a configuration error before API access."""
        with self.assertRaisesRegex(RuntimeError, "BOT_PAT is missing"):
            self.run_workflow([], {"GH_TOKEN": ""})

    def test_required_names_and_empty_checks_wait(self):
        """Missing explicit checks and empty rollups cannot qualify as green."""
        check = self.code["check_state"]
        self.assertFalse(check(pr(), {"other"}, "123")[0])
        self.assertTrue(check(pr(), {"CI"}, "123")[0])
        self.assertFalse(check(pr() | {"statusCheckRollup": []}, set(), "123")[0])

    def test_only_own_waiting_check_is_ignored(self):
        """Only the waiting job from this run is omitted from the check gate."""
        response = pr()
        own = {
            "name": "Auto Merge",
            "status": "IN_PROGRESS",
            "detailsUrl": "https://github.com/example/repo/actions/runs/123/job/4",
        }
        response["statusCheckRollup"].append(own)
        self.assertTrue(self.code["check_state"](response, set(), "123")[0])
        other = copy.deepcopy(response)
        other["statusCheckRollup"][-1]["detailsUrl"] = own["detailsUrl"].replace(
            "/123/", "/999/"
        )
        self.assertFalse(self.code["check_state"](other, set(), "123")[0])

    def test_legacy_status_context_and_neutral_checks(self):
        """Legacy statuses participate while neutral and skipped checks remain valid."""
        response = pr()
        response["statusCheckRollup"] = [
            own_check(),
            {"context": "external-status", "state": "SUCCESS"},
            {"name": "optional", "status": "COMPLETED", "conclusion": "NEUTRAL"},
            {"name": "filtered", "status": "COMPLETED", "conclusion": "SKIPPED"},
        ]
        self.assertTrue(
            self.code["check_state"](response, {"external-status"}, "123")[0]
        )
        response["statusCheckRollup"][1]["state"] = "PENDING"
        self.assertFalse(self.code["check_state"](response, set(), "123")[0])

    def test_wait_expires_with_actionable_error(self):
        """A long-running pending check eventually produces a visible timeout."""
        with (
            patch.dict(os.environ, self.environment),
            patch.dict(
                self.code,
                {
                    "gh": unittest.mock.Mock(side_effect=lambda *args: "true" if args[-1] == ".allow_auto_merge" else "main"),
                },
            ),
            patch("time.monotonic", side_effect=[0, 7201]),
            self.assertRaisesRegex(RuntimeError, "120 minutes"),
        ):
            self.code["main"]()

    def test_superseded_opened_attempts_do_not_poison_labeled_run(self):
        """PR 23's canceled opened jobs yield to the successful labeled attempts."""
        response = pr()
        response["statusCheckRollup"] = [
            attempt("auto-merge / Auto Merge", "Auto Merge PRs", 34664693259, "55", ""),
            attempt(
                "auto-approve / Auto Approve",
                "Auto Approve PRs",
                34664692972,
                "52",
                "CANCELLED",
            ),
            attempt(
                "auto-merge / Auto Merge",
                "Auto Merge PRs",
                34664692971,
                "52",
                "CANCELLED",
            ),
            attempt(
                "auto-approve / Auto Approve",
                "Auto Approve PRs",
                34664693242,
                "56",
                "SUCCESS",
            ),
            attempt("Workflow contracts", "CodeQL", 34664693056, "54", "SUCCESS"),
        ]
        self.assertTrue(self.code["check_state"](response, set(), "34664693259")[0])

    def test_newer_pending_attempt_does_not_inherit_old_result(self):
        """A newly queued run wins despite a missing start time and still blocks merge."""
        response = pr()
        response["statusCheckRollup"] += [
            attempt("Build", "Runtime CI", 100, "54", "FAILURE"),
            attempt("Build", "Runtime CI", 101, "54", ""),
        ]
        response["statusCheckRollup"][-1]["startedAt"] = ""
        response["statusCheckRollup"][-1]["status"] = "QUEUED"
        self.assertFalse(self.code["check_state"](response, set(), "123")[0])

    def test_waits_for_current_merger_context_registration(self):
        """An absent current context is a safe wait, never a merge or stale failure."""
        response = pr()
        response["statusCheckRollup"] = [
            attempt("auto-merge / Auto Merge", "Auto Merge PRs", 100, "52", "FAILURE"),
            attempt("CI", "Runtime CI", 101, "54", "SUCCESS"),
        ]
        self.assertFalse(self.code["check_state"](response, set(), "123")[0])
        response["statusCheckRollup"].append(
            attempt("auto-merge / Auto Merge", "Auto Merge PRs", 123, "55", "")
        )
        self.assertTrue(self.code["check_state"](response, set(), "123")[0])

    def test_newer_skipped_duplicate_event_preserves_registered_self(self):
        """The other event's skipped merger cannot hide this run's registered job."""
        response = pr()
        response["statusCheckRollup"].append(
            attempt("Auto Merge", "Auto Merge PRs", 124, "56", "SKIPPED")
        )
        self.assertTrue(self.code["check_state"](response, set(), "123")[0])
        response["statusCheckRollup"].append(
            attempt("Auto Merge", "Independent workflow", 125, "57", "FAILURE")
        )
        with self.assertRaisesRegex(RuntimeError, "Independent workflow / Auto Merge"):
            self.code["check_state"](response, set(), "123")

    def test_cannot_expand_trusted_author_allowlist(self):
        """Caller inputs cannot authorize an arbitrary additional account."""
        with self.assertRaisesRegex(RuntimeError, "trusted PR authors"):
            self.run_workflow([], {"PR_AUTHORS": "stranger"})

    def test_api_failure_is_not_silently_accepted(self):
        """API failures propagate instead of reporting successful automation."""
        failure = RuntimeError("API unavailable")
        with self.assertRaisesRegex(RuntimeError, "API unavailable"):
            self.run_workflow([failure])


def existing_request():
    """A pending request predating the label-removal event."""
    return {"enabledAt": "2026-10-31T12:00:00Z"}


def removal_event():
    """A GitHub webhook for removing this PR's automation opt-in."""
    return {
        "action": "unlabeled",
        "label": {"name": "automerge"},
        "repository": {"full_name": "example/repo"},
        "number": 7,
        "pull_request": {
            "head": {"sha": "abc", "ref": "renovate/ci-workflows",
                     "repo": {"full_name": "example/repo", "node_id": "R_example"}},
            "base": {"sha": "base", "ref": "main", "repo": {"full_name": "example/repo"}},
            "user": {"login": "californiantiramisu"},
            "updated_at": "2026-10-31T12:01:00Z",
        },
    }


@contextmanager
def event_environment(event):
    """Expose one synthetic local payload, without any network or credential use."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "event.json"
        path.write_text(json.dumps(event))
        yield {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(path)}


class WithdrawalTests(unittest.TestCase):
    """Execute targeted revocation using fresh metadata, without GitHub writes."""

    setUp = AutoMergeTests.setUp

    def run_workflow(self, snapshots, event=None):
        """Supply a real temporary webhook payload while keeping the API mocked."""
        with event_environment(
            removal_event() if event is None else event
        ) as environment:
            return AutoMergeTests.run_workflow(self, snapshots, environment)

    @staticmethod
    def withdrawn(**changes):
        """Represent an existing native request whose label was removed."""
        return pr() | {"labels": [], "autoMergeRequest": existing_request()} | changes

    def test_default_path_revokes_each_withdrawal_after_revalidation(self):
        """Labels, drafts and trusted same-repo base changes all revoke old intent."""
        for change in ({"labels": []}, {"isDraft": True}, {"baseRefName": "other"}):
            withdrawn = pr() | {"autoMergeRequest": existing_request()} | change
            with self.subTest(change=change):
                event = removal_event()
                event["pull_request"]["base"]["ref"] = withdrawn["baseRefName"]
                if change.get("isDraft"):
                    event["action"] = "converted_to_draft"
                elif change.get("baseRefName"):
                    event["action"] = "edited"
                    event["changes"] = {"base": {"ref": {"from": "main"}}}
                calls = self.run_workflow([withdrawn, copy.deepcopy(withdrawn)], event=event)
                self.assertEqual(
                    calls[-1].args,
                    ("pr", "merge", "7", "--repo", "example/repo", "--disable-auto"),
                )
                self.assertFalse(any("--auto" in call.args for call in calls))

    def test_withdrawal_at_final_observation_revokes_before_check_evaluation(self):
        """A failing or incomplete check cannot preserve an explicitly withdrawn request."""
        withdrawn = self.withdrawn(statusCheckRollup=[])
        opted_in = pr() | {"autoMergeRequest": existing_request()}
        calls = self.run_workflow(
            [opted_in, opted_in, withdrawn, copy.deepcopy(withdrawn)],
            event=removal_event() | {"action": "reopened"},
        )
        self.assertIn("--disable-auto", calls[-1].args)
        self.assertFalse(any("--auto" in call.args for call in calls))

    def test_surviving_positive_waiter_preserves_a_new_manual_request(self):
        """Unique cleanup groups leave old waiters alive; they cannot claim a new request."""
        for original in (None, existing_request()):
            for changes in ({"isDraft": True}, {"baseRefName": "other"}, {"labels": []}):
                opted_in = pr(state="PENDING") | {"autoMergeRequest": original}
                newer = pr() | changes | {"autoMergeRequest": {"enabledAt": "2026-10-31T12:02:00Z"}}
                with self.subTest(original=original, changes=changes):
                    calls = self.run_workflow([opted_in, newer, newer],
                                              event=removal_event() | {"action": "opened"})
                    self.assertFalse(any("merge" in call.args for call in calls))

    def test_queued_positive_event_does_not_claim_a_labelled_manual_request(self):
        """An old positive run may start only after a later explicit manual request."""
        for action in ("opened", "labeled", "reopened", "edited"):
            for changes in ({"isDraft": True}, {"baseRefName": "other"}):
                value = pr() | changes | {"autoMergeRequest": existing_request()}
                with self.subTest(action=action, changes=changes):
                    calls = self.run_workflow([value, value], event=removal_event() | {"action": action})
                    self.assertFalse(any("merge" in call.args for call in calls))

    def test_return_to_default_while_draft_preserves_later_manual_request(self):
        """A positive base-return event cannot revive a previous draft withdrawal."""
        value = pr() | {"isDraft": True, "autoMergeRequest": {"enabledAt": "2026-10-31T12:00:30Z"}}
        event = removal_event() | {"action": "edited", "changes": {"base": {"ref": {"from": "other"}}}}
        # Draft conversion was earlier; this new return-to-default event is not withdrawal.
        calls = self.run_workflow([value, value], event=event)
        self.assertFalse(any("merge" in call.args for call in calls))

    def test_surviving_waiter_can_still_cancel_its_same_observed_request(self):
        """Request ownership is retained despite a separate cleanup event queue."""
        opted_in = pr(state="PENDING") | {"autoMergeRequest": existing_request()}
        withdrawn = opted_in | {"isDraft": True}
        calls = self.run_workflow([opted_in, withdrawn, withdrawn],
                                  event=removal_event() | {"action": "opened"})
        self.assertIn("--disable-auto", calls[-1].args)

    def test_new_intent_or_changed_identity_is_never_cancelled(self):
        """A different source ref, base branch, author or request belongs to a later event."""
        withdrawn = self.withdrawn()
        for change in (
            {"labels": [{"name": "automerge"}]},
            {"headRefName": "different-branch"},
            {"baseRefOid": None},
            {"baseRefName": "other"},
            {"isCrossRepository": True},
            {"author": {"login": "4alvit"}},
            {"autoMergeRequest": {"enabledAt": "new"}},
            {"autoMergeRequest": None},
            {"state": "CLOSED"},
        ):
            with self.subTest(change=change):
                calls = self.run_workflow([withdrawn, withdrawn | change])
                self.assertFalse(any("merge" in call.args for call in calls))

    def test_no_request_or_untrusted_target_does_not_trigger_cleanup(self):
        """Revocation is not a sweep of unrelated manually enabled requests."""
        for value in (
            self.withdrawn(autoMergeRequest=None),
            self.withdrawn(author={"login": "stranger"}),
        ):
            with self.subTest(value=value):
                calls = self.run_workflow([value])
                self.assertEqual(len(calls), 1)

    def test_revocation_read_failure_aborts_without_mutation(self):
        """Unavailable fresh metadata cannot authorize cancellation."""
        withdrawn = self.withdrawn()
        unavailable = RuntimeError("metadata unavailable")
        with self.assertRaisesRegex(RuntimeError, "metadata unavailable"):
            self.run_workflow([withdrawn, unavailable])

    def test_unrelated_event_preserves_manual_native_request(self):
        """No automation opt-in means unrelated events cannot revoke manual intent."""
        for event in (
            removal_event() | {"action": "reopened"},
            removal_event() | {"action": "labeled", "label": {"name": "bug"}},
            removal_event() | {"label": {"name": "bug"}},
            removal_event() | {"repository": {"full_name": "other/repo"}},
            removal_event() | {"number": 8},
        ):
            with self.subTest(event=event):
                self.assertEqual(len(self.run_workflow([self.withdrawn()], event)), 1)

    def test_default_cleanup_accepts_base_advance_for_the_same_request(self):
        """The ordinary evaluator also keeps an unchanged withdrawal actionable."""
        value = self.withdrawn()
        advanced = value | {"baseRefOid": "advanced-base"}
        calls = self.run_workflow([value, advanced])
        self.assertEqual(calls[-1].args, (
            "pr", "merge", "7", "--repo", "example/repo", "--disable-auto",
        ))

    def test_default_cleanup_requires_complete_matching_event_identity(self):
        """Reject withdrawal payloads that describe a different PR identity."""
        value = self.withdrawn()
        for field, replacement in (
            ("base", {"sha": None, "ref": "main", "repo": {"full_name": "example/repo"}}),
            ("base", {"sha": "base", "ref": "other", "repo": {"full_name": "example/repo"}}),
            ("head", {"sha": "abc", "repo": {"full_name": "contributor/fork"}}),
            ("user", {"login": "4alvit"}),
        ):
            event = removal_event()
            event["pull_request"][field] = replacement
            with self.subTest(field=field, replacement=replacement):
                self.assertEqual(len(self.run_workflow([value], event)), 1)

    def test_stale_removal_cannot_cancel_newer_or_ambiguous_request(self):
        """Rerun webhooks and missing timestamps cannot claim a later manual request."""
        for enabled in (
            "2026-10-31T12:02:00Z",
            "2026-10-31T12:01:00Z",
            "2026-10-31T12:00:00",
            None,
            "invalid",
        ):
            with self.subTest(enabled=enabled):
                value = self.withdrawn(autoMergeRequest={"enabledAt": enabled})
                with patch("builtins.print") as output:
                    self.assertEqual(len(self.run_workflow([value])), 1)
                self.assertTrue(
                    any(
                        "Preserved pending auto-merge" in str(call)
                        for call in output.call_args_list
                    )
                )
        event = removal_event()
        event["pull_request"]["head"]["ref"] = "other-branch"
        self.assertEqual(len(self.run_workflow([self.withdrawn()], event)), 1)


class CallerWithdrawalTests(unittest.TestCase):
    """Evaluate the real caller guard against bounded event fixtures."""

    def test_callers_limit_token_writes_to_pull_request_jobs(self):
        """Match the called workflow without granting repository-content writes."""
        root = WORKFLOW.parents[2]
        reusable = yaml.safe_load(WORKFLOW.read_text())
        required = reusable["jobs"]["auto-merge"]["permissions"]
        self.assertEqual(required, {"contents": "read", "pull-requests": "write"})
        for name in ("docs/examples/auto-merge.yml", ".github/workflows/auto-merge-prs.yml"):
            caller = yaml.safe_load((root / name).read_text())
            with self.subTest(caller=name):
                self.assertEqual(caller["permissions"], {"contents": "read"})
                self.assertTrue(caller["jobs"])
                for job in caller["jobs"].values():
                    self.assertEqual(job["permissions"], required)
                    self.assertEqual(job["secrets"]["BOT_PAT"], "${{ secrets.BOT_PAT }}")

    def reachable(self, job="auto-merge", **changes):
        """Translate only this trusted expression's booleans and fixed metadata keys."""
        path = WORKFLOW.parents[2] / "docs/examples/auto-merge.yml"
        workflow = yaml.safe_load(path.read_text())
        values = {
            "github.event_name": "pull_request",
            "github.ref": "refs/heads/main",
            "github.event.label.name": "automerge",
            "github.event.action": "unlabeled",
            "github.event.changes.base": False,
            "github.event.pull_request.head.repo.full_name": "example/repo",
            "github.repository": "example/repo",
            "github.event.pull_request.user.login": "4alvit",
            "github.event.pull_request.base.ref": "main",
            "github.event.repository.default_branch": "main",
            "github.event.pull_request.labels.*.name": [],
            "github.event.pull_request.draft": False,
        } | changes
        expression = workflow["jobs"][job]["if"]
        expression = re.sub(
            r"github\.[A-Za-z_.*]+", lambda match: repr(values[match[0]]), expression
        )
        expression = (
            re.sub(r"!(?!=)", " not ", expression)
            .replace("&&", " and ")
            .replace("||", " or ")
        )

        def evaluate(node):
            """Evaluate only the supported expression nodes used by the caller guard."""
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.List):
                return [evaluate(value) for value in node.elts]
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                return not evaluate(node.operand)
            if isinstance(node, ast.BoolOp):
                values = (evaluate(value) for value in node.values)
                return all(values) if isinstance(node.op, ast.And) else any(values)
            if isinstance(node, ast.Compare) and len(node.ops) == 1:
                left, right = evaluate(node.left), evaluate(node.comparators[0])
                if isinstance(node.ops[0], ast.Eq):
                    return left == right
                if isinstance(node.ops[0], ast.NotEq):
                    return left != right
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and not node.keywords
            ):
                args = [evaluate(value) for value in node.args]
                if node.func.id == "format" and args:
                    return args[0].format(*args[1:])
                if node.func.id == "fromJSON" and len(args) == 1:
                    return json.loads(args[0])
                if node.func.id == "contains" and len(args) == 2:
                    return args[1] in args[0]
            raise AssertionError(f"Unsupported caller expression: {ast.dump(node)}")

        return evaluate(ast.parse(" ".join(expression.split()), mode="eval").body)

    def test_withdrawals_use_only_the_target_cleanup_route(self):
        """Conflicts need no pull_request run; off-default forks use metadata only."""
        for author in ("4alvit", "dependabot[bot]", "californiantiramisu", "renovate[bot]"):
            for head_repo in ("example/repo", "contributor/fork"):
                for event in (
                    {},
                    {"github.event.action": "converted_to_draft", "github.event.pull_request.draft": True},
                    {"github.event.action": "edited", "github.event.changes.base": True,
                     "github.event.pull_request.base.ref": "other"},
                ):
                    values = {"github.event.pull_request.user.login": author,
                              "github.event.pull_request.head.repo.full_name": head_repo} | event
                    with self.subTest(values=values):
                        self.assertFalse(self.reachable(**values))
                        self.assertFalse(self.reachable("withdraw-auto-merge", **values))
                        values["github.event_name"] = "pull_request_target"
                        self.assertFalse(self.reachable(**values))
                        self.assertTrue(self.reachable("withdraw-auto-merge", **values))

    def test_positive_default_base_routing_and_return_are_preserved(self):
        """Retargeting back to default can enable again through the ordinary path."""
        for head_repo, author, event in (
            ("example/repo", "4alvit", "pull_request"),
            ("contributor/fork", "4alvit", "pull_request_target"),
            ("example/repo", "dependabot[bot]", "pull_request_target"),
        ):
            for action in ("opened", "edited"):
                values = {"github.event.action": action, "github.event.changes.base": True,
                          "github.event_name": event,
                          "github.event.pull_request.head.repo.full_name": head_repo,
                          "github.event.pull_request.user.login": author}
                with self.subTest(values=values):
                    self.assertTrue(self.reachable(**values))
                    self.assertFalse(self.reachable("withdraw-auto-merge", **values))
                    if event == "pull_request_target":
                        self.assertFalse(self.reachable(**(values | {"github.event.pull_request.base.ref": "other"})))

    def test_untrusted_authors_and_irrelevant_edits_remain_excluded(self):
        """No credentials or jobs for an untrusted author, ref or irrelevant edit."""
        for job in ("auto-merge", "withdraw-auto-merge"):
            self.assertFalse(self.reachable(job, **{"github.event.pull_request.user.login": "stranger"}))
            self.assertFalse(self.reachable(job, **{"github.event.action": "edited"}))
        self.assertFalse(self.reachable("withdraw-auto-merge", **{
            "github.event_name": "pull_request_target", "github.ref": "refs/heads/other"}))


class TargetWithdrawalTests(unittest.TestCase):
    """Run the real cancellation-only entry point with bounded webhook sequences."""

    setUp = AutoMergeTests.setUp

    @staticmethod
    def value(**changes):
        """Build a ready PR with an existing request and its opt-in label removed."""
        value = pr() | {"labels": [], "autoMergeRequest": existing_request()} | changes
        if value["isCrossRepository"]:
            value["headRepository"] = {"id": "R_fork", "nameWithOwner": "contributor/fork"}
        return value

    @staticmethod
    def event(value, action="unlabeled"):
        """Create a matching withdrawal webhook with an explicit event timestamp."""
        return {
            "action": action, "number": 7, "label": {"name": "automerge"},
            "repository": {"full_name": "example/repo"},
            "changes": {"base": {"ref": {"from": "main"}}},
            "pull_request": {
                "head": {"sha": value["headRefOid"], "ref": value["headRefName"],
                         "repo": {"full_name": value["headRepository"]["nameWithOwner"],
                                  "node_id": value["headRepository"]["id"]}},
                "base": {"sha": value["baseRefOid"], "ref": value["baseRefName"],
                         "repo": {"full_name": "example/repo"}},
                "user": {"login": value["author"]["login"]},
                "updated_at": "2026-10-31T12:01:00Z",
            },
        }

    def run_target(self, snapshots, event, **environment):
        """Execute the real target-only mode with mocked transport and event data."""
        with event_environment(event) as env:
            return AutoMergeTests.run_workflow(self, snapshots, env | {
                "WITHDRAWAL_ONLY": "true", "GITHUB_EVENT_NAME": "pull_request_target",
                "GITHUB_REF": "refs/heads/main",
            } | environment)

    def test_conflicted_and_off_default_prs_cancel_without_positive_evaluation(self):
        """Revoke matching requests without relying on a pull-request workflow run."""
        for fork in (False, True):
            for action, changes in (
                ("unlabeled", {}),
                ("converted_to_draft", {"labels": [{"name": "automerge"}], "isDraft": True}),
                ("edited", {"labels": [{"name": "automerge"}], "baseRefName": "other"}),
            ):
                value = self.value(isCrossRepository=fork, **changes)
                with self.subTest(fork=fork, action=action):
                    calls = self.run_target([value, copy.deepcopy(value)], self.event(value, action))
                    self.assertEqual(calls[-1].args, ("pr", "merge", "7", "--repo", "example/repo", "--disable-auto"))
                    self.assertEqual(len(calls), 2)

    def test_input_rejects_merge_modes_and_untrusted_execution_context(self):
        """Reject conflicting modes and execution outside the trusted default context."""
        value = self.value()
        event = self.event(value)
        for env in (
            {"NATIVE_AUTO_MERGE": "true"}, {"REVIEWED_MERGE": "true"},
            {"REVIEWED_SINGLE_PASS": "true"}, {"ALLOW_DEPENDABOT_ACTIONS_REVIEW": "true"},
            {"ALLOW_OWNED_BOT_ACTIONS_REVIEW": "true"},
            {"GITHUB_EVENT_NAME": "pull_request"}, {"GITHUB_REF": "refs/heads/other"},
        ):
            with self.subTest(env=env), self.assertRaises(RuntimeError):
                self.run_target([], event, **env)

    def test_eligible_pr_never_enables_or_merges_even_with_successful_checks(self):
        """Keep the cancellation-only path inert when current eligibility is restored."""
        value = self.value(labels=[{"name": "automerge"}])
        calls = self.run_target([value], self.event(value))
        self.assertEqual(len(calls), 1)

    def test_stale_draft_and_retarget_preserve_manual_and_ambiguous_requests(self):
        """Preserve newer requests and requests whose ordering cannot be proven."""
        for action, change in (("converted_to_draft", {"isDraft": True}), ("edited", {"baseRefName": "other"})):
            value = self.value(labels=[{"name": "automerge"}], **change)
            event = self.event(value, action)
            for request in (None, {"enabledAt": "2026-10-31T12:02:00Z"},
                            {"enabledAt": "2026-10-31T12:01:00Z"}, {"enabledAt": None}):
                with self.subTest(action=action, request=request):
                    current = value | {"autoMergeRequest": request}
                    self.assertEqual(len(self.run_target([current], event)), 1)

    def test_webhook_and_fresh_snapshot_identity_must_both_match(self):
        """Require both webhook identity and the final metadata observation to agree."""
        value = self.value()
        event = self.event(value)
        changes = (
            {"headRefName": "different-branch"}, {"baseRefOid": None},
            {"baseRefName": "other"}, {"isCrossRepository": True},
            {"author": {"login": "4alvit"}}, {"labels": [{"name": "automerge"}]},
        )
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(len(self.run_target([value | change], event)), 1)
                self.assertEqual(len(self.run_target([value, value | change], event)), 1)
        for field in ("head", "base", "user"):
            malformed = copy.deepcopy(event)
            malformed["pull_request"].pop(field)
            with self.subTest(missing=field):
                self.assertEqual(len(self.run_target([value], malformed)), 1)
        for change in ({"number": 8}, {"repository": {"full_name": "other/repo"}},
                       {"action": "labeled", "label": {"name": "bug"}}):
            self.assertEqual(len(self.run_target([], event | change)), 1)

    def test_base_advances_do_not_strand_the_same_withdrawn_request(self):
        """The base commit may move before either read without replacing the request."""
        for fork in (False, True):
            for action, changes in (
                ("unlabeled", {}),
                ("converted_to_draft", {"labels": [{"name": "automerge"}], "isDraft": True}),
                ("edited", {"labels": [{"name": "automerge"}], "baseRefName": "other"}),
            ):
                value = self.value(isCrossRepository=fork, **changes)
                event = self.event(value, action)
                advanced = value | {"baseRefOid": "advanced-base"}
                later = value | {"baseRefOid": "later-base"}
                for snapshots in ([advanced, advanced], [value, advanced], [advanced, later]):
                    with self.subTest(fork=fork, action=action, snapshots=snapshots):
                        calls = self.run_target(snapshots, event)
                        self.assertEqual(calls[-1].args, (
                            "pr", "merge", "7", "--repo", "example/repo", "--disable-auto",
                        ))
                        self.assertEqual(len(calls), 2)

    def test_base_advance_never_owns_a_newer_or_replaced_request(self):
        """Base movement cannot authorize cancellation of a later manual request."""
        value = self.value()
        event = self.event(value)
        advanced = value | {"baseRefOid": "advanced-base"}
        for request in (None, {"enabledAt": "2026-10-31T12:01:00Z"},
                        {"enabledAt": "2026-10-31T12:02:00Z"}, {"enabledAt": None}):
            with self.subTest(request=request):
                replaced = advanced | {"autoMergeRequest": request}
                self.assertEqual(len(self.run_target([replaced], event)), 1)
                self.assertEqual(len(self.run_target([advanced, replaced], event)), 1)

    def test_base_advance_preserves_identity_and_renewed_eligibility_guards(self):
        """A moving base never permits a different source ref, author, branch or opt-in."""
        value = self.value()
        event = self.event(value)
        advanced = value | {"baseRefOid": "advanced-base"}
        for change in ({"headRefName": "different-branch"}, {"baseRefName": "other"},
                       {"isCrossRepository": True}, {"author": {"login": "outsider"}},
                       {"labels": [{"name": "automerge"}]}, {"baseRefOid": None}):
            with self.subTest(change=change):
                changed = advanced | change
                self.assertEqual(len(self.run_target([changed], event)), 1)
                self.assertEqual(len(self.run_target([advanced, changed], event)), 1)

    def test_new_manual_request_after_first_read_is_preserved(self):
        """Do not revoke a replacement request created between metadata reads."""
        value = self.value()
        newer = value | {"autoMergeRequest": {"enabledAt": "2026-10-31T12:02:00Z"}}
        self.assertEqual(len(self.run_target([value, newer], self.event(value))), 1)

    def test_metadata_only_implementation_and_bounded_runtime(self):
        """Keep withdrawal execution bounded and free of PR-code checkout."""
        workflow = yaml.safe_load(WORKFLOW.read_text())
        job = workflow["jobs"]["auto-merge"]
        self.assertIn("inputs.withdrawal-only", job["timeout-minutes"])
        self.assertEqual(len(job["steps"]), 2)
        self.assertTrue(job["steps"][0]["uses"].startswith("step-security/harden-runner@"))
        self.assertNotIn("checkout", str(job["steps"]))
        self.assertNotIn("gh pr checkout", SOURCE)


class CheckAttemptOrderingTests(unittest.TestCase):
    """Model concurrent GitHub runs whose numeric IDs differ from execution order."""

    setUp = AutoMergeTests.setUp
    run_workflow = AutoMergeTests.run_workflow

    @staticmethod
    def approval_attempts(state="SUCCESS"):
        """Reproduce the actual approval attempts from dbus-evcharger PR 24."""
        canceled = attempt(
            "auto-approve / Auto Approve",
            "Auto Approve PRs",
            34672797328,
            "42",
            "CANCELLED",
        )
        canceled.update(
            detailsUrl="https://github.com/example/repo/actions/runs/34672797328/job/103497240819",
            startedAt="2026-09-12T04:21:42Z",
            completedAt="2026-09-12T04:21:43Z",
        )
        replacement = attempt(
            "auto-approve / Auto Approve", "Auto Approve PRs", 34672797312, "45", state
        )
        replacement.update(
            detailsUrl="https://github.com/example/repo/actions/runs/34672797312/job/103497242907",
            startedAt="2026-09-12T04:21:45Z",
            completedAt="2026-09-12T04:21:52Z" if state else "",
        )
        return canceled, replacement

    def test_later_success_can_have_a_lower_run_id(self):
        """A canceled higher run ID cannot hide the subsequently executed approval."""
        response = pr()
        response["statusCheckRollup"].extend(self.approval_attempts())
        calls = self.run_workflow([response, response, response])
        self.assertIn("--auto", calls[-1].args)

    def test_lower_id_pending_replacement_must_finish(self):
        """The active replacement blocks merge even before its start time is available."""
        pending = pr()
        canceled, replacement = self.approval_attempts("")
        replacement.update(status="QUEUED", startedAt="")
        pending["statusCheckRollup"].extend([canceled, replacement])
        passed = pr()
        passed["statusCheckRollup"].extend(self.approval_attempts())
        calls = self.run_workflow([pending, passed, passed, passed])
        self.assertIn("--auto", calls[-1].args)

    def test_later_failure_is_not_hidden_by_older_success(self):
        """Execution order also preserves a real failing replacement with a lower ID."""
        response = pr()
        older, newer = self.approval_attempts("FAILURE")
        older["conclusion"] = "SUCCESS"
        response["statusCheckRollup"].extend([older, newer])
        with self.assertRaisesRegex(RuntimeError, "Auto Approve.*FAILURE"):
            self.run_workflow([response, response, response])


class BotAuthorTests(unittest.TestCase):
    """Exercise GitHub App identity normalization using the workflow harness."""

    setUp = AutoMergeTests.setUp
    run_workflow = AutoMergeTests.run_workflow

    def test_known_graphql_bots_use_canonical_allowlist(self):
        """Verified app identities match their configured REST bot names."""
        for bot in ("dependabot", "renovate"):
            with self.subTest(bot=bot):
                response = pr() | {"author": {"login": f"app/{bot}", "is_bot": True}}
                calls = self.run_workflow(
                    [response, response, response],
                    {"PR_AUTHORS": f"{bot}[bot]"},
                )
                self.assertIn("--auto", calls[-1].args)

    def test_app_alias_requires_verified_bot_identity(self):
        """Missing, false, or truthy non-boolean bot markers never normalize."""
        for bot in ("dependabot", "renovate"):
            for marker in (None, False, "true", 1):
                with self.subTest(bot=bot, marker=marker):
                    author = {"login": f"app/{bot}"}
                    if marker is not None:
                        author["is_bot"] = marker
                    calls = self.run_workflow(
                        [pr() | {"author": author}],
                        {"PR_AUTHORS": f"{bot}[bot]"},
                    )
                    self.assertEqual(len(calls), 1)

    def test_bot_alias_does_not_expand_configured_subset(self):
        """Canonical bots still require the caller's explicit allowed subset."""
        for login in ("app/dependabot", "app/renovate", "app/other"):
            with self.subTest(login=login):
                response = pr() | {"author": {"login": login, "is_bot": True}}
                self.assertEqual(len(self.run_workflow([response])), 1)


if __name__ == "__main__":
    unittest.main()


class NativeAutoMergeTests(AutoMergeTests):
    """The optional native mode delegates waiting only to verified branch rules."""

    def run_native(self, snapshots, rules=None, environment=None):
        """Run native mode with effective branch rules provided by the API fixture."""
        if rules is None:
            rules = [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "required_status_checks": [{"context": "CI gate"}],
                    },
                }
            ]
        import json  # pylint: disable=import-outside-toplevel

        transport = unittest.mock.Mock(
            side_effect=lambda *args: (
                json.dumps(rules) if "/rules/branches/" in args[1] else "true" if args[-1] == ".allow_auto_merge" else "main"
            )
        )
        with (
            patch.dict(
                os.environ,
                self.environment | {"NATIVE_AUTO_MERGE": "true"} | (environment or {}),
            ),
            patch.dict(
                self.code,
                {
                    "snapshot": unittest.mock.Mock(side_effect=snapshots),
                    "gh": transport,
                },
            ),
            patch(
                "time.sleep", side_effect=AssertionError("Native mode must not wait")
            ),
        ):
            self.code["main"]()
        return transport.call_args_list

    def test_pending_gate_is_left_to_github(self):
        """Verified branch protections may safely own pending validation."""
        calls = self.run_native([pr(state="PENDING"), pr(state="PENDING")])
        self.assertIn("--auto", calls[-1].args)
        self.assertEqual(calls[-1].args[-2:], ("--match-head-commit", "abc"))
        self.assertNotIn("--admin", calls[-1].args)

    def test_missing_or_relaxed_protection_fails_closed(self):
        """A native request requires strict protected status checks."""
        snapshot = pr()
        for parameters in (
            {},
            {"strict_required_status_checks_policy": True},
            {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [{"context": "CI gate"}],
            },
        ):
            with (
                self.subTest(parameters=parameters),
                self.assertRaisesRegex(RuntimeError, "strict protected"),
            ):
                self.run_native(
                    [snapshot],
                    [{"type": "required_status_checks", "parameters": parameters}],
                )

    def test_additional_check_must_be_protected(self):
        """Explicit external requirements cannot remain unprotected."""
        snapshots = [pr()]
        with self.assertRaisesRegex(RuntimeError, "every required check"):
            self.run_native(snapshots, environment={"REQUIRED_CHECKS": "Extra review"})

    def test_head_race_does_not_merge(self):
        """A concurrent commit prevents a stale native request."""
        calls = self.run_native([pr(), pr(head="new")])
        self.assertFalse(any("merge" in call.args for call in calls))

    def test_label_removal_disables_pending_request(self):
        """Withdraw authorization when the opt-in label is removed."""
        withdrawn = pr() | {"labels": [], "autoMergeRequest": existing_request()}
        opted_in = pr() | {"autoMergeRequest": existing_request()}
        for snapshots in ([withdrawn, withdrawn], [opted_in, withdrawn, withdrawn]):
            with event_environment(removal_event()) as environment:
                calls = self.run_native(snapshots, environment=environment)
                self.assertIn("--disable-auto", calls[-1].args)
                self.assertFalse(any("--auto" in call.args for call in calls))

    def test_native_revalidation_does_not_cancel_a_replaced_request(self):
        """Bind native-mode cancellation to the earlier eligible request observation."""
        for original in (None, existing_request()):
            opted_in = pr() | {"autoMergeRequest": original}
            newer = pr() | {"isDraft": True, "autoMergeRequest": {"enabledAt": "2026-10-31T12:02:00Z"}}
            with self.subTest(original=original), event_environment(removal_event()) as environment:
                calls = self.run_native([opted_in, newer, newer], environment=environment)
                self.assertFalse(any("merge" in call.args for call in calls))

    def test_native_initial_ineligible_stale_positive_event_preserves_manual_request(self):
        """Prevent a stale positive wakeup from claiming a later manual request."""
        for action in ("opened", "labeled", "edited"):
            for change in ({"isDraft": True}, {"baseRefName": "other"}):
                value = pr() | change | {"autoMergeRequest": {"enabledAt": "2026-10-31T12:02:00Z"}}
                event = removal_event() | {"action": action}
                event["pull_request"]["base"]["ref"] = value["baseRefName"]
                with self.subTest(action=action, change=change), event_environment(event) as environment:
                    calls = self.run_native([value, value], environment=environment)
                    self.assertFalse(any("merge" in call.args for call in calls))

    def test_untrusted_author_never_enables_native_merge(self):
        """Native mode retains the shared trusted-author boundary."""
        calls = self.run_native([pr("stranger")])
        self.assertFalse(any("merge" in call.args for call in calls))


class ReviewedMergeTests(unittest.TestCase):
    """Opt-in merging requires independent current-head review and exact green checks."""

    def setUp(self):
        """Load the same workflow while declaring the dynamically populated namespace."""
        self.code = {}
        AutoMergeTests.setUp(self)

    run_workflow = AutoMergeTests.run_workflow

    @staticmethod
    def reviewed(head="abc", state="SUCCESS"):
        """Include the paginated REST review metadata used by the strict mode."""
        return pr(author="4alvit", head=head, state=state) | {
            "isCrossRepository": False,
            "reviewHistory": [
                {
                    "id": 1,
                    "user": {"login": "californiantiramisu"},
                    "state": "APPROVED",
                    "commit_id": head,
                }
            ],
        }

    def run_reviewed(self, snapshots, **environment):
        """Run the shared workflow in explicitly opted-in reviewed mode."""
        return self.run_workflow(
            snapshots,
            {"REVIEWED_MERGE": "true", "REQUIRED_CHECKS": "CI", **environment},
        )

    def test_green_reviewed_head_merges_without_queued_request(self):
        """The immutable head is guarded and no unprotected auto request is left behind."""
        ready = self.reviewed()
        calls = self.run_reviewed([ready, ready, ready])
        self.assertEqual(calls[-1].args[:2], ("pr", "merge"))
        self.assertEqual(calls[-1].args[-2:], ("--match-head-commit", "abc"))
        self.assertNotIn("--auto", calls[-1].args)
        self.assertNotIn("--admin", calls[-1].args)

    def test_required_checks_must_succeed_never_skip_or_neutral(self):
        """A skipped mandatory job cannot masquerade as completed validation."""
        check = self.code["checked_snapshot"]
        for state in ("SKIPPED", "NEUTRAL", "PENDING"):
            with self.subTest(state=state):
                self.assertFalse(
                    check(self.reviewed(state=state), {"CI"}, "123", True)[0]
                )
        self.assertFalse(check(self.reviewed(), {"Missing"}, "123", True)[0])
        duplicated = self.reviewed()
        duplicated["statusCheckRollup"].append(
            attempt("CI", "Other validation", 125, "59", "SKIPPED")
        )
        self.assertFalse(check(duplicated, {"CI"}, "123", True)[0])

    def test_only_independent_trusted_current_head_approval_qualifies(self):
        """Self, stranger, dismissed, missing and stale approvals never qualify."""
        ready = self.reviewed()
        original = ready["reviewHistory"][0]
        for patching in (
            {"user": {"login": "4alvit"}},
            {"user": {"login": "stranger"}},
            {"user": None},
            {"state": "DISMISSED"},
            {"state": "COMMENTED"},
            {"commit_id": "old"},
        ):
            with self.subTest(patching=patching):
                response = ready | {"reviewHistory": [original | patching]}
                self.assertFalse(self.code["review_state"](response)[0])
        self.assertFalse(self.code["review_state"](ready | {"reviewHistory": []})[0])

    def test_changes_requested_blocks_even_with_another_approval(self):
        """Comments do not erase an outstanding changes request or approval."""
        ready = self.reviewed()
        blocked = ready | {
            "reviewHistory": ready["reviewHistory"]
            + [
                {
                    "id": 2,
                    "user": {"login": "reviewer"},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": "old",
                },
                {
                    "id": 3,
                    "user": {"login": "reviewer"},
                    "state": "COMMENTED",
                    "commit_id": "abc",
                },
            ]
        }
        self.assertFalse(self.code["review_state"](blocked)[0])
        blocked["reviewHistory"].append(
            {
                "id": 4,
                "user": {"login": "reviewer"},
                "state": "DISMISSED",
                "commit_id": "abc",
            }
        )
        self.assertTrue(self.code["review_state"](blocked)[0])

    def test_approval_removal_in_final_refresh_cannot_merge(self):
        """Review state participates in both stability and the final refresh."""
        ready = self.reviewed()
        withdrawn = ready | {"reviewHistory": []}
        closed = withdrawn | {"state": "CLOSED"}
        calls = self.run_reviewed([ready, ready, withdrawn, closed])
        self.assertFalse(any(call.args[:2] == ("pr", "merge") for call in calls))

    def test_changed_head_requires_new_approval_and_stability(self):
        """A new commit with an old approval waits until an independent new review."""
        old = self.reviewed()
        stale_review = old | {"headRefOid": "new"}
        ready = self.reviewed(head="new")
        calls = self.run_reviewed([old, old, stale_review, ready, ready, ready])
        self.assertEqual(calls[-1].args[-1], "new")

    def test_forks_and_unknown_repository_identity_never_merge(self):
        """The shared helper also enforces the caller's no-forks boundary."""
        for cross in (True, None):
            calls = self.run_reviewed([self.reviewed() | {"isCrossRepository": cross}])
            self.assertEqual(len(calls), 1)

    def test_requires_checks_and_disallows_conflicting_native_mode(self):
        """There is no strict mode with an empty validation contract."""
        for environment in ({"REQUIRED_CHECKS": ""}, {"NATIVE_AUTO_MERGE": "true"}):
            with self.assertRaisesRegex(RuntimeError, "explicit checks"):
                self.run_reviewed([], **environment)

    def test_reviews_are_loaded_from_every_page_without_repository_checkout(self):
        """An old changes request on another API page is not silently truncated."""
        import json  # pylint: disable=import-outside-toplevel

        first = self.reviewed()["reviewHistory"][0]
        old = {
            "id": 0,
            "user": {"login": "reviewer"},
            "state": "CHANGES_REQUESTED",
            "commit_id": "old",
        }
        transport = unittest.mock.Mock(
            side_effect=[json.dumps(pr()), json.dumps([[old], [first]])]
        )
        with patch.dict(self.code, {"gh": transport}):
            result = self.code["snapshot"]("example/repo", "7", reviewed=True)
        self.assertEqual(result["reviewHistory"], [old, first])
        self.assertFalse(self.code["review_state"](result)[0])
        self.assertIn("--paginate", transport.call_args_list[-1].args)
        self.assertIn("--slurp", transport.call_args_list[-1].args)


class SinglePassReviewedTests(unittest.TestCase):
    """Exercise wakeups, API identities and races without occupying a runner."""

    setUp = ReviewedMergeTests.setUp
    HEAD = "a" * 40
    BASE = "b" * 40

    @classmethod
    def ready(cls):
        """A same-repository, current-base branch with a current-head approval."""
        value = ReviewedMergeTests.reviewed(head=cls.HEAD)
        value["baseRefOid"] = cls.BASE
        value["statusCheckRollup"][1]["__typename"] = "CheckRun"
        return value

    @staticmethod
    def candidate(number=7):
        """REST candidate discovery is separate from authoritative snapshots."""
        return {
            "number": number,
            "draft": False,
            "base": {"ref": "main"},
            "head": {"repo": {"full_name": "example/repo"}},
            "user": {"login": "4alvit"},
            "labels": [{"name": "automerge"}],
        }

    def run_once(
        self,
        snapshots,
        *,
        pages=None,
        event=None,
        environment=None,
        runs=None,
        jobs=None,
        check_runs=None,
        compare=None,
        base_refs=None,
        failure=None,
        rejected_merges=(),
    ):
        """Mock only metadata transport; execute the actual main and merge decision."""
        import json  # pylint: disable=import-outside-toplevel

        current = {}
        values = (
            {number: iter(items) for number, items in snapshots.items()}
            if isinstance(snapshots, dict)
            else iter(snapshots)
        )
        branch_refs = iter(base_refs) if base_refs is not None else None

        def read_snapshot(*_args, **_kwargs):
            current.clear()
            sequence = values[int(_args[1])] if isinstance(values, dict) else values
            current.update(copy.deepcopy(next(sequence)))
            return copy.deepcopy(current)

        def transport(*args):
            if args == ("api", "repos/example/repo", "--jq", ".default_branch"):
                return "main"
            if args[:2] == ("pr", "merge"):
                if int(args[2]) in rejected_merges:
                    raise self.code["subprocess"].CalledProcessError(1, args)
                return ""
            endpoint = next(item for item in args if item.startswith("repos/"))
            if failure and failure in endpoint:
                raise RuntimeError("API/authentication failed")
            if endpoint == "repos/example/repo/git/ref/heads/main":
                return next(branch_refs) if branch_refs is not None else self.BASE
            if "/actions/runs/" in endpoint:
                number = int(endpoint.rsplit("/", 1)[1])
                return json.dumps(
                    (runs or {}).get(
                        number,
                        {
                            "id": number,
                            "workflow_id": 10,
                            "repository": {"full_name": "example/repo"},
                        },
                    )
                )
            if "/actions/jobs/" in endpoint:
                number = int(endpoint.rsplit("/", 1)[1])
                return json.dumps(
                    (jobs or {}).get(
                        number,
                        {
                            "id": number,
                            "run_id": 123,
                            "name": "Auto Merge",
                            "check_run_url": "https://api.github.com/repos/example/repo/check-runs/42",
                        },
                    )
                )
            if "/check-runs?" in endpoint:
                checks = (
                    check_runs
                    if check_runs is not None
                    else [
                        {
                            "name": "Auto Merge",
                            "app": {"slug": "github-actions"},
                            "details_url": own_check()["detailsUrl"],
                            "head_sha": current["headRefOid"],
                            "url": "https://api.github.com/repos/example/repo/check-runs/42",
                        }
                    ]
                )
                return json.dumps([{"check_runs": checks}])
            if "/compare/" in endpoint:
                return compare if compare is not None else current["baseRefOid"]
            if endpoint.endswith("/pulls?state=open&per_page=100"):
                return json.dumps(pages if pages is not None else [[self.candidate()]])
            raise AssertionError(f"Unexpected API call: {args}")

        env = (
            self.environment
            | {
                "PR_NUMBER": "",
                "REVIEWED_MERGE": "true",
                "REVIEWED_SINGLE_PASS": "true",
                "REQUIRED_CHECKS": "CI",
                "GITHUB_EVENT_NAME": "workflow_run",
                "GITHUB_EVENT_PATH": "/test/event.json",
                "GITHUB_SERVER_URL": "https://github.com",
            }
            | (environment or {})
        )
        payload = (
            event
            if event is not None
            else {"repository": {"full_name": "example/repo"}}
        )
        mocked = unittest.mock.Mock(side_effect=transport)
        with (
            patch.dict(os.environ, env, clear=True),
            patch.dict(self.code, {"snapshot": read_snapshot, "gh": mocked}),
            patch(
                "builtins.open", unittest.mock.mock_open(read_data=json.dumps(payload))
            ),
            patch("time.sleep") as sleep,
        ):
            try:
                self.code["main"]()
            finally:
                self.last_calls = mocked.call_args_list
                sleep.assert_not_called()
        return mocked.call_args_list

    @staticmethod
    def merges(calls):
        """Select only actual merge mutations from read-only API calls."""
        return [call.args for call in calls if call.args[:2] == ("pr", "merge")]

    def test_default_branch_wakeups_need_no_own_check_or_pr_number(self):
        """Completion/cron events evaluate current state even for an old event head."""
        ready = self.ready()
        ready["statusCheckRollup"] = ready["statusCheckRollup"][:1]
        for event_name in ("workflow_run", "schedule", "status", "pull_request_review"):
            with self.subTest(event=event_name):
                calls = self.run_once(
                    [ready] * 3,
                    environment={"GITHUB_EVENT_NAME": event_name},
                    event={
                        "repository": {"full_name": "example/repo"},
                        "workflow_run": {"head_sha": "old", "pull_requests": []},
                    },
                )
                self.assertEqual(len(self.merges(calls)), 1)
                self.assertEqual(
                    self.merges(calls)[0][-2:], ("--match-head-commit", self.HEAD)
                )
                self.assertNotIn("--admin", self.merges(calls)[0])
                self.assertNotIn("--auto", self.merges(calls)[0])

    def test_main_wires_separate_official_reviewer_options(self):
        """The two flags allow only their configured author through the full gate."""
        for author, flag in (
            ("dependabot[bot]", "ALLOW_DEPENDABOT_ACTIONS_REVIEW"),
            ("californiantiramisu", "ALLOW_OWNED_BOT_ACTIONS_REVIEW"),
        ):
            with self.subTest(author=author):
                value = self.ready()
                value["author"] = {"login": author, "is_bot": True}
                value["reviewHistory"][0]["user"] = {
                    "login": "github-actions[bot]",
                    "type": "Bot",
                }
                pages = [[self.candidate() | {"user": {"login": author}}]]
                env = {"PR_AUTHORS": author}
                self.assertFalse(
                    self.merges(self.run_once([value], pages=pages, environment=env))
                )
                self.assertEqual(
                    len(
                        self.merges(
                            self.run_once(
                                [value] * 3,
                                pages=pages,
                                environment=env | {flag: "true"},
                            )
                        )
                    ),
                    1,
                )

    def test_cancelled_same_workflow_job_is_excluded_by_verified_identity(self):
        """An earlier cancelled orchestration job cannot deadlock future wakeups."""
        ready = self.ready()
        ready["statusCheckRollup"][1].update(status="COMPLETED", conclusion="CANCELLED")
        self.assertEqual(len(self.merges(self.run_once([ready] * 3))), 1)

    def test_pending_failed_missing_and_skipped_required_checks_defer_without_sleep(
        self,
    ):
        """Incomplete and failed validation frees the sole metadata runner normally."""
        for state in ("PENDING", "FAILURE", "CANCELLED", "SKIPPED", "NEUTRAL"):
            with self.subTest(state=state):
                value = self.ready()
                value["statusCheckRollup"][0]["conclusion"] = state
                self.assertFalse(self.merges(self.run_once([value])))
        self.assertFalse(
            self.merges(
                self.run_once(
                    [self.ready()], environment={"REQUIRED_CHECKS": "Missing"}
                )
            )
        )

    def test_every_candidate_page_is_processed_after_an_unready_pr(self):
        """Events coalesce safely because one failed PR cannot hide a later ready PR."""
        failed = self.ready()
        failed["statusCheckRollup"][0]["conclusion"] = "FAILURE"
        calls = self.run_once(
            [failed, self.ready(), self.ready(), self.ready()],
            pages=[[self.candidate()], [self.candidate(8)]],
        )
        self.assertEqual(self.merges(calls)[0][2], "8")
        listing = next(call for call in calls if "/pulls?" in " ".join(call.args))
        self.assertIn("--paginate", listing.args)
        self.assertIn("--slurp", listing.args)

    def test_rejected_merge_does_not_starve_later_pr_but_still_fails_the_run(self):
        """A write rejection is not a success or a retry of an ambiguous operation."""
        snapshots = [self.ready()] * 6
        pages = [[self.candidate(), self.candidate(8)]]
        with self.assertRaisesRegex(RuntimeError, r"#7 \(exit 1\)"):
            self.run_once(snapshots, pages=pages, rejected_merges=(7,))
        self.assertEqual([call[2] for call in self.merges(self.last_calls)], ["7", "8"])
        self.assertEqual(
            sum("/compare/" in " ".join(call.args) for call in self.last_calls), 6
        )

    def test_historical_pr_base_does_not_hide_a_behind_candidate(self):
        """A green reviewed PR must include actual main, not only its old PR base."""
        behind = self.ready() | {"mergeStateStatus": "BEHIND"}
        calls = self.run_once(
            [behind] * 3,
            base_refs=["c" * 40],
            compare=self.BASE,
        )
        self.assertFalse(self.merges(calls))
        comparison = next(call for call in calls if "/compare/" in " ".join(call.args))
        self.assertIn(f"/compare/{'c' * 40}...{self.HEAD}", comparison.args[1])

    def test_behind_candidate_does_not_fail_or_starve_the_ready_candidate(self):
        """Expected outdated ancestry is skipped while a later page still merges."""
        behind = self.ready() | {"mergeStateStatus": "BEHIND"}
        ready = self.ready() | {"baseRefOid": "c" * 40}
        calls = self.run_once(
            {7: [behind] * 3, 8: [ready] * 3},
            pages=[[self.candidate()], [self.candidate(8)]],
            base_refs=["c" * 40] * 4,
            rejected_merges=(7,),
        )
        self.assertEqual([call[2] for call in self.merges(calls)], ["8"])

    def test_actual_base_advances_before_final_read_without_changing_pr_base(self):
        """Three stable PR-base values cannot mask a new default branch commit."""
        calls = self.run_once(
            [self.ready()] * 3,
            base_refs=[self.BASE, self.BASE, "c" * 40],
        )
        self.assertFalse(self.merges(calls))

    def test_unstable_own_failed_orchestration_check_does_not_block_ready_source(self):
        """Provider UNSTABLE is not a substitute for verified product-check state."""
        ready = self.ready() | {"mergeStateStatus": "UNSTABLE"}
        ready["statusCheckRollup"][1].update(status="COMPLETED", conclusion="FAILURE")
        self.assertEqual(len(self.merges(self.run_once([ready] * 3))), 1)

    def test_missing_or_malformed_actual_branch_identity_fails_closed(self):
        """A denied or incomplete branch-ref response cannot authorize a merge."""
        for value in ("", "null", "main", "c" * 39):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "default branch identity"):
                    self.run_once([self.ready()] * 3, base_refs=[value])
                self.assertFalse(self.merges(self.last_calls))

    def test_read_failure_aborts_before_attempting_any_candidate_merge(self):
        """Continuing after write rejection never hides a read or authentication error."""
        snapshots = [self.ready()] * 6
        pages = [[self.candidate(), self.candidate(8)]]
        with self.assertRaisesRegex(RuntimeError, "API/authentication"):
            self.run_once(snapshots, pages=pages, failure="/compare/")
        self.assertFalse(self.merges(self.last_calls))
        self.assertEqual(
            sum("/compare/" in " ".join(call.args) for call in self.last_calls), 1
        )

    def test_current_base_is_required_before_first_observation_and_merge(self):
        """A stable but outdated branch must be updated and revalidated first."""
        self.assertFalse(self.merges(self.run_once([self.ready()], compare="c" * 40)))
        calls = self.run_once([self.ready()] * 3)
        self.assertEqual(sum("/compare/" in " ".join(call.args) for call in calls), 3)

    def test_final_state_changes_never_merge(self):
        """Current base, head, decisions and eligibility are checked at the last read."""
        ready = self.ready()
        for change in (
            {"baseRefOid": "c" * 40},
            {"headRefOid": "d" * 40},
            {"reviewHistory": []},
            {"reviewHistory": [ready["reviewHistory"][0] | {"id": 2}]},
            {
                "headRefOid": "d" * 40,
                "reviewHistory": [ready["reviewHistory"][0] | {"commit_id": "d" * 40}],
            },
            {"isCrossRepository": True},
            {"state": "CLOSED"},
            {"isDraft": True},
            {"author": {"login": "stranger"}},
            {"labels": []},
            {"baseRefName": "other"},
            {"statusCheckRollup": [{"name": "CI", "status": "IN_PROGRESS"}]},
        ):
            with self.subTest(change=change):
                self.assertFalse(
                    self.merges(self.run_once([ready, ready, ready | change]))
                )

    def test_fork_draft_untrusted_and_unlabelled_candidates_are_not_read(self):
        """The paginated discovery prefilter never authorizes an ineligible PR."""
        for change in (
            {"draft": True},
            {"base": {"ref": "dev"}},
            {"labels": []},
            {"user": {"login": "stranger"}},
            {"head": {"repo": {"full_name": "fork/repo"}}},
        ):
            self.assertFalse(
                self.merges(self.run_once([], pages=[[self.candidate() | change]]))
            )

    def test_malformed_or_foreign_events_and_run_identity_fail_closed(self):
        """Neither an unrelated repository nor an unverified run can authorize merge."""
        for kwargs in (
            {"event": {"repository": {"full_name": "foreign/repo"}}},
            {"environment": {"GITHUB_EVENT_NAME": "push"}},
            {"environment": {"GITHUB_RUN_ID": "bad"}},
            {
                "runs": {
                    123: {
                        "id": 123,
                        "workflow_id": 0,
                        "repository": {"full_name": "example/repo"},
                    }
                }
            },
            {
                "runs": {
                    123: {
                        "id": 1,
                        "workflow_id": 10,
                        "repository": {"full_name": "example/repo"},
                    }
                }
            },
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(RuntimeError):
                self.run_once([], **kwargs)

    def test_api_errors_never_become_normal_not_ready(self):
        """Auth and incomplete API results fail the workflow so recovery is visible."""
        for failure in (
            "/pulls?",
            "/actions/runs/",
            "/compare/",
            "/git/ref/heads/",
            "/actions/jobs/",
            "/check-runs?",
        ):
            with (
                self.subTest(failure=failure),
                self.assertRaisesRegex(RuntimeError, "API/authentication"),
            ):
                self.run_once([self.ready()], failure=failure)

    def test_foreign_workflow_job_and_spoofed_check_are_never_excluded(self):
        """A name or URL alone does not prove an orchestration check belongs to us."""
        base_run = {
            "id": 122,
            "workflow_id": 99,
            "repository": {"full_name": "example/repo"},
        }
        variants = [
            {
                "detailsUrl": "https://foreign.example/example/repo/actions/runs/123/job/4"
            },
            {"detailsUrl": "https://github.com/foreign/repo/actions/runs/123/job/4"},
            {"detailsUrl": own_check()["detailsUrl"] + "?forged=1"},
            {"detailsUrl": own_check()["detailsUrl"] + "/extra"},
            {"detailsUrl": own_check()["detailsUrl"].replace("/123/", "/122/")},
            {"__typename": "StatusContext"},
            {"name": "Actual validator"},
        ]
        for change in variants:
            with self.subTest(change=change):
                value = self.ready()
                value["statusCheckRollup"][1].update(change)
                self.assertFalse(
                    self.merges(self.run_once([value], runs={122: base_run}))
                )
        for job in (
            {"id": 4, "run_id": 999, "name": "Auto Merge"},
            {"id": 4, "run_id": 123, "name": "Actual validator"},
        ):
            self.assertFalse(self.merges(self.run_once([self.ready()], jobs={4: job})))
        self.assertFalse(self.merges(self.run_once([self.ready()], check_runs=[])))
        legitimate = {
            "name": "Auto Merge",
            "app": {"slug": "github-actions"},
            "head_sha": self.HEAD,
            "details_url": own_check()["detailsUrl"],
            "url": "https://api.github.com/repos/example/repo/check-runs/42",
        }
        for checks in (
            [legitimate | {"app": {"slug": "foreign-app"}}],
            [
                legitimate
                | {"url": "https://api.github.com/repos/example/repo/check-runs/99"}
            ],
            [legitimate, legitimate | {"app": {"slug": "foreign-app"}}],
        ):
            self.assertFalse(
                self.merges(self.run_once([self.ready()], check_runs=checks))
            )


class OfficialActionsReviewTests(unittest.TestCase):
    """The optional Actions reviewer is scoped to two explicitly allowed authors."""

    setUp = ReviewedMergeTests.setUp

    @staticmethod
    def response(author="dependabot[bot]", bot=True):
        """Represent REST reviewer type independently of the GraphQL PR author."""
        value = ReviewedMergeTests.reviewed()
        value["author"] = {"login": author, "is_bot": bot}
        value["reviewHistory"][0]["user"] = {
            "login": "github-actions[bot]",
            "type": "Bot",
        }
        return value

    def test_each_official_path_requires_its_own_explicit_option(self):
        """Legacy defaults and the other author's flag cannot enable this reviewer."""
        for author in ("dependabot[bot]", "californiantiramisu"):
            response = self.response(author)
            self.assertFalse(self.code["review_state"](response)[0])
            self.assertTrue(self.code["review_state"](response, {author})[0])
            other = {"dependabot[bot]", "californiantiramisu"} - {author}
            self.assertFalse(self.code["review_state"](response, other)[0])
        self.assertTrue(
            self.code["review_state"](
                self.response("app/dependabot"), {"dependabot[bot]"}
            )[0]
        )

    def test_human_and_other_bot_authors_never_gain_actions_review(self):
        """Even both flags cannot substitute Actions for the existing human PR policy."""
        for author, bot in (
            ("4alvit", False),
            ("renovate[bot]", True),
            ("dependabot[bot]", False),
            ("app/dependabot", False),
            ("stranger", False),
        ):
            self.assertFalse(
                self.code["review_state"](
                    self.response(author, bot),
                    {"dependabot[bot]", "californiantiramisu"},
                )[0]
            )

    def test_forks_stale_or_nonbot_reviews_never_qualify(self):
        """An exact reviewer login is insufficient without Bot type and current head."""
        ready = self.response()
        for cross in (True, None):
            self.assertFalse(
                self.code["review_state"](
                    ready | {"isCrossRepository": cross}, {"dependabot[bot]"}
                )[0]
            )
        for change in (
            {"user": {"login": "github-actions[bot]", "type": "User"}},
            {"user": {"login": "other[bot]", "type": "Bot"}},
            {"commit_id": "old"},
            {"state": "DISMISSED"},
            {"state": "COMMENTED"},
        ):
            value = ready | {"reviewHistory": [ready["reviewHistory"][0] | change]}
            self.assertFalse(self.code["review_state"](value, {"dependabot[bot]"})[0])

    def test_later_dismissal_and_changes_requested_still_block(self):
        """Opting in does not erase reviewer decisions or accept a dismissed review."""
        ready = self.response()
        for change in (
            {"state": "DISMISSED"},
            {"state": "CHANGES_REQUESTED", "user": {"login": "someone"}},
        ):
            value = ready | {
                "reviewHistory": ready["reviewHistory"]
                + [ready["reviewHistory"][0] | change | {"id": 2}]
            }
            self.assertFalse(self.code["review_state"](value, {"dependabot[bot]"})[0])

    def test_main_requires_reviewed_mode_for_all_new_options(self):
        """Neither new mode nor reviewer flags affect legacy or native consumers."""
        for flag in (
            "REVIEWED_SINGLE_PASS",
            "ALLOW_DEPENDABOT_ACTIONS_REVIEW",
            "ALLOW_OWNED_BOT_ACTIONS_REVIEW",
        ):
            with (
                self.subTest(flag=flag),
                self.assertRaisesRegex(RuntimeError, "require reviewed"),
            ):
                AutoMergeTests.run_workflow(self, [], {flag: "true"})


class DisabledNativeCapabilityTests(unittest.TestCase):
    """Execute the checked fallback without relaxing protected merge requirements."""

    setUp = AutoMergeTests.setUp

    def run_capability(
        self, snapshots, capability="false", environment=None, merge_error=None
    ):
        """Keep real main/check/review code and replace only API transport and time."""
        env = self.environment | (environment or {})

        def transport(*args):
            if args == ("api", "repos/example/repo", "--jq", ".allow_auto_merge"):
                if isinstance(capability, Exception):
                    raise capability
                return capability
            if args[:2] == ("pr", "merge") and merge_error:
                raise merge_error
            return "main"

        self.transport = unittest.mock.Mock(side_effect=transport)
        self.snapshots = unittest.mock.Mock(side_effect=snapshots)
        with (
            patch.dict(os.environ, env),
            patch.dict(self.code, {"gh": self.transport, "snapshot": self.snapshots}),
            patch("time.sleep"),
            patch("time.monotonic", return_value=0),
        ):
            self.code["main"]()
        return self.transport.call_args_list

    def merge_calls(self):
        """Return only mutations, independent of how many metadata reads occur."""
        return [
            call
            for call in self.transport.call_args_list
            if call.args[:2] == ("pr", "merge")
        ]

    def test_enabled_capability_keeps_native_request(self):
        """The default enabled path still submits one exact-head native request."""
        self.run_capability([pr(), pr(), pr()], capability="true")
        self.assertEqual(len(self.merge_calls()), 1)
        self.assertIn("--auto", self.merge_calls()[0].args)
        self.assertNotIn("--admin", self.merge_calls()[0].args)
        self.assertEqual(
            self.merge_calls()[0].args[-2:], ("--match-head-commit", "abc")
        )

    def test_disabled_capability_requires_new_reviewed_observations(self):
        """Three fresh reviewed snapshots follow the discarded initial read."""
        ready = ReviewedMergeTests.reviewed()
        self.run_capability([pr(), ready, ready, ready])
        self.assertEqual(len(self.merge_calls()), 1)
        args = self.merge_calls()[0].args
        self.assertEqual(args[-2:], ("--match-head-commit", "abc"))
        self.assertNotIn("--auto", args)
        self.assertNotIn("--admin", args)
        self.assertEqual(self.snapshots.call_args_list[0].kwargs, {})
        self.assertTrue(
            all(
                call.kwargs == {"reviewed": True}
                for call in self.snapshots.call_args_list[1:]
            )
        )

    def test_unusable_capability_never_authorizes_fallback(self):
        """Missing, malformed, non-boolean and denied metadata never mutates."""
        for capability in (
            "null",
            "0",
            "1",
            '"false"',
            "{}",
            "[]",
            "",
            "false trailing",
            RuntimeError("Forbidden"),
        ):
            with (
                self.subTest(capability=capability),
                self.assertRaises((RuntimeError, ValueError)),
            ):
                self.run_capability([pr()], capability=capability)
            self.assertEqual(self.merge_calls(), [])

    def test_ineligible_pr_does_not_read_capability_or_mutate(self):
        """Closed, draft, wrong author/target and unlabeled PRs remain ineligible."""
        for changes in (
            {"state": "CLOSED"},
            {"isDraft": True},
            {"author": {"login": "stranger"}},
            {"baseRefName": "other"},
            {"labels": []},
        ):
            with self.subTest(changes=changes):
                self.run_capability(
                    [pr() | changes], capability=RuntimeError("must not query")
                )
                self.assertEqual(self.merge_calls(), [])
                self.assertEqual(len(self.transport.call_args_list), 1)

    def test_absent_stale_or_retracted_review_never_merges(self):
        """The fallback does not inherit old-head, self, dismissed or blocked approval."""
        ready = ReviewedMergeTests.reviewed()
        review = ready["reviewHistory"][0]
        histories = [
            [],
            [review | {"commit_id": "old"}],
            [review | {"user": {"login": "4alvit"}}],
            [review | {"state": "DISMISSED"}],
            [
                review,
                review
                | {"id": 2, "state": "CHANGES_REQUESTED", "user": {"login": "other"}},
            ],
        ]
        for history in histories:
            with self.subTest(history=history):
                self.run_capability(
                    [
                        pr(),
                        ready | {"reviewHistory": history},
                        ready | {"state": "CLOSED"},
                    ]
                )
                self.assertEqual(self.merge_calls(), [])

    def test_pending_empty_and_nonpassing_named_checks_never_merge(self):
        """An approved PR still requires actual success and every configured gate."""
        ready = ReviewedMergeTests.reviewed()
        for changes, required in (
            ({"statusCheckRollup": []}, ""),
            ({"statusCheckRollup": [own_check()]}, ""),
            ({}, "Missing"),
            (
                {
                    "statusCheckRollup": [
                        {"name": "CI", "status": "IN_PROGRESS"},
                        own_check(),
                    ]
                },
                "",
            ),
            (
                {
                    "statusCheckRollup": [
                        {"name": "CI", "status": "COMPLETED", "conclusion": "SKIPPED"},
                        own_check(),
                    ]
                },
                "",
            ),
            (
                {
                    "statusCheckRollup": [
                        {"name": "CI", "status": "COMPLETED", "conclusion": "NEUTRAL"},
                        own_check(),
                    ]
                },
                "CI",
            ),
        ):
            with self.subTest(changes=changes, required=required):
                self.run_capability(
                    [pr(), ready | changes, ready | {"state": "CLOSED"}],
                    environment={"REQUIRED_CHECKS": required},
                )
                self.assertEqual(self.merge_calls(), [])

    def test_failed_or_cancelled_check_stops_fallback(self):
        """Failure cannot be hidden by the independently approved review."""
        for state in ("FAILURE", "CANCELLED", "TIMED_OUT", "ERROR"):
            with (
                self.subTest(state=state),
                self.assertRaisesRegex(RuntimeError, "check failed"),
            ):
                self.run_capability([pr(), ReviewedMergeTests.reviewed(state=state)])
            self.assertEqual(self.merge_calls(), [])

    def test_fork_and_unknown_identity_stop_fallback(self):
        """Only same-repository PRs have a direct-merge fallback."""
        for cross in (True, None):
            with self.subTest(cross=cross):
                self.run_capability(
                    [pr(), ReviewedMergeTests.reviewed() | {"isCrossRepository": cross}]
                )
                self.assertEqual(self.merge_calls(), [])

    def test_missing_base_identity_fails_closed(self):
        """A missing base cannot qualify as a stable base across observations."""
        with self.assertRaisesRegex(RuntimeError, "current base"):
            self.run_capability(
                [pr(), ReviewedMergeTests.reviewed() | {"baseRefOid": None}]
            )
        self.assertEqual(self.merge_calls(), [])

    def test_changed_base_or_head_requires_new_stable_observations(self):
        """A final refresh race invalidates the old green fingerprint."""
        ready = ReviewedMergeTests.reviewed()
        for changed in (
            ready | {"baseRefOid": "new-base"},
            ReviewedMergeTests.reviewed(head="new-head"),
        ):
            with self.subTest(changed=changed):
                self.run_capability(
                    [pr(), ready, ready, changed, changed, changed, changed]
                )
                self.assertEqual(self.snapshots.call_count, 7)
                self.assertEqual(len(self.merge_calls()), 1)
                self.assertEqual(self.merge_calls()[0].args[-1], changed["headRefOid"])

    def test_withdrawn_final_snapshot_never_merges(self):
        """Final loss of label, draft eligibility or review stops the mutation."""
        ready = ReviewedMergeTests.reviewed()
        for changes in (
            {"labels": []},
            {"isDraft": True},
            {"reviewHistory": []},
            {"baseRefName": "other"},
        ):
            with self.subTest(changes=changes):
                self.run_capability(
                    [pr(), ready, ready, ready | changes, ready | {"state": "CLOSED"}]
                )
                self.assertEqual(self.merge_calls(), [])

    def test_explicit_native_mode_rejects_disabled_capability(self):
        """Explicit native callers receive a diagnostic, never a silent mode switch."""
        with self.assertRaisesRegex(RuntimeError, "Native auto-merge is disabled"):
            self.run_capability([pr()], environment={"NATIVE_AUTO_MERGE": "true"})
        self.assertEqual(self.merge_calls(), [])

    def test_ambiguous_merge_error_is_not_replayed(self):
        """A mutation response error propagates after exactly one protected attempt."""
        ready = ReviewedMergeTests.reviewed()
        with self.assertRaisesRegex(RuntimeError, "ambiguous response"):
            self.run_capability(
                [pr(), ready, ready, ready],
                merge_error=RuntimeError("ambiguous response"),
            )
        self.assertEqual(len(self.merge_calls()), 1)


class MergeAuthorIdentityTests(unittest.TestCase):
    """Authenticate the merge author without relying on personal email settings."""

    setUp = AutoMergeTests.setUp

    def test_authenticated_account_not_public_email_or_event_actor(self):
        account = {"id": 123456, "login": "merge-account", "type": "User",
                   "email": "private@example.invalid"}
        transport = unittest.mock.Mock(return_value=json.dumps(account))
        with patch.dict(self.code, {"gh": transport}):
            self.assertEqual(self.real_merge_author_email(),
                             "123456+merge-account@users.noreply.github.com")
        transport.assert_called_once_with("api", "user", "--jq", "{id,login,type}")

    def test_missing_malformed_or_nonuser_identity_fails_closed(self):
        for value in [None, [], {}, {"id": True, "login": "actor", "type": "User"},
                      {"id": 0, "login": "actor", "type": "User"},
                      {"id": "12", "login": "actor", "type": "User"},
                      {"id": 12, "login": "actor", "type": "Bot"},
                      {"id": 12, "login": "--actor", "type": "User"},
                      {"id": 12, "login": "actor@invalid", "type": "User"},
                      {"id": 12, "login": "actor\n", "type": "User"}]:
            with self.subTest(value=value), patch.dict(
                self.code, {"gh": unittest.mock.Mock(return_value=json.dumps(value))}
            ), self.assertRaisesRegex(RuntimeError, "authenticated merge author"):
                self.real_merge_author_email()

    def test_unavailable_account_never_reaches_merge(self):
        def transport(*args):
            if args[:2] == ("api", "user"):
                raise self.code["subprocess"].CalledProcessError(1, ["gh", "api", "user"])
            return "true" if args[-1] == ".allow_auto_merge" else "main"
        mocked = unittest.mock.Mock(side_effect=transport)
        with patch.dict(os.environ, self.environment), patch.dict(self.code, {
            "gh": mocked, "snapshot": unittest.mock.Mock(side_effect=[pr(), pr(), pr()]),
            "merge_author_email": self.real_merge_author_email,
        }), patch("time.sleep"), patch("time.monotonic", return_value=0), self.assertRaises(
            self.code["subprocess"].CalledProcessError
        ):
            self.code["main"]()
        self.assertFalse(any(c.args[:2] == ("pr", "merge") for c in mocked.call_args_list))

    def test_email_rejection_never_retries_with_personal_default(self):
        def transport(*args):
            if args[:2] == ("api", "user"):
                return json.dumps({"id": 123456, "login": "merge-account", "type": "User"})
            if args[:2] == ("pr", "merge"):
                raise self.code["subprocess"].CalledProcessError(1, ["gh", "pr", "merge"])
            return "true" if args[-1] == ".allow_auto_merge" else "main"
        mocked = unittest.mock.Mock(side_effect=transport)
        with patch.dict(os.environ, self.environment), patch.dict(self.code, {
            "gh": mocked, "snapshot": unittest.mock.Mock(side_effect=[pr(), pr(), pr()]),
            "merge_author_email": self.real_merge_author_email,
        }), patch("time.sleep"), patch("time.monotonic", return_value=0), self.assertRaises(
            self.code["subprocess"].CalledProcessError
        ):
            self.code["main"]()
        merges = [c.args for c in mocked.call_args_list if c.args[:2] == ("pr", "merge")]
        self.assertEqual(len(merges), 1)
        self.assertIn("--author-email", merges[0])
        self.assertIn("123456+merge-account@users.noreply.github.com", merges[0])
        self.assertEqual(merges[0][-2:], ("--match-head-commit", "abc"))

    def test_every_positive_actuator_binds_author_and_head_but_withdrawal_does_not(self):
        calls = [node for node in ast.walk(ast.parse(SOURCE))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == "gh" and len(node.args) >= 2
                 and all(isinstance(node.args[i], ast.Constant)
                         and node.args[i].value == value for i, value in enumerate(("pr", "merge")))]
        positive = []
        for call in calls:
            strings = [a.value for a in call.args if isinstance(a, ast.Constant)]
            if "--disable-auto" in strings:
                self.assertNotIn("--author-email", strings)
            else:
                positive.append(call)
                self.assertIn("--author-email", strings)
                self.assertIn("--match-head-commit", strings)
                self.assertNotIn("--admin", strings)
                self.assertTrue(any(isinstance(a, ast.Call) and isinstance(a.func, ast.Name)
                                    and a.func.id == "merge_author_email" for a in call.args))
        self.assertEqual(len(positive), 3)

    def test_real_modes_keep_explicit_author(self):
        ordinary = AutoMergeTests(); ordinary.setUp()
        native = NativeAutoMergeTests(); native.setUp()
        reviewed = ReviewedMergeTests(); reviewed.setUp()
        single = SinglePassReviewedTests(); single.setUp()
        ready = reviewed.reviewed()
        ready_single = single.ready()
        cases = [ordinary.run_workflow([pr(), pr(), pr()]),
                 native.run_native([pr(), pr()]),
                 reviewed.run_reviewed([ready, ready, ready]),
                 single.run_once([ready_single, ready_single, ready_single])]
        for calls in cases:
            merges = [c.args for c in calls if c.args[:2] == ("pr", "merge")]
            self.assertEqual(len(merges), 1)
            self.assertIn("--author-email", merges[0])
            self.assertIn("123456+merge-account@users.noreply.github.com", merges[0])
            self.assertEqual(merges[0][-2], "--match-head-commit")


class WithdrawalHeadAdvanceTests(unittest.TestCase):
    """Execute the actual cancellation paths across commits, never live mutations."""

    setUp = AutoMergeTests.setUp
    run_target = TargetWithdrawalTests.run_target
    run_native = NativeAutoMergeTests.run_native
    run_workflow = WithdrawalTests.run_workflow

    @staticmethod
    def value(head="abc", **changes):
        return pr(head=head) | {"labels": [], "autoMergeRequest": existing_request()} | changes

    def assert_cancel_only(self, calls):
        mutations = [call.args for call in calls if call.args[:2] == ("pr", "merge")]
        self.assertEqual(mutations, [("pr", "merge", "7", "--repo", "example/repo", "--disable-auto")])

    def test_original_event_survives_head_changes_before_and_between_reads(self):
        """The original unlabel event owns the same request on the same branch."""
        for values in ([self.value("new"), self.value("new")],
                       [self.value(), self.value("new")],
                       [self.value("new"), self.value("newer")]):
            with self.subTest(heads=[value["headRefOid"] for value in values]):
                self.assert_cancel_only(self.run_target(values, removal_event()))
                self.assert_cancel_only(self.run_workflow(values))
                with event_environment(removal_event()) as environment:
                    self.assert_cancel_only(self.run_native(values, environment=environment))

    def test_surviving_waiter_owns_same_request_after_push_at_both_observations(self):
        """A prior eligible observation binds the request without freezing its commit."""
        for changes in ({"labels": []}, {"isDraft": True}, {"baseRefName": "other"}):
            opted = pr(state="PENDING") | {"autoMergeRequest": existing_request()}
            withdrawn = pr(head="new") | {"autoMergeRequest": existing_request()} | changes
            with self.subTest(changes=changes):
                self.assert_cancel_only(self.run_workflow(
                    [opted, withdrawn, withdrawn], removal_event() | {"action": "opened"}))
                ready = opted | {"statusCheckRollup": pr()["statusCheckRollup"]}
                self.assert_cancel_only(self.run_workflow(
                    [ready, ready, withdrawn, withdrawn], removal_event() | {"action": "opened"}))
                with event_environment(removal_event() | {"action": "opened"}) as environment:
                    self.assert_cancel_only(self.run_native([opted, withdrawn, withdrawn], environment=environment))

    def test_synchronize_alone_cannot_claim_unobserved_manual_request(self):
        """The replacement event is not a substitute for surviving withdrawal routing."""
        event = removal_event() | {"action": "synchronize"}
        event["pull_request"]["head"]["sha"] = "new"
        values = [self.value("new"), self.value("new")]
        self.assertFalse(any("--disable-auto" in call.args for call in self.run_target(values, event)))
        self.assertFalse(any("--disable-auto" in call.args for call in self.run_workflow(values, event)))

    def test_changed_stable_identity_and_missing_fields_refuse_cancellation(self):
        """A known request cannot cross PRs, repository nodes, refs, authors or unknown metadata."""
        for changes in ({"number": 8}, {"number": None}, {"headRefName": "other"},
                        {"headRefName": None}, {"headRefOid": None}, {"baseRefOid": None},
                        {"baseRefName": "other"}, {"author": {"login": "4alvit"}},
                        {"headRepository": {"id": "R_recreated", "nameWithOwner": "example/repo"}},
                        {"headRepository": {"id": "R_example", "nameWithOwner": "other/repo"}},
                        {"headRepository": None}, {"isCrossRepository": None}):
            with self.subTest(changes=changes):
                changed = self.value("new", **changes)
                for values in ([changed], [self.value(), changed]):
                    self.assertFalse(any("--disable-auto" in call.args for call in self.run_target(values, removal_event())))

    def test_renewed_event_intent_is_preserved_even_if_other_reason_is_ineligible(self):
        """Restoring the event's withdrawn condition cannot transfer ownership to another reason."""
        restored = self.value("new", labels=[{"name": "automerge"}], isDraft=True)
        self.assertFalse(any("--disable-auto" in call.args for call in self.run_target([restored], removal_event())))
        for action, initial, changed in (
            ("unlabeled", self.value(), restored),
            ("converted_to_draft", self.value(labels=[{"name": "automerge"}], isDraft=True),
             self.value("new", labels=[{"name": "automerge"}], isDraft=False, baseRefName="other")),
            ("edited", self.value(labels=[{"name": "automerge"}], baseRefName="other"),
             self.value("new", labels=[{"name": "automerge"}], baseRefName="main", isDraft=True)),
        ):
            with self.subTest(action=action):
                event = TargetWithdrawalTests.event(initial, action)
                self.assertFalse(any("--disable-auto" in call.args for call in self.run_target([initial, changed], event)))
        original_draft = self.value(labels=[{"name": "automerge"}], isDraft=True)
        restored_draft = self.value("new", labels=[{"name": "automerge"}], isDraft=False)
        self.assertFalse(any("--disable-auto" in call.args for call in self.run_target(
            [restored_draft], TargetWithdrawalTests.event(original_draft, "converted_to_draft"))))

    def test_replacement_and_ambiguous_time_survive_commit_advancement(self):
        """Neither an event nor an observed request can claim a replacement or unknown time."""
        for request in (None, {"enabledAt": "2026-10-31T12:01:00Z"},
                        {"enabledAt": "2026-10-31T12:02:00Z"}, {"enabledAt": None},
                        {"enabledAt": "2026-10-31T12:00:00"},
                        existing_request() | {"enabledBy": {"login": "manual-replacement"}}):
            current = self.value("new", autoMergeRequest=request)
            with self.subTest(request=request):
                # A fresh replacement between reads always stays untouched.
                self.assertFalse(any("--disable-auto" in call.args for call in self.run_target(
                    [self.value(), current], removal_event())))
                opted = pr(state="PENDING") | {"autoMergeRequest": existing_request()}
                self.assertFalse(any("--disable-auto" in call.args for call in self.run_workflow(
                    [opted, current], removal_event() | {"action": "opened"})))

    def test_missing_event_ref_repository_node_author_or_time_refuses(self):
        """Commit advancement does not make partial old webhook identity trustworthy."""
        for path in (("head", "ref"), ("head", "repo", "node_id"),
                     ("head", "repo", "full_name"), ("user", "login"), ("updated_at",)):
            event = removal_event()
            parent = event["pull_request"]
            for key in path[:-1]:
                parent = parent[key]
            parent.pop(path[-1])
            with self.subTest(path=path):
                self.assertFalse(any("--disable-auto" in call.args for call in self.run_target(
                    [self.value("new")], event)))

    def test_positive_native_head_race_still_refuses_and_current_head_stays_bound(self):
        """Cancellation's branch identity never weakens positive exact-head admission."""
        calls = self.run_native([pr(), pr(head="new")])
        self.assertFalse(any(call.args[:2] == ("pr", "merge") for call in calls))
        calls = self.run_native([pr(head="new"), pr(head="new")])
        self.assertEqual(calls[-1].args[-2:], ("--match-head-commit", "new"))
        self.assertIn("--author-email", calls[-1].args)
