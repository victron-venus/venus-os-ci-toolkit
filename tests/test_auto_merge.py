"""Exercise the reusable workflow's actual embedded implementation without GitHub calls."""

import copy
import os
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

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
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": author},
        "baseRefName": "main",
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
                    "gh": unittest.mock.Mock(return_value="main"),
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
        self.assertEqual(len(self.run_workflow([pr(), pr(), changed, changed])), 1)

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
                    "gh": unittest.mock.Mock(return_value="main"),
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
                json.dumps(rules) if "/rules/branches/" in args[1] else "main"
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
        withdrawn = pr() | {"labels": [], "autoMergeRequest": {"enabledAt": "today"}}
        for snapshots in ([withdrawn], [pr(), withdrawn]):
            calls = self.run_native(snapshots)
            self.assertIn("--disable-auto", calls[-1].args)
            self.assertFalse(any("--auto" in call.args for call in calls))

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
        failure=None,
        rejected_merges=(),
    ):
        """Mock only metadata transport; execute the actual main and merge decision."""
        import json  # pylint: disable=import-outside-toplevel

        current = {}
        values = iter(snapshots)

        def read_snapshot(*_args, **_kwargs):
            current.clear()
            current.update(copy.deepcopy(next(values)))
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
