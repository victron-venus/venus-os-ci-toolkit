"""Exercise fleet identity checks without invoking Git or changing remote state."""

import argparse
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "fleet", Path(__file__).resolve().parents[1] / "scripts/fleet.py"
)
fleet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleet)


class FleetIdentityTests(unittest.TestCase):
    """Keep fetch, push and policy identities bound to the selected repository."""

    def test_source_and_push_origin_must_both_match(self):
        """Reject mismatched origins and lookalike GitHub hosts before submission."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".release-policy.json").write_text(
                json.dumps({"repository": "owner/repo"})
            )
            for remotes, accepted in [
                (
                    [
                        "https://github.com/owner/repo.git",
                        "git@github.com:owner/repo.git",
                    ],
                    True,
                ),
                (
                    [
                        "https://github.com/other/repo.git",
                        "git@github.com:owner/repo.git",
                    ],
                    False,
                ),
                (
                    [
                        "https://github.com/owner/repo.git",
                        "git@github.com:other/repo.git",
                    ],
                    False,
                ),
                (
                    [
                        "https://github.com.example.com/owner/repo.git",
                        "git@github.com:owner/repo.git",
                    ],
                    False,
                ),
            ]:
                with (
                    self.subTest(remotes=remotes),
                    mock.patch.object(
                        fleet,
                        "run",
                        side_effect=[
                            subprocess.CompletedProcess([], 0, url) for url in remotes
                        ],
                    ),
                ):
                    if accepted:
                        fleet.validate_identity(root, {"repository": "owner/repo"})
                    else:
                        with self.assertRaises(ValueError):
                            fleet.validate_identity(root, {"repository": "owner/repo"})

    def test_wrong_policy_is_rejected_before_git_access(self):
        """Fail a conflicting local policy without consulting repository remotes."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".release-policy.json").write_text(
                json.dumps({"repository": "other/repo"})
            )
            with mock.patch.object(fleet, "run") as run, self.assertRaises(ValueError):
                fleet.validate_identity(root, {"repository": "owner/repo"})
            run.assert_not_called()


class GeneratedTrackingTests(unittest.TestCase):
    """Ensure commit preparation includes every required generated release file."""

    def setUp(self):
        """Create an isolated Git repository with a broad legacy release ignore rule."""
        # unittest cleanup owns the directory for the complete test lifetime.
        temporary = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        (self.root / ".release-policy.json").write_text(
            json.dumps({"repository": "owner/repo", "mode": "release"})
        )
        (self.root / ".gitignore").write_text("release.*\n")
        (self.root / "scripts").mkdir()
        (self.root / "scripts/release.py").write_text("# generated client\n")

    def test_untracked_ignored_client_blocks_submission(self):
        """Catch the required client that git add --all would leave out of a PR."""
        with self.assertRaisesRegex(ValueError, r"scripts/release\.py"):
            fleet.validate_generated_tracking(self.root)

    def test_tracked_ignored_client_remains_addable(self):
        """Permit tracked files whose names also match a legacy ignore pattern."""
        subprocess.run(
            ["git", "add", "--force", "scripts/release.py"], cwd=self.root, check=True
        )
        fleet.validate_generated_tracking(self.root)


class SubmissionTests(unittest.TestCase):
    """Exercise submission boundaries with mocked commands and no remote writes."""

    def setUp(self):
        """Use an explicitly selected app migration branch for each scenario."""
        self.root = Path("/isolated/repo")
        self.item = {"repository": "owner/repo", "branch": "ci/release-standard-apps"}
        self.row = {"repository": self.item["repository"]}

    def test_dry_run_only_reads_branch_and_status(self):
        """A dirty worktree cannot cause writes without the execute flag."""
        with mock.patch.object(
            fleet,
            "run",
            side_effect=[
                subprocess.CompletedProcess([], 0, self.item["branch"] + "\n"),
                subprocess.CompletedProcess([], 0, " M workflow.yml\n"),
            ],
        ) as run:
            fleet.submit_repository(self.root, self.item, False, self.row)
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [["git", "branch", "--show-current"], ["git", "status", "--short"]],
        )
        self.assertEqual(self.row["changes"], "M workflow.yml")
        self.assertIn("--execute", self.row["action"])
        self.assertNotIn("pr", self.row)

    def test_unexpected_branch_stops_before_staging(self):
        """Both the exact fleet branch and its migration prefix are mandatory."""
        for selected, current in [
            ("ci/release-standard-apps", "main"),
            ("ci/release-standard-apps", "ci/release-standard-other"),
            ("main", "main"),
        ]:
            with (
                self.subTest(selected=selected, current=current),
                mock.patch.object(
                    fleet,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, current),
                ) as run,
                self.assertRaisesRegex(ValueError, "unexpected branch"),
            ):
                fleet.submit_repository(
                    self.root, {**self.item, "branch": selected}, True, self.row
                )
            self.assertEqual(run.call_count, 1)

    def test_failed_preflight_never_reaches_submission(self):
        """A failed check must stop even an explicitly executed submission."""
        args = argparse.Namespace(root=self.root.parent, command="submit", execute=True)
        with (
            mock.patch.object(
                fleet, "check_repository", side_effect=ValueError("invalid origin")
            ),
            mock.patch.object(fleet, "submit_repository") as submit,
            self.assertRaisesRegex(ValueError, "invalid origin"),
        ):
            fleet.process_repository(self.root, self.item, args, self.row)
        submit.assert_not_called()

    def test_commit_failure_preserves_changes_and_never_pushes(self):
        """A rejected hook remains visible in the result and cannot be bypassed."""
        with (
            mock.patch.object(
                fleet,
                "run",
                side_effect=[
                    subprocess.CompletedProcess([], 0, self.item["branch"]),
                    subprocess.CompletedProcess([], 0, "M workflow.yml"),
                    subprocess.CompletedProcess([], 0),
                    subprocess.CalledProcessError(1, ["git", "commit"]),
                ],
            ) as run,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            fleet.submit_repository(self.root, self.item, True, self.row)
        self.assertEqual(self.row["changes"], "M workflow.yml")
        self.assertFalse(
            any(call.args[0][:2] == ["git", "push"] for call in run.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
