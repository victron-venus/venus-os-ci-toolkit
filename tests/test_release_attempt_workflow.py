"""Artifact collection must never select a successful package from a prior retry."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install_release
import release_attempt_workflow as attempts


class AttemptWorkflowTests(unittest.TestCase):
    def test_generated_runbook_retains_existing_publication_safeguards(self):
        policy = json.loads((install_release.ROOT / ".release-policy.json").read_text())
        guide = install_release.operator_guide(policy)
        for explanation in (
            "The required ordinary **CI gate** uses `always()`",
            "Assets are uploaded to the draft's known release ID",
            "Reuse one budget across the",
            "wait across both updates and rechecks execution identity",
            "must not claim a new",
            "Re-run all jobs",
        ):
            with self.subTest(explanation=explanation):
                self.assertIn(explanation, guide)

    def test_upload_and_download_scope_preserves_other_artifacts_and_is_idempotent(self):
        steps = [
            {"uses": "actions/upload-artifact@pinned", "with": {"name": "release-assets-${{ matrix.os }}"}},
            {"uses": "actions/download-artifact@pinned", "with": {"pattern": "release-assets*"}},
            {"uses": "actions/download-artifact@pinned", "with": {"name": "browser-candidate", "artifact-ids": "123"}},
            {"uses": "actions/upload-artifact@pinned", "with": {"name": "diagnostics"}},
        ]
        workflow = {"jobs": {"build": {"steps": copy.deepcopy(steps)}}}
        self.assertTrue(attempts.scope(workflow, "release-assets*"))
        scoped = workflow["jobs"]["build"]["steps"]
        self.assertEqual(scoped[0]["with"]["name"], steps[0]["with"]["name"] + attempts.SUFFIX)
        self.assertEqual(scoped[1]["with"]["pattern"], "release-assets*" + attempts.SUFFIX)
        self.assertEqual(scoped[2:], steps[2:])
        self.assertFalse(attempts.scope(workflow, "release-assets*"))

    def test_versioned_publishers_and_signer_select_current_attempt(self):
        policy = {"repository": "example/project", "mode": "release", "sign_releases": True,
                  "versioning": {"schema": 1}, "validation_workflows": ["ci.yml"]}
        jobs = install_release.release(policy)["jobs"]
        for name in ("candidate", "final", "sign"):
            downloads = [step["with"] for step in jobs[name]["steps"]
                         if step.get("uses", "").startswith("actions/download-artifact@")]
            self.assertIn({"pattern": "release-assets*" + attempts.SUFFIX,
                           "path": ".release-download", "merge-multiple": False}, downloads)
        sign_upload = next(step["with"] for step in jobs["sign"]["steps"]
                           if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(sign_upload["name"], "release-assets-signature" + attempts.SUFFIX)

    def test_nested_build_adapters_are_scoped_without_touching_candidate_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflows = root / ".github/workflows"
            workflows.mkdir(parents=True)
            parent = {"on": {"workflow_call": {}}, "jobs": {
                "native": {"uses": "./.github/workflows/native.yml"}}}
            child = {"on": {"workflow_call": {}}, "jobs": {"build": {"steps": [
                {"uses": "actions/upload-artifact@pin", "with": {"name": "release-assets-linux"}},
                {"uses": "actions/download-artifact@pin", "with": {"artifact-ids": "${{ inputs.candidate }}"}},
            ]}}}
            (workflows / "release-build.yml").write_text(json.dumps(parent))
            (workflows / "native.yml").write_text(json.dumps(child))
            files = install_release.attempt_scoped_build_adapters(root, {})
            self.assertEqual(set(files), {".github/workflows/native.yml"})
            result = install_release.yaml.load(files[".github/workflows/native.yml"],
                                              Loader=install_release.WorkflowLoader)
            steps = result["jobs"]["build"]["steps"]
            self.assertEqual(steps[0]["with"]["name"], "release-assets-linux" + attempts.SUFFIX)
            self.assertEqual(steps[1], child["jobs"]["build"]["steps"][1])

    def test_native_intermediates_cannot_be_reused_by_a_new_assembly_attempt(self):
        native = {"jobs": {"native": {"steps": [
            {"uses": "actions/upload-artifact@pin", "with": {"name": "container-platform-${{ matrix.arch }}"}},
        ]}}}
        assembly = {"jobs": {"assemble": {"steps": [
            {"uses": "actions/download-artifact@pin", "with": {"name": "container-platform-amd64"}},
            {"uses": "actions/download-artifact@pin", "with": {"name": "container-platform-arm64"}},
            {"uses": "actions/download-artifact@pin", "with": {"name": "${{ inputs.release_plan_artifact }}"}},
            {"uses": "actions/download-artifact@pin", "with": {"artifact-ids": "123", "run-id": "789"}},
        ]}}}
        selectors = attempts.producer_selectors(native)
        self.assertEqual(selectors, {"container-platform-*"})
        self.assertTrue(attempts.scope(native, "release-assets*", selectors))
        self.assertTrue(attempts.scope(assembly, "release-assets*", selectors))
        steps = assembly["jobs"]["assemble"]["steps"]
        for architecture, step in zip(("amd64", "arm64"), steps):
            self.assertEqual(step["with"]["name"], "container-platform-" + architecture + attempts.SUFFIX)
        self.assertEqual(steps[2]["with"]["name"], "${{ inputs.release_plan_artifact }}")
        self.assertEqual(steps[3]["with"]["artifact-ids"], "123")
        self.assertFalse(attempts.scope(assembly, "release-assets*", selectors))


if __name__ == "__main__":
    unittest.main()
