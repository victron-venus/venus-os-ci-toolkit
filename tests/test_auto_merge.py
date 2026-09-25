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
            with (
                self.subTest(state=state),
                self.assertRaisesRegex(RuntimeError, "check failed"),
            ):
                self.run_workflow([pr(state=state)])

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
        with self.assertRaisesRegex(RuntimeError, "API unavailable"):
            self.run_workflow([RuntimeError("API unavailable")])


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
