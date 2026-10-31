"""Select one generated release attempt before collecting and verifying packages."""

# Imports use the same scripts as generated consumers; all payloads are synthetic.
# pylint: disable=wrong-import-position,missing-class-docstring,missing-function-docstring

import fnmatch
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install_release
import release as release_client
from release_attempt_workflow import SUFFIX
import version_plan
import version_receipt


# Each test owns its source, artifact service inventory, and downloaded checkout.
# pylint: disable-next=too-many-instance-attributes
class AttemptSelectionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repository = "example/project"
        self.run_id = 123
        self.source_sha = "a" * 40
        self.policy = {
            "repository": self.repository,
            "mode": "release",
            "validation_workflows": ["ci.yml"],
            "version_file": "package.json",
            "versioning": {
                "schema": 1,
                "promotion": "final-build",
                "files": [{"path": "package.json", "format": "json",
                           "field": "version", "value": "full"}],
                "artifacts": [
                    {"path": f"package-{platform}.json", "format": "json",
                     "field": "version", "value": "full"}
                    for platform in ("a", "b")
                ],
            },
        }
        (self.root / "package.json").write_text('{"version":"1.2.3"}', encoding="utf-8")
        self.plan = version_plan.create_plan("1.2.3", "rc", 1, self.source_sha, self.policy, 51)
        files = version_plan.sync_versions(self.root, self.policy, self.plan)
        self.plan_path = self.root / ".release-plan.json"
        self.plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        self.inputs_path = self.root / ".release-inputs.json"
        self.inputs_path.write_text(json.dumps({
            "source_sha": self.source_sha,
            "plan_sha256": version_plan.plan_digest(self.plan),
            "effective_inputs_sha256": version_plan.effective_inputs_digest(files),
            "files": files,
        }), encoding="utf-8")
        self.inventory = self.root / "artifact-service"
        self.inventory.mkdir()
        self.assets = self.root / ".release-assets"
        self.enterContext(patch.object(version_receipt, "capture_toolchain",
                                       return_value={"python": "synthetic-3.13"}))

        # Obtain both ends from production generators instead of restating the suffix.
        pipeline = install_release.release(self.policy)
        download = next(step["with"] for step in pipeline["jobs"]["candidate"]["steps"]
                        if step.get("uses", "").startswith("actions/download-artifact@")
                        and "pattern" in step.get("with", {}))
        self.assertIs(download["merge-multiple"], False)
        self.download_pattern = download["pattern"]
        workflows = self.root / ".github/workflows"
        workflows.mkdir(parents=True)
        adapter_path = ".github/workflows/release-build.yml"
        (self.root / adapter_path).write_text(json.dumps({"jobs": {"build": {"steps": [{
            "uses": "actions/upload-artifact@pin",
            "with": {"name": "release-assets-${{ matrix.platform }}"},
        }]}}}), encoding="utf-8")
        rendered = install_release.attempt_scoped_build_adapters(self.root, self.policy)
        adapter = install_release.yaml.load(rendered[adapter_path],
                                             Loader=install_release.WorkflowLoader)
        self.upload_name = adapter["jobs"]["build"]["steps"][0]["with"]["name"]

    def expand(self, template, attempt, platform=""):
        result = template.replace("${{ github.run_id }}", str(self.run_id))
        result = result.replace("${{ github.run_attempt }}", str(attempt))
        result = result.replace("${{ matrix.platform }}", platform)
        self.assertNotIn("${{", result, "Fixture must resolve every generated expression")
        return result

    def artifact(self, platform, attempt):
        directory = self.inventory / self.expand(self.upload_name, attempt, platform)
        directory.mkdir()
        payload = {"version": version_plan.projected_value(self.plan, {"value": "full"}),
                   "attempt": attempt, "platform": platform}
        (directory / f"package-{platform}.json").write_text(json.dumps(payload), encoding="utf-8")
        with patch.dict(os.environ, {
            "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": self.repository,
            "GITHUB_RUN_ID": str(self.run_id), "GITHUB_RUN_ATTEMPT": str(attempt),
        }):
            version_receipt.create_receipt(
                self.plan_path, self.inputs_path, directory,
                directory / f"release-inputs-{platform}.json",
            )
        return directory

    def materialize(self, attempt):
        """Model only name selection/download; the real collector does all flattening."""
        download = self.root / ".release-download"
        download.mkdir()
        pattern = self.expand(self.download_pattern, attempt)
        selected = sorted(path for path in self.inventory.iterdir()
                          if fnmatch.fnmatchcase(path.name, pattern))
        for path in selected:
            shutil.copytree(path, download / path.name)
        return selected

    def collect(self, attempt):
        selected = self.materialize(attempt)
        with patch.object(release_client, "ROOT", self.root):
            release_client.collect_assets()
        return selected

    def verify_receipts(self, attempt):
        payloads = [{"name": path.name, "size": path.stat().st_size,
                     "sha256": version_receipt.sha256(path.read_bytes())}
                    for path in sorted(self.assets.iterdir())]
        expected = version_receipt.producer_identity(
            self.repository, self.run_id, attempt, self.source_sha)
        return version_receipt.verify_receipts(
            self.assets, self.plan, payloads, self.policy, expected)

    def test_first_attempt_collects_complete_current_packages(self):
        expected = [self.artifact(platform, 1) for platform in ("a", "b")]
        self.assertEqual(self.collect(1), expected)
        self.assertEqual(len(self.verify_receipts(1)), 2)
        self.assertEqual(len(version_receipt.verify_declared_artifacts(
            self.assets, self.policy, self.plan)), 2)

    def test_full_rerun_selects_current_bytes_without_removing_old_artifacts(self):
        for attempt in (1, 2):
            for platform in ("a", "b"):
                self.artifact(platform, attempt)
        before = {path.relative_to(self.inventory): path.read_bytes()
                  for path in self.inventory.rglob("*") if path.is_file()}
        self.assertEqual(len(self.collect(2)), 2)
        for platform in ("a", "b"):
            payload = json.loads((self.assets / f"package-{platform}.json").read_text())
            self.assertEqual(payload["attempt"], 2)
        self.assertEqual(len(self.verify_receipts(2)), 2)
        self.assertEqual(len(version_receipt.verify_declared_artifacts(
            self.assets, self.policy, self.plan)), 2)
        self.assertEqual(before, {path.relative_to(self.inventory): path.read_bytes()
                                  for path in self.inventory.rglob("*") if path.is_file()})

    def test_migrated_nested_producers_feed_both_publishers_only_current_attempt(self):
        pipeline_path = ".github/workflows/release-pipeline.yml"
        build_path = ".github/workflows/release-build.yml"
        native_path = ".github/workflows/release-native.yml"
        (self.root / ".release-policy.json").write_text(json.dumps(self.policy), encoding="utf-8")
        # Start with a real generated consumer, before attempt suffixes were adopted.
        old_pipeline = install_release.dump(install_release.release(self.policy)).replace(SUFFIX, "")
        (self.root / pipeline_path).write_text(old_pipeline, encoding="utf-8")
        build = {"on": {"workflow_call": {}}, "jobs": {
            "native": {"uses": "./" + native_path},
            "assemble": {"needs": "native", "strategy": {"matrix": {"platform": ["a", "b"]}},
                         "steps": [
                             {"uses": "actions/download-artifact@pin", "with": {"pattern": "native-*"}},
                             {"uses": "actions/upload-artifact@pin", "with": {
                                 "name": "release-assets-${{ matrix.platform }}"}},
                         ]},
        }}
        native = {"on": {"workflow_call": {}}, "jobs": {"compile": {
            "strategy": {"matrix": {"platform": ["a", "b"]}}, "steps": [
                {"uses": "actions/upload-artifact@pin", "with": {"name": "native-${{ matrix.platform }}"}},
            ],
        }}}
        for name, workflow in ((build_path, build), (native_path, native)):
            (self.root / name).write_text(install_release.dump(workflow), encoding="utf-8")
        migrated = install_release.render_release_attempts(self.root)
        self.assertEqual(set(migrated), {pipeline_path, build_path, native_path})
        parsed = {name: install_release.yaml.load(source, Loader=install_release.WorkflowLoader)
                  for name, source in migrated.items()}
        assembly = parsed[build_path]["jobs"]["assemble"]["steps"]
        native_upload = next(step["with"]["name"]
                             for step in parsed[native_path]["jobs"]["compile"]["steps"]
                             if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.upload_name = next(step["with"]["name"] for step in assembly
                                if step.get("uses", "").startswith("actions/upload-artifact@"))
        native_download = next(step["with"]["pattern"] for step in assembly
                               if step.get("uses", "").startswith("actions/download-artifact@"))

        native_inventory = self.root / "native-service"
        native_inventory.mkdir()
        for attempt in (1, 2):
            for platform in ("a", "b"):
                artifact = native_inventory / self.expand(native_upload, attempt, platform)
                artifact.mkdir()
                (artifact / "input.json").write_text(
                    json.dumps({"platform": platform, "attempt": attempt}), encoding="utf-8")
        native_pattern = self.expand(native_download, 2)
        current_inputs = sorted(path for path in native_inventory.iterdir()
                                if fnmatch.fnmatchcase(path.name, native_pattern))
        self.assertEqual(len(current_inputs), 2)
        for platform in ("a", "b"):
            self.artifact(platform, 1)
        for artifact in current_inputs:
            supplied = json.loads((artifact / "input.json").read_text(encoding="utf-8"))
            self.assertEqual(supplied["attempt"], 2)
            self.artifact(supplied["platform"], supplied["attempt"])
        before = {path.relative_to(self.inventory): path.read_bytes()
                  for path in self.inventory.rglob("*") if path.is_file()}

        for publisher in ("candidate", "final"):
            with self.subTest(publisher=publisher):
                download = next(step["with"] for step in parsed[pipeline_path]["jobs"][publisher]["steps"]
                                if step.get("uses", "").startswith("actions/download-artifact@")
                                and "pattern" in step.get("with", {}))
                self.assertIs(download["merge-multiple"], False)
                self.download_pattern = download["pattern"]
                self.assertEqual(len(self.collect(2)), 2)
                self.assertEqual(len(self.verify_receipts(2)), 2)
                self.assertEqual(len(version_receipt.verify_declared_artifacts(
                    self.assets, self.policy, self.plan)), 2)
                self.assertEqual(before, {path.relative_to(self.inventory): path.read_bytes()
                                          for path in self.inventory.rglob("*") if path.is_file()})
                shutil.rmtree(self.assets)
                shutil.rmtree(self.root / ".release-download")
        self.assertEqual(len(list(native_inventory.iterdir())), 4)

    def test_failed_job_only_rerun_cannot_fill_empty_current_set_from_old_attempt(self):
        for platform in ("a", "b"):
            self.artifact(platform, 1)
        self.assertEqual(self.materialize(2), [])
        with patch.object(release_client, "ROOT", self.root):
            with self.assertRaisesRegex(ValueError, "No build assets were downloaded"):
                release_client.collect_assets()
        self.assertFalse(self.assets.exists())

    def test_partial_rerun_requires_missing_declared_package_not_old_platform(self):
        for platform in ("a", "b"):
            self.artifact(platform, 1)
        current = self.artifact("a", 2)
        self.assertEqual(self.collect(2), [current])
        self.assertFalse((self.assets / "package-b.json").exists())
        # Receipts cover the actual subset; only policy declarations require B.
        self.assertEqual(len(self.verify_receipts(2)), 1)
        with self.assertRaisesRegex(ValueError, "metadata target is missing: package-b.json"):
            version_receipt.verify_declared_artifacts(self.assets, self.policy, self.plan)

    def test_current_artifact_name_does_not_authorize_stale_receipt_identity(self):
        for platform in ("a", "b"):
            self.artifact(platform, 2)
        self.collect(2)
        path = self.assets / "release-inputs-a.json"
        original = path.read_text(encoding="utf-8")
        for field, value in (("repository", "other/project"), ("run_id", 124),
                             ("run_attempt", 1), ("source_sha", "b" * 40)):
            with self.subTest(field=field):
                stale = json.loads(original)
                stale["producer"][field] = value
                path.write_text(json.dumps(stale), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "different package producer attempt"):
                    self.verify_receipts(2)

    def test_duplicate_current_payloads_remain_ambiguous_even_with_identical_bytes(self):
        original = self.artifact("a", 2)
        shutil.copytree(original, self.inventory / self.expand(self.upload_name, 2, "copy"))
        self.assertEqual(len(self.materialize(2)), 2)
        with patch.object(release_client, "ROOT", self.root):
            with self.assertRaisesRegex(ValueError, "Duplicate asset basename"):
                release_client.collect_assets()
        self.assertFalse(self.assets.exists())


if __name__ == "__main__":
    unittest.main()
