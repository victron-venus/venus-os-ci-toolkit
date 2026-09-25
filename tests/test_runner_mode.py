"""Exercise activation and rollback against a simulated GitHub API, without writes."""

import copy
import importlib.util
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "runner_mode", Path(__file__).resolve().parents[1] / "scripts/runner_mode.py"
)
mode = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mode)
NOW = datetime(2026, 9, 24, 18, tzinfo=timezone.utc)


class RunnerModeTests(unittest.TestCase):
    def setUp(self):
        self.repo = "open-ott-play/ottplay-android"
        self.metadata = {
            "private": True,
            "full_name": self.repo,
            "default_branch": "main",
        }
        self.variable = None
        self.writes = []
        self.tip = {"protected": True, "commit": {"sha": "a" * 40}}
        self.run = {
            "event": "workflow_dispatch",
            "path": mode.SMOKE_PATH,
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
            "head_sha": "a" * 40,
            "created_at": "2026-09-24T17:00:00Z",
            "head_repository": {"full_name": self.repo},
            "run_attempt": 2,
        }
        self.groups = [
            {
                "id": 11,
                "name": mode.CI_GROUP,
                "visibility": "selected",
                "allows_public_repositories": False,
            },
            {
                "id": 12,
                "name": mode.RELEASE_GROUP,
                "visibility": "selected",
                "allows_public_repositories": False,
                "restricted_to_workflows": True,
                "selected_workflows": [
                    f"{self.repo}/.github/workflows/{file}@refs/heads/main"
                    for file in ("release.yml", "runner-smoke.yml")
                ],
            },
        ]
        self.jobs = [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "labels": [label],
                "runner_group_id": 12 if group == mode.RELEASE_GROUP else 11,
                "runner_name": "ephemeral-deleted-after-job",
            }
            for name, (label, group) in mode.POOLS["ottplay-android"].items()
        ]

    def api(self, path, method="GET", payload=None):
        if method != "GET":
            self.writes.append((path, method, payload))
            self.variable = payload["value"]
            return None
        path = path.split("?")[0]
        if path == f"repos/{self.repo}":
            return self.metadata
        if path == f"repos/{self.repo}/actions/variables":
            return {
                "variables": []
                if self.variable is None
                else [{"name": mode.VARIABLE, "value": self.variable}]
            }
        if path.endswith("/branches/main"):
            return self.tip
        if path.endswith("/actions/runs/77"):
            return self.run
        if path.endswith("/attempts/2/jobs"):
            return {"jobs": self.jobs}
        if path == f"orgs/{mode.ORG}/actions/runner-groups":
            return {"runner_groups": self.groups}
        if path.endswith("/repositories"):
            return {"repositories": [self.metadata]}
        self.fail(f"Unexpected API endpoint {path}")

    def verify(self):
        with patch.object(mode, "api", side_effect=self.api):
            mode.verify_smoke(self.repo, self.metadata, 77, NOW)

    def execute(self, *arguments):
        with (
            patch.object(mode, "api", side_effect=self.api),
            patch.object(mode, "datetime") as date,
        ):
            date.now.return_value = NOW
            date.fromisoformat.side_effect = datetime.fromisoformat
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                return mode.main(["--repo", "ottplay-android", *arguments])

    def test_scale_zero_needs_no_online_runner_inventory(self):
        self.verify()  # The fake API deliberately implements no /actions/runners endpoint.

    def test_dry_run_does_not_mutate(self):
        self.assertEqual(self.execute("--mode", "k3s", "--smoke-run", "77"), 0)
        self.assertEqual(self.writes, [])

    def test_activation_creates_exact_repository_variable(self):
        self.assertEqual(
            self.execute("--mode", "k3s", "--smoke-run", "77", "--apply"), 0
        )
        self.assertEqual(
            self.writes,
            [
                (
                    f"repos/{self.repo}/actions/variables",
                    "POST",
                    {"name": mode.VARIABLE, "value": "k3s"},
                )
            ],
        )

    def test_rollback_does_not_require_smoke_or_live_cluster(self):
        self.variable = "k3s"
        self.run = {}
        self.groups = []
        self.assertEqual(self.execute("--mode", "github", "--apply"), 0)
        self.assertEqual(self.writes[0][1], "PATCH")
        self.assertEqual(self.variable, "github")

    def test_missing_smoke_never_writes(self):
        self.assertEqual(self.execute("--mode", "k3s", "--apply"), 2)
        self.assertEqual(self.writes, [])

    def test_public_repository_is_rejected(self):
        self.metadata["private"] = False
        self.assertEqual(self.execute("--mode", "github", "--apply"), 2)
        self.assertEqual(self.writes, [])

    def test_stale_wrong_source_or_unsuccessful_runs_are_rejected(self):
        original = copy.deepcopy(self.run)
        for key, value in (
            ("head_sha", "b" * 40),
            ("head_branch", "feature"),
            ("event", "pull_request"),
            ("path", ".github/workflows/forged.yml"),
            ("status", "in_progress"),
            ("conclusion", "failure"),
            ("head_repository", {"full_name": "fork/android"}),
            ("created_at", "2026-09-22T17:00:00Z"),
            ("created_at", "2026-09-25T17:00:00Z"),
            ("run_attempt", None),
        ):
            with self.subTest(key=key, value=value):
                self.run = {**original, key: value}
                self.assertEqual(
                    self.execute("--mode", "k3s", "--smoke-run", "77", "--apply"), 2
                )
                self.assertEqual(self.writes, [])

    def test_success_on_hosted_or_wrong_group_is_not_evidence(self):
        original = copy.deepcopy(self.jobs)
        for key, value in (
            ("labels", ["ubuntu-24.04"]),
            ("runner_group_id", 1),
            ("runner_name", ""),
            ("conclusion", "skipped"),
        ):
            with self.subTest(key=key):
                self.jobs = copy.deepcopy(original)
                self.jobs[1][key] = value
                with self.assertRaises(mode.PreflightError):
                    self.verify()

    def test_missing_duplicate_or_old_attempt_jobs_are_rejected(self):
        original = copy.deepcopy(self.jobs)
        for jobs in (original[:1], original + [original[0]]):
            self.jobs = jobs
            with self.assertRaises(mode.PreflightError):
                self.verify()

    def test_release_labels_cannot_replace_workflow_restrictions(self):
        for key, value in (
            ("restricted_to_workflows", False),
            ("selected_workflows", []),
            ("allows_public_repositories", True),
            ("visibility", "all"),
        ):
            original = copy.deepcopy(self.groups)
            with self.subTest(key=key):
                self.groups[1][key] = value
                with self.assertRaises(mode.PreflightError):
                    self.verify()
            self.groups = original

    def test_unprotected_branch_rejected(self):
        self.tip["protected"] = False
        with self.assertRaises(mode.PreflightError):
            self.verify()

    def test_api_failure_prevents_write(self):
        with (
            patch.object(
                mode, "api", side_effect=mode.PreflightError("API unavailable")
            ),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                mode.main(["--repo", "ottplay-core", "--mode", "github", "--apply"]),
                2,
            )
        self.assertEqual(self.writes, [])

    def test_concurrent_operator_change_is_not_overwritten(self):
        with patch.object(
            mode,
            "repo_state",
            side_effect=[(self.metadata, None), (self.metadata, "k3s")],
        ):
            self.assertEqual(self.execute("--mode", "github", "--apply"), 2)
        self.assertEqual(self.writes, [])

    def test_pagination_includes_following_pages(self):
        with patch.object(
            mode, "api", side_effect=[{"jobs": [1] * 100}, {"jobs": [2]}]
        ) as api:
            self.assertEqual(mode.collection("path", "jobs"), [1] * 100 + [2])
            self.assertEqual(api.call_args_list[1].args[0], "path?per_page=100&page=2")

    def test_variable_changed_and_restored_is_not_overwritten(self):
        before = {
            "name": mode.VARIABLE,
            "value": "github",
            "updated_at": "2026-09-24T17:00:00Z",
        }
        after = {**before, "updated_at": "2026-09-24T18:00:00Z"}
        with patch.object(
            mode,
            "repo_state",
            side_effect=[(self.metadata, before), (self.metadata, after)],
        ):
            self.assertEqual(self.execute("--mode", "github", "--apply"), 2)
        self.assertEqual(self.writes, [])

    def test_smoke_is_revalidated_immediately_before_activation(self):
        with patch.object(
            mode,
            "verify_smoke",
            side_effect=[("old",), mode.PreflightError("Main advanced")],
        ) as verify:
            self.assertEqual(
                self.execute("--mode", "k3s", "--smoke-run", "77", "--apply"), 2
            )
            self.assertEqual(verify.call_count, 2)
        self.assertEqual(self.writes, [])

    def test_operator_update_during_second_smoke_check_is_not_overwritten(self):
        self.variable = "github"
        original_api = self.api
        branch_reads = 0
        updated_at = "2026-09-24T17:00:00Z"

        def competing_operator(path, method="GET", payload=None):
            nonlocal branch_reads, updated_at
            if method == "GET" and path.endswith("/branches/main"):
                branch_reads += 1
                if branch_reads == 2:
                    # Another operator explicitly retains hosted mode while the
                    # second smoke verification is still making API requests.
                    updated_at = "2026-09-24T18:00:00Z"
            result = original_api(path, method, payload)
            if (
                method == "GET"
                and path.split("?")[0].endswith("/actions/variables")
                and result["variables"]
            ):
                result["variables"][0]["updated_at"] = updated_at
            return copy.deepcopy(result)

        self.api = competing_operator
        self.assertEqual(
            self.execute("--mode", "k3s", "--smoke-run", "77", "--apply"), 2
        )
        self.assertEqual(branch_reads, 2)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.variable, "github")
