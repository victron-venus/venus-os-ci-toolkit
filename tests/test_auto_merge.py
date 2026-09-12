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
    .split("python3 - <<'PY'\n", 1)[1]
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
