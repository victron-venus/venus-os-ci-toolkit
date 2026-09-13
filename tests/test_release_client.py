"""Exercise local release commands and generated workflow safety contracts offline."""

import ast
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def module(name):
    """Load a toolkit script directly without executing its command entry point."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


client = module("release")
installer = module("install_release")


class ClientTest(unittest.TestCase):
    """Check version resolution, asset collection and guarded dispatch behavior."""

    def test_all_declared_local_checks_run_in_order(self):
        """Do not silently omit security/integration after the default unit suite."""
        checks = ["bash scripts/ci.sh", "bash scripts/ci.sh security"]
        with mock.patch.object(client, "run") as run:
            client.local_checks({"local_checks": checks})
        self.assertEqual([call.args[-1] for call in run.call_args_list], checks)
        self.assertTrue(
            all(
                call.args[:5] == ("bash", "-e", "-o", "pipefail", "-c")
                for call in run.call_args_list
            )
        )

    def test_failed_local_check_stops_following_commands(self):
        """A failing validation cannot fall through into the next stage."""
        with (
            mock.patch.object(
                client, "run", side_effect=subprocess.CalledProcessError(1, "check")
            ) as run,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            client.local_checks({"local_checks": ["first", "second"]})
        self.assertEqual(run.call_count, 1)

    def test_malformed_local_checks_rejected_before_execution(self):
        """Validate the complete command list before running its first entry."""
        for checks in [[], "bash scripts/ci.sh", ["first", None], ["first", " "]]:
            with mock.patch.object(client, "run") as run, self.assertRaises(ValueError):
                client.local_checks({"local_checks": checks})
            run.assert_not_called()

    def test_validation_only_doctor_does_not_request_paid_environment(self):
        """Infrastructure needs no release reviewers or environment API call."""
        with mock.patch.object(client, "gh_json") as api:
            client.doctor({"repository": "owner/repo", "mode": "validation-only"})
        api.assert_not_called()

    def test_source_versions(self):
        """Resolve supported committed plain-text, JSON and TOML version sources."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(client, "ROOT", root):
                for name, data in [
                    ("version", "v1.2.3\n"),
                    ("package.json", '{"version":"1.2.3"}'),
                    ("Cargo.toml", '[workspace.package]\nversion = "1.2.3"'),
                    ("pyproject.toml", '[project]\nversion="1.2.3"'),
                ]:
                    (root / name).write_text(data)
                    self.assertEqual(
                        client.resolve_version({"version_file": name}), "1.2.3"
                    )

    def test_reject_requested_injection(self):
        """Reject noncanonical or injected explicit version arguments."""
        for value in [
            "v1.2.3",
            "1.2.3-rc.1",
            "1.2.03",
            "1.2.3\nchannel=stable",
            "$(id)",
            "1١.2.3",
            "1.2.3١",
        ]:
            with self.assertRaises(ValueError):
                client.resolve_version({}, value)
        self.assertIsNone(client.RC.fullmatch("v1.2.3-rc.1١"))

    def test_missing_version_is_not_invented(self):
        """Require a version source rather than inventing a release version."""
        with self.assertRaises(ValueError):
            client.resolve_version({})

    def test_collection_rejects_cross_platform_collisions(self):
        """Reject duplicate basenames instead of overwriting another platform asset."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".release-policy.json").write_text("{}")
            for platform in ["linux", "windows"]:
                directory = root / ".release-download" / platform
                directory.mkdir(parents=True)
                (directory / "checksums.txt").write_text(platform)
            with (
                mock.patch.object(client, "ROOT", root),
                mock.patch(
                    "sys.argv",
                    ["release.py", "collect", ".release-download", ".release-assets"],
                ),
            ):
                self.assertEqual(client.main(), 1)

    def test_collection_copies_only_fixed_download_directory(self):
        """Copy the exact artifact bytes without accepting arbitrary CLI paths."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / ".release-download" / "linux"
            source.mkdir(parents=True)
            (source / "app.tar.gz").write_bytes(b"approved bytes")
            with mock.patch.object(client, "ROOT", root):
                client.collect_assets()
                self.assertEqual(
                    (root / ".release-assets/app.tar.gz").read_bytes(),
                    b"approved bytes",
                )
                with self.assertRaises(FileExistsError):
                    client.collect_assets()
            with (
                mock.patch(
                    "sys.argv", ["release.py", "collect", "../secrets", "../outside"]
                ),
                self.assertRaises(SystemExit) as error,
            ):
                client.arguments()
            self.assertEqual(error.exception.code, 2)

    def test_collection_rejects_symlink_escape_before_creating_output(self):
        """Reject a download-root symlink rather than reading outside the checkout."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            (root / ".release-download").symlink_to(
                Path(temp), target_is_directory=True
            )
            with mock.patch.object(client, "ROOT", root), self.assertRaises(ValueError):
                client.collect_assets()
            self.assertFalse((root / ".release-assets").exists())

    def test_publication_commands_are_remote_workflow_dispatch(self):
        """Publish only by dispatching the default-branch workflow through the CLI."""
        config = {"repository": "owner/repo", "version": "1.2.3"}
        args = type(
            "Args", (), {"command": "rc", "version": "", "rc": "", "dry_run": False}
        )()
        with (
            mock.patch.object(
                client,
                "gh_json",
                side_effect=[{"default_branch": "main"}, {"sha": "a" * 40}],
            ),
            mock.patch.object(client, "run", side_effect=["a" * 40, "", ""]) as run,
        ):
            client.dispatch(args, config)
        final = run.call_args.args
        self.assertEqual(final[:4], ("gh", "workflow", "run", "release-pipeline.yml"))
        self.assertNotIn("release", final)

    def test_dirty_checkout_refused(self):
        """Reject stable requests from a dirty source checkout."""
        args = type(
            "Args", (), {"command": "stable", "rc": "v1.2.3-rc.1", "dry_run": False}
        )()
        with (
            mock.patch.object(
                client,
                "gh_json",
                side_effect=[{"default_branch": "main"}, {"sha": "a" * 40}],
            ),
            mock.patch.object(client, "run", side_effect=["a" * 40, " M source.py"]),
            self.assertRaises(ValueError),
        ):
            client.dispatch(args, {"repository": "owner/repo"})


class GeneratorTest(unittest.TestCase):
    """Verify generated validation and publication dependencies fail closed."""

    def setUp(self):
        """Provide a minimal policy with two real callable validators."""
        self.policy = {
            "repository": "owner/repo",
            "validation_workflows": ["ci.yml", "codeql.yml"],
        }

    def test_asset_restrictions_are_rendered_as_current_static_policy(self):
        """Older RC promotion uses the current retirement policy without rereading files."""
        restrictions = [
            {
                "suffixes": [".apk", ".aab"],
                "reason": "Android APK/AAB publication moved to the team's native repository",
            }
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = root / ".github/workflows/release-build.yml"
            adapter.parent.mkdir(parents=True)
            adapter.write_text("on: {workflow_call: {}}\njobs: {}\n", encoding="utf-8")
            for policy, expected in (
                (self.policy, ()),
                (
                    dict(self.policy, asset_restrictions=restrictions),
                    tuple(restrictions),
                ),
            ):
                source = installer.release_files(root, policy)[
                    "scripts/release_control.py"
                ]
                assignments = [
                    item
                    for item in ast.parse(source).body
                    if isinstance(item, ast.Assign)
                    and any(
                        isinstance(target, ast.Name)
                        and target.id == "ASSET_RESTRICTIONS"
                        for target in item.targets
                    )
                ]
                self.assertEqual(len(assignments), 1)
                self.assertEqual(ast.literal_eval(assignments[0].value), expected)

    def test_asset_restrictions_reject_ambiguous_or_unsafe_policy(self):
        """Require explicit literal suffixes and printable, bounded rejection reasons."""
        valid = {"suffixes": [".apk"], "reason": "Retired package"}
        invalid = (
            None,
            {},
            [None],
            [{}],
            [{**valid, "extra": True}],
            [{**valid, "suffixes": ".apk"}],
            [{**valid, "suffixes": []}],
            [{**valid, "suffixes": [".APK"]}],
            [{**valid, "suffixes": ["*.apk"]}],
            [{**valid, "suffixes": ["../apk"]}],
            [{**valid, "suffixes": [".apk", ".apk"]}],
            [valid, valid],
            [{**valid, "reason": ""}],
            [{**valid, "reason": " leading space"}],
            [{**valid, "reason": "first\nsecond"}],
            [{**valid, "reason": "nul\0byte"}],
            [{**valid, "reason": "x" * 501}],
        )
        with tempfile.TemporaryDirectory() as temp:
            for restrictions in invalid:
                with (
                    self.subTest(restrictions=restrictions),
                    self.assertRaises(ValueError),
                ):
                    installer.validate_policy(
                        Path(temp), dict(self.policy, asset_restrictions=restrictions)
                    )

    def test_release_publication_depends_on_build_and_checks(self):
        """Require completed validation and packaging before candidate publication."""
        jobs = installer.release(self.policy)["jobs"]
        self.assertEqual(jobs["gate"]["needs"], ["prepare", "checks", "build"])
        self.assertIn("gate", jobs["candidate"]["needs"])
        self.assertEqual(jobs["stable"]["environment"], "release")
        self.assertEqual(jobs["build"]["permissions"], {"contents": "read"})
        self.assertIn("!= 'success'", jobs["gate"]["steps"][0]["run"])

    def test_pr_and_queue_have_aggregate_gate(self):
        """Run an unconditional aggregate gate for PR and merge-queue validation."""
        result = installer.quality(self.policy)
        self.assertIn("pull_request", result["on"])
        self.assertIn("merge_group", result["on"])
        self.assertEqual(result["jobs"]["gate"]["name"], "CI gate")
        self.assertEqual(result["jobs"]["gate"]["if"], "${{ always() }}")

    def test_versioned_renderer_fixes_plan_before_build_and_protects_final_acceptance(
        self,
    ):
        """Freeze the release identity before compilation and gate final publication."""
        policy = dict(
            self.policy,
            mode="release",
            versioning={
                "schema": 1,
                "promotion": "final-build",
                "files": [{"path": "version", "format": "text"}],
            },
        )
        workflow = installer.release(policy)
        jobs = workflow["jobs"]
        self.assertEqual(
            jobs["prepare"]["permissions"], {"contents": "write", "actions": "read"}
        )
        self.assertIn(
            "release_versioned.py prepare", jobs["prepare"]["steps"][-2]["run"]
        )
        upload = jobs["prepare"]["steps"][-1]
        self.assertEqual(upload["with"]["path"], ".release-plan.json")
        self.assertTrue(upload["with"]["include-hidden-files"])
        self.assertEqual(
            jobs["build"]["with"]["release_plan_artifact"],
            "${{ needs.prepare.outputs.plan_artifact }}",
        )
        self.assertIn("gate", jobs["final"]["needs"])
        self.assertEqual(jobs["final"]["environment"], "release")
        self.assertIn("build == 'true'", jobs["final"]["if"])
        self.assertIn("build == 'false'", jobs["stable"]["if"])
        self.assertNotIn("environment", jobs["candidate"])
        self.assertTrue(
            any(
                "release_versioned.py publish" in step.get("run", "")
                for step in jobs["final"]["steps"]
            )
        )

    def test_legacy_renderer_does_not_gain_version_mutation_or_final_rebuild(self):
        """Preserve the existing byte-promotion workflow for legacy policies."""
        workflow = installer.release(self.policy)
        self.assertNotIn("final", workflow["jobs"])
        self.assertNotIn("permissions", workflow["jobs"]["prepare"])
        self.assertNotIn("release_plan_artifact", workflow["jobs"]["build"]["with"])
        self.assertTrue(
            any(
                "release_control.py promote" in step.get("run", "")
                for step in workflow["jobs"]["stable"]["steps"]
            )
        )
        self.assertFalse(
            any(
                "version_plan" in step.get("run", "")
                for job in workflow["jobs"].values()
                for step in job.get("steps", [])
            )
        )

    def test_versioned_build_adapter_requires_saved_plan_input(self):
        """Reject an incompatible reusable API before generating an invalid workflow."""
        policy = dict(
            self.policy,
            mode="release",
            versioning={
                "schema": 1,
                "promotion": "final-build",
                "files": [{"path": "version", "format": "text"}],
            },
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workflows = root / ".github/workflows"
            workflows.mkdir(parents=True)
            for name in self.policy["validation_workflows"]:
                (workflows / name).write_text(
                    installer.dump({"on": {"workflow_call": {}}, "jobs": {}}),
                    encoding="utf-8",
                )
            for declaration in (
                None,
                {},
                {"type": "string"},
                {"type": "string", "required": False},
                {"type": "boolean", "required": True},
                {"type": "number", "required": True},
            ):
                with self.subTest(declaration=declaration):
                    inputs = {
                        "version": {"type": "string"},
                        "channel": {"type": "string"},
                    }
                    if declaration is not None:
                        inputs["release_plan_artifact"] = declaration
                    (workflows / "release-build.yml").write_text(
                        installer.dump(
                            {"on": {"workflow_call": {"inputs": inputs}}, "jobs": {}}
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, "required string"):
                        installer.validate_workflow_adapters(root, policy)
                    installer.validate_workflow_adapters(root, self.policy)
            (workflows / "release-build.yml").write_text(
                installer.dump(
                    {
                        "on": {
                            "workflow_call": {
                                "inputs": {
                                    "release_plan_artifact": {
                                        "type": "string",
                                        "required": True,
                                    }
                                }
                            }
                        },
                        "jobs": {},
                    }
                ),
                encoding="utf-8",
            )
            installer.validate_workflow_adapters(root, policy)

    def test_committed_source_version_gate_is_only_added_for_opted_in_policies(self):
        """Only opted-in policies promise consistent declared source versions."""
        versioned = dict(
            self.policy,
            mode="release",
            versioning={
                "schema": 1,
                "promotion": "promote-bytes",
                "files": [{"path": "version", "format": "text"}],
            },
        )
        steps = installer.quality(versioned)["jobs"]["release-contracts"]["steps"]
        self.assertTrue(
            any(
                step.get("run") == "python3 scripts/version_plan.py check-base"
                for step in steps
            )
        )
        plain_steps = installer.quality(self.policy)["jobs"]["release-contracts"][
            "steps"
        ]
        self.assertFalse(
            any("version_plan" in step.get("run", "") for step in plain_steps)
        )

    def test_private_validation_uses_free_permissions_and_opt_in_nightly(self):
        """Private checks require no Code Security write scope or paid environment."""
        workflow = installer.quality(
            dict(self.policy, mode="validation-only", visibility="private")
        )
        for job in workflow["jobs"].values():
            self.assertNotIn("environment", job)
            self.assertNotIn("security-events", job.get("permissions", {}))
            self.assertIn("github.event_name != 'schedule'", job["if"])
            self.assertIn("NIGHTLY_CHECKS_ENABLED", job["if"])
        self.assertIn("always()", workflow["jobs"]["gate"]["if"])
        self.assertIn("workflow_dispatch", workflow["on"])

    def test_private_release_cannot_inherit_public_paid_environment_adapter(self):
        """A future private application needs an explicit supported approval adapter."""
        with self.assertRaisesRegex(ValueError, "manual approval adapter"):
            installer.validate_policy(
                Path("."), dict(self.policy, visibility="private")
            )

    def test_recursion_and_empty_checks_rejected(self):
        """Reject missing validators and unsafe recursive workflow references."""
        for workflows in [[], ["quality-gate.yml"], ["../ci.yml"]]:
            with self.assertRaises(ValueError):
                installer.quality(dict(self.policy, validation_workflows=workflows))

    def test_nightly_is_staggered_and_retained_evidence(self):
        """Retain hidden provenance artifacts and stagger scheduled fleet builds."""
        workflow = installer.release(self.policy)
        self.assertNotEqual(workflow["on"]["schedule"][0]["cron"].split()[0], "0")
        upload = workflow["jobs"]["candidate"]["steps"][-1]["with"]
        self.assertTrue(upload["include-hidden-files"])
        self.assertEqual(upload["name"], "release-evidence")
        self.assertEqual(upload["retention-days"], 90)

    def test_validation_only_rejects_stale_release_pipeline(self):
        """Reject a leftover publication workflow when the policy forbids releases."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".release-policy.json").write_text(
                json.dumps(dict(self.policy, mode="validation-only"))
            )
            workflow = root / ".github/workflows/release-pipeline.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: leftover publisher\n")
            with self.assertRaisesRegex(ValueError, "stale release-pipeline"):
                installer.render(root)

    def test_stale_pipeline_cannot_run_after_policy_disables_releases(self):
        """Stop a stale nightly workflow before it emits publication metadata."""
        script = installer.release(self.policy)["jobs"]["prepare"]["steps"][-1]["run"]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            event = root / "event.json"
            event.write_text(
                json.dumps(
                    {
                        "repository": {"default_branch": "main"},
                        "inputs": {"channel": "nightly"},
                    }
                )
            )
            (root / ".release-policy.json").write_text(
                json.dumps(dict(self.policy, mode="validation-only"))
            )
            output = root / "output"
            result = subprocess.run(
                ["bash", "-e", "-c", script],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                env=dict(
                    os.environ,
                    GITHUB_EVENT_PATH=str(event),
                    GITHUB_REF="refs/heads/main",
                    GITHUB_EVENT_NAME="workflow_dispatch",
                    GITHUB_OUTPUT=str(output),
                ),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not permit releases", result.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
