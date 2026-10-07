"""Exercise real producer adaptation and the generated coverage permission boundary."""

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("install_release", ROOT / "scripts/install_release.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
REF = "a" * 40
CONTRACT_SPEC = importlib.util.spec_from_file_location("workflow_contracts", ROOT / "scripts/workflow_contracts.py")
contracts = importlib.util.module_from_spec(CONTRACT_SPEC)
CONTRACT_SPEC.loader.exec_module(contracts)


class CoveragePolicyTests(unittest.TestCase):
    """A coverage profile must never change a test command or grant it OIDC."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / ".github/workflows").mkdir(parents=True)
        self.report = {
            "name": "python", "workflow": "ci.yml", "job": "tests",
            "path": "coverage.xml", "format": "cobertura", "required": False,
        }
        self.policy = {
            "repository": "owner/repo", "mode": "validation-only", "visibility": "public",
            "single_entry_ci": True, "validation_workflows": ["ci.yml"],
            "coverage": {"toolkit_ref": REF, "reports": [self.report]},
        }
        self.workflow = {
            "on": {"workflow_call": {}}, "permissions": {"contents": "read"},
            "jobs": {"tests": {"runs-on": "ubuntu-latest", "timeout-minutes": 10,
                                 "steps": [{"run": "pytest --cov=app --cov-fail-under=73 --cov-report=xml"}]}},
        }
        self.write_workflow()

    def write_workflow(self):
        (self.root / ".github/workflows/ci.yml").write_text(yaml.safe_dump(self.workflow, sort_keys=False))

    def test_opt_out_preserves_generated_workflows_and_permissions(self):
        del self.policy["coverage"]
        jobs = installer.quality(self.policy, self.root)["jobs"]
        self.assertFalse(any(name.startswith("coverage-") for name in jobs))
        self.assertNotIn("id-token", jobs["check-0"]["permissions"])
        self.assertEqual(installer.coverage_adapters(self.root, self.policy), {})
        self.policy["mode"] = "release"
        self.assertNotIn("id-token", installer.release(self.policy)["jobs"]["checks"]["permissions"])

    def test_only_uploader_and_outer_release_call_receive_oidc(self):
        jobs = installer.quality(self.policy, self.root)["jobs"]
        self.assertNotIn("id-token", jobs["check-0"]["permissions"])
        upload = jobs["coverage-python"]
        self.assertEqual(upload["needs"], ["scope", "check-0"])
        self.assertEqual(upload["permissions"], {"contents": "read", "actions": "read", "id-token": "write"})
        self.assertEqual(upload["uses"], installer.TOOLKIT + "/.github/workflows/coverage-upload.yml@" + REF)
        self.assertIn("coverage-python", jobs["gate"]["needs"])
        self.policy["mode"] = "release"
        release = installer.release(self.policy)
        self.assertEqual(release["jobs"]["checks"]["permissions"]["id-token"], "write")
        for name in ("build", "candidate"):
            self.assertNotIn("id-token", release["jobs"][name].get("permissions", {}))

    def test_gate_accepts_only_documented_skip_and_rejects_missing_upload(self):
        self.policy["change_scope"] = {"documentation_paths": ["docs"], "always_validate_workflows": ["ci.yml"]}
        jobs = installer.quality(self.policy, self.root)["jobs"]
        run = next(step["run"] for step in jobs["gate"]["steps"] if "run" in step)
        results = {name: {"result": "success"} for name in jobs if name != "gate"}
        results["scope"]["outputs"] = {"run": "false", "reason": "documentation-only"}
        results["coverage-python"]["result"] = "skipped"
        def execute():
            return subprocess.run(["bash", "-c", run], env={**os.environ, "RESULTS": json.dumps(results)}, capture_output=True, text=True, check=False)
        self.assertEqual(execute().returncode, 0)
        results["scope"]["outputs"] = {"run": "true", "reason": "required"}
        self.assertNotEqual(execute().returncode, 0)
        results["coverage-python"]["result"] = "success"
        self.assertEqual(execute().returncode, 0)

    def test_custom_export_is_idempotent_preserving_commands_and_yaml_types(self):
        self.report["path"] = "backend/coverage.xml"
        before = copy.deepcopy(self.workflow["jobs"]["tests"]["steps"])
        files = installer.coverage_adapters(self.root, self.policy)
        path, text = next(iter(files.items()))
        rewritten = yaml.load(text, Loader=installer.WorkflowLoader)
        self.assertIn("on", rewritten)
        self.assertEqual(rewritten["jobs"]["tests"]["steps"][:-1], before)
        exported = rewritten["jobs"]["tests"]["steps"][-1]
        self.assertEqual(exported["with"]["path"], "backend/coverage.xml")
        self.assertEqual(exported["with"]["name"], "coverage-python-${{ github.run_attempt }}")
        self.assertEqual(exported["with"]["if-no-files-found"], "error")
        self.assertNotIn("id-token", rewritten["permissions"])
        (self.root / path).write_text(text)
        self.assertEqual(installer.coverage_adapters(self.root, self.policy), files)
        self.assertEqual(installer.coverage_jobs(self.policy)["coverage-python"]["with"]["report-file"], "coverage.xml")

    def test_shared_python_and_go_export_without_duplicate_tests(self):
        for language, filename, format_name in [("python", "coverage.xml", "cobertura"), ("go", "coverage.out", "go")]:
            with self.subTest(language=language):
                self.workflow["jobs"]["tests"] = {
                    "uses": installer.TOOLKIT + f"/.github/workflows/{language}-ci.yml@" + "b" * 40,
                    "with": {"working-directory": "src", "coverage-threshold": 73, "test-args": "existing"},
                }
                self.write_workflow()
                self.report.update(path="src/" + filename, format=format_name)
                rendered = next(iter(installer.coverage_adapters(self.root, self.policy).values()))
                job = yaml.load(rendered, Loader=installer.WorkflowLoader)["jobs"]["tests"]
                self.assertTrue(job["uses"].endswith("@" + REF))
                self.assertEqual(job["with"]["coverage-threshold"], 73)
                self.assertEqual(job["with"]["test-args"], "existing")
                self.assertEqual(job["with"]["coverage-artifact-name"], "coverage-python")
                self.assertNotIn("steps", job)

    def test_rejects_unreviewed_or_ambiguous_profiles(self):
        cases = [
            ("name", "../invalid"), ("name", "${{ github.ref }}"), ("workflow", "unknown.yml"),
            ("job", "invalid/job"), ("path", "../coverage.xml"), ("path", "/coverage.xml"),
            ("path", "coverage*.xml"), ("path", "a/./coverage.xml"), ("required", "false"),
            ("format", "unknown"),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                policy = copy.deepcopy(self.policy)
                policy["coverage"]["reports"][0][key] = value
                with self.assertRaises(ValueError):
                    installer.coverage_policy(policy)
        for key, value in [("visibility", "private"), ("ci_execution", "local")]:
            policy = {**self.policy, key: value}
            with self.assertRaises(ValueError):
                installer.coverage_policy(policy)
        self.policy["coverage"]["toolkit_ref"] = "main"
        with self.assertRaises(ValueError):
            installer.coverage_policy(self.policy)

    def test_does_not_silently_replace_mandatory_existing_upload(self):
        self.workflow["jobs"]["tests"]["steps"].append({"uses": "codecov/codecov-action@" + REF})
        self.write_workflow()
        with self.assertRaisesRegex(ValueError, "existing Codecov"):
            installer.coverage_adapters(self.root, self.policy)

    def test_never_overwrites_repository_owned_step_with_colliding_id(self):
        job = self.workflow["jobs"]["tests"]
        job["steps"].append({"id": "export_coverage_python", "run": "pytest --cov-fail-under=99"})
        self.write_workflow()
        path = self.root / ".github/workflows/ci.yml"
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "collides with a repository-owned"):
            installer.coverage_adapters(self.root, self.policy)
        self.assertEqual(path.read_bytes(), original)

    def test_matrix_and_duplicate_reports_fail_before_writing(self):
        self.workflow["jobs"]["tests"]["strategy"] = {"matrix": {"python": ["3.11", "3.12"]}}
        self.write_workflow()
        with self.assertRaisesRegex(ValueError, "non-matrix"):
            installer.coverage_adapters(self.root, self.policy)
        self.policy["coverage"]["reports"].append(copy.deepcopy(self.report))
        with self.assertRaises(ValueError):
            installer.coverage_policy(self.policy)

    def test_duplicate_yaml_keys_are_rejected_without_rewriting(self):
        (self.root / ".github/workflows/ci.yml").write_text("on: {}\non: {}\njobs: {}\n")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            installer.coverage_adapters(self.root, self.policy)

    def test_full_render_is_idempotent_and_vendored_gate_rejects_pin_drift(self):
        (self.root / ".release-policy.json").write_text(json.dumps(self.policy))
        files = installer.render(self.root)
        for name, text in files.items():
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text)
        self.assertEqual(installer.render(self.root), files)
        contracts.validate(self.root)
        gate_path = self.root / ".github/workflows/quality-gate.yml"
        gate_path.write_text(gate_path.read_text().replace(REF, "b" * 40))
        with self.assertRaisesRegex(ValueError, "differs from its policy"):
            contracts.validate(self.root)

    def test_cli_invalid_profile_does_not_partially_write_generated_files(self):
        self.report["path"] = "../coverage.xml"
        (self.root / ".release-policy.json").write_text(json.dumps(self.policy))
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        result = subprocess.run([sys.executable, str(ROOT / "scripts/install_release.py"), str(self.root)], capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        after = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_round_trip_preserves_maintainer_comments_and_third_party_labels(self):
        path = self.root / ".github/workflows/ci.yml"
        source = """# Repository-owned validation: keep this explanation.
name: CI
on:
  workflow_call: {}
permissions:
  contents: read
jobs:
  tests:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      # Keep the tracked version label for Renovate.
      - uses: example/vendor-action@bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb # v12.3.4
      - name: 'Quoted name'
        run: |
          # This is shell code, not a YAML comment.
          cat <<'LITERAL'
          uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a
          LITERAL
          pytest --cov=app --cov-fail-under=73 --cov-report=xml
"""
        path.write_text(source)
        rendered = installer.coverage_adapters(self.root, self.policy)[".github/workflows/ci.yml"]
        for line in source.splitlines():
            self.assertIn(line, rendered)
        original_workflow = yaml.load(source, Loader=installer.WorkflowLoader)
        rendered_workflow = yaml.load(rendered, Loader=installer.WorkflowLoader)
        self.assertEqual(original_workflow["jobs"]["tests"]["steps"], rendered_workflow["jobs"]["tests"]["steps"][:-1])
        path.write_text(rendered)
        self.assertEqual(installer.coverage_adapters(self.root, self.policy)[".github/workflows/ci.yml"], rendered)

    def test_aliased_producer_is_rejected_without_modifying_other_jobs(self):
        path = self.root / ".github/workflows/ci.yml"
        source = """on: {workflow_call: {}}
permissions: {contents: read}
jobs:
  tests: &common
    runs-on: ubuntu-latest
    steps:
      - run: pytest --cov-report=xml
  other: *common
"""
        path.write_text(source)
        with self.assertRaisesRegex(ValueError, "YAML anchors"):
            installer.coverage_adapters(self.root, self.policy)
        self.assertEqual(path.read_text(), source)
