"""Exercise local release commands and generated workflow safety contracts offline."""

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
        ]:
            with self.assertRaises(ValueError):
                client.resolve_version({}, value)

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
                (root / platform).mkdir()
                (root / platform / "checksums.txt").write_text(platform)
            with (
                mock.patch.object(client, "ROOT", root),
                mock.patch(
                    "sys.argv", ["release.py", "collect", str(root), str(root / "out")]
                ),
            ):
                self.assertEqual(client.main(), 1)

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
