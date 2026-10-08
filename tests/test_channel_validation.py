"""Render channel validation and exercise the consumer's actual workflow contracts."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import install_release as installer  # noqa: E402
import workflow_contracts as contracts  # noqa: E402


class ChannelValidationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = self.root / ".github/workflows"
        self.directory.mkdir(parents=True)
        self.policy = {
            "repository": "owner/repo",
            "mode": "release",
            "visibility": "public",
            "default_branch": "main",
            "single_entry_ci": True,
            "validation_workflows": ["ci.yml", "native.yml", "codeql.yml"],
            "change_scope": {"always_validate_workflows": ["codeql.yml"]},
            "version_file": "version",
            "versioning": {
                "schema": 1,
                "promotion": "promote-bytes",
                "files": [
                    {"path": "version", "format": "text", "value": "package"},
                ],
            },
            "channel_validation_workflows": {"beta": ["beta-checks.yml"]},
        }
        (self.root / "version").write_text("1.2.3\n")
        for name in (
            *self.policy["validation_workflows"],
            "beta-checks.yml",
            "release-build.yml",
        ):
            self.write(
                name,
                {
                    "on": {
                        "workflow_call": {
                            "inputs": {
                                "release_plan_artifact": {
                                    "type": "string",
                                    "required": True,
                                },
                            }
                        }
                        if name == "release-build.yml"
                        else {}
                    },
                    "permissions": {"contents": "read"},
                    "jobs": {
                        "check": {
                            "runs-on": "ubuntu-latest",
                            "timeout-minutes": 5,
                            "steps": [{"run": "true"}],
                        }
                    },
                },
            )

    def write(self, name, workflow):
        (self.directory / name).write_text(installer.dump(workflow))

    def render(self):
        (self.root / ".release-policy.json").write_text(json.dumps(self.policy))
        files = installer.render(self.root)
        for name, value in files.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value)
        return files

    def workflows(self):
        return {
            path.name: yaml.load(path.read_text(), Loader=contracts.UniqueKeyLoader)
            for path in self.directory.glob("*.yml")
        }

    def test_rendered_consumer_keeps_pr_full_and_build_adapter_unchanged(self):
        original = copy.deepcopy(self.policy)
        del original["channel_validation_workflows"]
        self.assertEqual(
            installer.quality(original, self.root),
            installer.quality(self.policy, self.root),
        )
        build = (self.directory / "release-build.yml").read_bytes()
        files = self.render()
        self.assertNotIn(".github/workflows/release-build.yml", files)
        self.assertEqual((self.directory / "release-build.yml").read_bytes(), build)
        self.assertIn("scripts/release_validation.py", files)
        self.assertIn(".github/release-tests/test_release_validation.py", files)
        contracts.validate(self.root)
        pipeline = installer.release(self.policy)["jobs"]
        self.assertEqual(
            pipeline["checks"]["with"],
            {"channel": "${{ needs.prepare.outputs.channel }}"},
        )
        default = installer.release(original)["jobs"]
        for name in ("build", "gate", "candidate", "stable", "final"):
            self.assertEqual(pipeline[name], default[name])

    def test_no_opt_in_preserves_the_existing_release_workflow(self):
        del self.policy["channel_validation_workflows"]
        files = self.render()
        self.assertNotIn(".github/workflows/release-quality-gate.yml", files)
        pipeline = installer.release(self.policy)
        self.assertEqual(
            pipeline["jobs"]["checks"]["uses"], "./.github/workflows/quality-gate.yml"
        )
        self.assertEqual(pipeline["jobs"]["checks"]["with"], {"force-full": True})
        contracts.validate(self.root)

    def test_coverage_only_refreshes_both_gates_and_preserves_release_engine(self):
        self.render()
        self.policy["coverage"] = {
            "toolkit_ref": "a" * 40,
            "reports": [
                {
                    "name": "unit",
                    "workflow": "ci.yml",
                    "job": "check",
                    "path": "coverage.xml",
                    "format": "cobertura",
                    "required": False,
                }
            ],
        }
        (self.root / ".release-policy.json").write_text(json.dumps(self.policy))
        before = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        files = installer.render_coverage(self.root)
        self.assertIn(".github/workflows/release-quality-gate.yml", files)
        for name, content in files.items():
            (self.root / name).write_text(content)
        for name in set(before) - set(files):
            self.assertEqual((self.root / name).read_bytes(), before[name], name)
        contracts.validate(self.root)
        self.assertEqual(installer.render_coverage(self.root), files)
        jobs = self.workflows()["release-quality-gate.yml"]["jobs"]
        self.assertIn("coverage-unit", jobs["gate"]["needs"])
        self.assertIn("inputs.channel != 'beta'", jobs["coverage-unit"]["if"])

    def test_runner_only_keeps_release_validators_on_ci_profile(self):
        self.render()
        before = self.workflows()
        files = installer.render_runners(self.root)
        for name, content in files.items():
            (self.root / name).write_text(content)
        contracts.validate(self.root)
        self.assertEqual(installer.render_runners(self.root), files)
        after = self.workflows()
        for name in ("quality-gate.yml", "release-quality-gate.yml", "beta-checks.yml"):
            for key, job in after[name]["jobs"].items():
                if "runs-on" in job:
                    self.assertIn("CI_RUNNER_LABELS", job["runs-on"])
                    self.assertNotIn("CI_RUNNER_RELEASE_LABELS", job["runs-on"])
                    original = copy.deepcopy(before[name]["jobs"][key])
                    original["runs-on"] = job["runs-on"]
                    self.assertEqual(job, original)
        self.assertEqual(set(files), {".github/workflows/" + name for name in before})

    def test_each_gate_or_routing_bypass_is_rejected(self):
        self.render()
        source = self.workflows()

        def remove_full(jobs):
            del jobs["check-1"]

        def skip_full(jobs):
            jobs["check-2"]["if"] = "false"

        def omit_beta_need(jobs):
            jobs["gate"]["needs"].remove("beta-check-0")

        def skip_beta(jobs):
            jobs["beta-check-0"]["if"] = "false"

        def unconditional_gate(jobs):
            jobs["gate"]["steps"][-1]["run"] = "true"

        for mutate in (
            remove_full,
            skip_full,
            omit_beta_need,
            skip_beta,
            unconditional_gate,
        ):
            with self.subTest(mutation=mutate.__name__):
                data = copy.deepcopy(source)
                mutate(data["release-quality-gate.yml"]["jobs"])
                with self.assertRaises(ValueError):
                    contracts.validate_release_channels(self.policy, data)
        source["release-pipeline.yml"]["jobs"]["checks"]["with"]["channel"] = "beta"
        with self.assertRaises(ValueError):
            contracts.validate_release_channels(self.policy, source)

    def test_missing_recursive_or_independently_triggered_beta_workflow_is_rejected(
        self,
    ):
        (self.directory / "beta-checks.yml").unlink()
        with self.assertRaises(OSError):
            self.render()
        self.write(
            "beta-checks.yml",
            {
                "on": {"workflow_call": {}},
                "jobs": {
                    "again": {"uses": "./.github/workflows/beta-checks.yml"},
                },
            },
        )
        with self.assertRaises(ValueError):
            self.render()
        self.write(
            "beta-checks.yml",
            {"on": {"workflow_call": {}, "pull_request": {}}, "jobs": {}},
        )
        self.render()
        with self.assertRaises(ValueError):
            contracts.validate(self.root)


if __name__ == "__main__":
    unittest.main()
