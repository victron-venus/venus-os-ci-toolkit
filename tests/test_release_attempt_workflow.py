"""Artifact collection must never select a successful package from a prior retry."""

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            "name-scoping helper does not verify them",
        ):
            with self.subTest(explanation=explanation):
                self.assertIn(explanation, guide)

    def test_upload_and_download_scope_preserves_other_artifacts_and_is_idempotent(self):
        steps = [
            {"uses": "actions/upload-artifact@pinned", "with": {"name": "release-assets-${{ matrix.os }}"}},
            {"uses": "actions/download-artifact@pinned", "with": {"pattern": "release-assets*"}},
            {"uses": "actions/download-artifact@pinned", "with": {"artifact-ids": "123"}},
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

    def test_broad_and_overlapping_download_patterns_cover_producer_inputs(self):
        for producer, download in (("native-amd64", "native-*"),
                                   ("container-platform-*", "container-*"),
                                   ("native-linux-*", "native-*-amd64")):
            with self.subTest(producer=producer, download=download):
                workflow = {"jobs": {"assemble": {"steps": [
                    {"uses": "actions/download-artifact@pin", "with": {"pattern": download}},
                ]}}}
                self.assertTrue(attempts.scope(workflow, "release-assets*", {producer}))
                self.assertEqual(workflow["jobs"]["assemble"]["steps"][0]["with"]["pattern"],
                                 download + attempts.SUFFIX)
                self.assertFalse(attempts.scope(workflow, "release-assets*", {producer}))

    def test_unknown_dynamic_in_run_download_requires_explicit_lineage(self):
        workflow = {"jobs": {"assemble": {"steps": [
            {"uses": "actions/download-artifact@pin", "with": {"name": "${{ needs.other.outputs.name }}"}},
        ]}}}
        with self.assertRaisesRegex(ValueError, "explicit lineage"):
            attempts.scope(workflow, "release-assets*", {"native-*"})

    def test_current_context_downloads_still_receive_the_attempt_suffix(self):
        for context in ({"run-id": "${{ github.run_id }}"},
                        {"repository": "${{github.repository}}"},
                        {"run-id": "${{github.run_id}}", "repository": "${{ github.repository }}"}):
            with self.subTest(context=context):
                options = {"pattern": "native-*", **context}
                workflow = {"jobs": {"build": {"steps": [
                    {"uses": "actions/download-artifact@pin", "with": options},
                ]}}}
                self.assertTrue(attempts.scope(workflow, "release-assets*", {"native-amd64"}))
                self.assertEqual(options["pattern"], "native-*" + attempts.SUFFIX)
                self.assertFalse(attempts.scope(workflow, "release-assets*", {"native-amd64"}))

    def test_unknown_or_literal_run_and_repository_overrides_fail_closed(self):
        for context in ({"run-id": "123"}, {"run-id": 123},
                        {"run-id": "${{ inputs.run_id }}"},
                        {"repository": "example/project"},
                        {"repository": "${{ inputs.repository }}"}):
            with self.subTest(context=context):
                workflow = {"jobs": {"build": {"steps": [
                    {"uses": "actions/download-artifact@pin", "with": {"pattern": "native-*", **context}},
                ]}}}
                with self.assertRaisesRegex(ValueError, "artifact IDs and explicit lineage"):
                    attempts.scope(workflow, "release-assets*", {"native-amd64"})

    def test_selector_free_or_ambiguous_id_downloads_fail_closed(self):
        for options in ({}, {"name": ""}, {"name": None}, {"pattern": " "},
                        {"pattern": 123}, {"name": 123, "pattern": "native-*"},
                        {"name": "native-amd64", "pattern": "native-*"},
                        {"artifact-ids": ""}, {"artifact-ids": 123},
                        {"artifact-ids": "123", "name": "native-amd64"},
                        {"artifact-ids": "123", "pattern": "native-*"}):
            with self.subTest(options=options):
                workflow = {"jobs": {"build": {"steps": [
                    {"uses": "actions/download-artifact@pin", "with": options},
                ]}}}
                with self.assertRaises(ValueError):
                    attempts.scope(workflow, "release-assets*", {"native-amd64"})

    def test_literal_generator_run_id_cannot_be_pinned_into_future_workflows(self):
        workflow = {"jobs": {"build": {"steps": [
            {"uses": "actions/download-artifact@pin", "with": {"run-id": "123", "pattern": "native-*"}},
        ]}}}
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "123"}):
            with self.assertRaisesRegex(ValueError, "artifact IDs and explicit lineage"):
                attempts.scope(workflow, "release-assets*", {"native-amd64"})


if __name__ == "__main__":
    unittest.main()
