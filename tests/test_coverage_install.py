"""Adopt coverage without migrating an older consumer's unrelated release engine."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("coverage_installer", ROOT / "scripts/install_release.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
REF = "a" * 40


class ScopedCoverageInstallTests(unittest.TestCase):
    """Exercise the CLI against old release files, not just its selected file list."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.policy = {
            "repository": "owner/repo", "mode": "validation-only", "visibility": "public",
            "single_entry_ci": True, "validation_workflows": ["ci.yml"],
            "coverage": {
                "toolkit_ref": REF,
                "reports": [{"name": "python", "workflow": "ci.yml", "job": "tests",
                             "path": "coverage.xml", "format": "cobertura", "required": False}],
            },
        }
        self.write(".github/workflows/ci.yml", """name: CI
on: {workflow_call: {}}
permissions: {contents: read}
jobs:
  tests:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      # Retain the repository's own command and threshold.
      - run: pytest --cov=app --cov-report=xml --cov-fail-under=73
""")
        coverage = self.policy.pop("coverage")
        self.write_policy()
        for name, content in installer.render(self.root).items():
            self.write(name, content)
        self.policy["coverage"] = coverage
        self.write_policy()
        self.write("scripts/release.py", "# Older, independently reviewed release client.\n")
        self.write("docs/release-workflow.md", "Repository-owned release instructions.\n")

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def write_policy(self):
        self.write(".release-policy.json", json.dumps(self.policy))

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mode)
            for path in self.root.rglob("*") if path.is_file()
        }

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/install_release.py"), str(self.root), *arguments],
            capture_output=True, text=True, check=False,
        )

    def enable_release(self):
        self.policy["mode"] = "release"
        self.write_policy()
        self.write(".github/workflows/release-build.yml", """on: {workflow_call: {}}
permissions: {contents: read}
jobs:
  build:
    runs-on: ubuntu-latest
    steps: [{run: 'echo existing build'}]
""")
        self.write(".github/workflows/release-pipeline.yml", """# Retain this older release's reviewed behavior.
name: Release pipeline
on: {workflow_dispatch: {}}
concurrency: 'old-release-${{ github.ref }}'
permissions: {contents: read}
jobs:
  prepare:
    runs-on: ubuntu-latest
    steps: [{run: 'echo existing preparation'}]
  checks:
    needs: prepare
    if: ${{ needs.prepare.result == 'success' }}
    uses: ./.github/workflows/quality-gate.yml
    permissions:
      contents: read # Keep this comment.
      actions: read
      security-events: write
  publish:
    needs: checks
    runs-on: ubuntu-latest
    steps:
      - run: |
          echo 'uses: do-not-rewrite-shell-literals'
          echo 'existing release recipe'
""")
        self.write(".github/release-tests/test_old_contract.py", "# Keep the old release contract.\n")
        self.write("scripts/native_oci_build.py", "# Keep the old native helper.\n")
        self.write("VERSION", "1.2.3\n")

    def test_validation_only_is_bounded_idempotent_and_check_is_read_only(self):
        before = self.snapshot()
        outputs = installer.render_coverage(self.root)
        self.assertEqual(set(outputs), {
            ".github/workflows/quality-gate.yml", ".github/workflows/ci.yml",
            "scripts/change_scope.py", "scripts/workflow_contracts.py",
            ".github/requirements-workflow-contracts.txt",
            ".github/workflow-tests/test_workflow_yaml_contracts.py",
        })
        result = self.cli("--coverage-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.snapshot()
        for name in set(before) - set(outputs):
            self.assertEqual(after[name], before[name], name)
        result = self.cli("--coverage-only", "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(), after)
        self.assertNotEqual(self.cli("--check").returncode, 0)
        self.assertEqual(self.snapshot(), after)

    def test_existing_release_changes_only_oidc_and_preserves_unrelated_files(self):
        self.enable_release()
        before = self.snapshot()
        name = ".github/workflows/release-pipeline.yml"
        original = yaml.load(before[name][0], Loader=yaml.BaseLoader)
        outputs = installer.render_coverage(self.root)
        rewritten = yaml.load(outputs[name], Loader=yaml.BaseLoader)
        self.assertEqual(rewritten["jobs"]["checks"]["permissions"].pop("id-token"), "write")
        self.assertEqual(rewritten, original)
        for line in before[name][0].decode().splitlines():
            if "#" in line or line.lstrip().startswith("echo "):
                self.assertIn(line, outputs[name])
        result = self.cli("--coverage-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.snapshot()
        for path in set(before) - set(outputs):
            self.assertEqual(after[path], before[path], path)
        result = self.cli("--coverage-only", "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(), after)
        self.assertEqual(installer.render_coverage(self.root), outputs)

    def test_rejects_unsafe_release_callers_before_any_write(self):
        self.enable_release()
        path = self.root / ".github/workflows/release-pipeline.yml"
        original = path.read_text()
        invalid_sources = [
            original.replace("uses: ./.github/workflows/quality-gate.yml", "uses: example/repo/.github/workflows/ci.yml@" + REF),
            original.replace("    permissions:\n", "    permissions: &shared\n"),
            original.replace("  checks:\n", "  checks: &shared\n"),
            original.replace("jobs:\n", "jobs: &shared\n"),
            original.replace("      contents: read # Keep this comment.", "      contents: read\n      contents: write"),
            original.replace("    permissions:\n      contents: read # Keep this comment.\n      actions: read\n      security-events: write", "    permissions: read-all"),
            original.replace("      actions: read\n", ""),
            original.replace("      actions: read\n", "      id-token: write\n"),
            "jobs: {checks: null}\n", "jobs: []\n", "[]\n",
        ]
        for source in invalid_sources:
            with self.subTest(source=source):
                path.write_text(source)
                before = self.snapshot()
                result = self.cli("--coverage-only")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.snapshot(), before)
        path.unlink()
        before = self.snapshot()
        self.assertNotEqual(self.cli("--coverage-only").returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_missing_profile_or_existing_quality_gate_fails_before_writes(self):
        coverage = self.policy.pop("coverage")
        self.write_policy()
        before = self.snapshot()
        self.assertNotEqual(self.cli("--coverage-only").returncode, 0)
        self.assertEqual(self.snapshot(), before)
        self.policy["coverage"] = coverage
        self.write_policy()
        (self.root / ".github/workflows/quality-gate.yml").unlink()
        before = self.snapshot()
        self.assertNotEqual(self.cli("--coverage-only").returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_check_detects_producer_drift_without_writing(self):
        result = self.cli("--coverage-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.root / ".github/workflows/ci.yml"
        path.write_text(path.read_text().replace("retention-days: 7", "retention-days: 5"))
        before = self.snapshot()
        self.assertNotEqual(self.cli("--coverage-only", "--check").returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_without_single_entry_contracts_rejects_before_writes(self):
        self.policy["single_entry_ci"] = False
        self.write_policy()
        before = self.snapshot()
        result = self.cli("--coverage-only")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("single_entry_ci", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_does_not_overwrite_handwritten_or_ambiguous_quality_gate(self):
        path = self.root / ".github/workflows/quality-gate.yml"
        header = path.read_text().splitlines()[0] + "\n"
        for source in ("jobs: {}\n", header + "jobs: {}\njobs: {}\n"):
            with self.subTest(source=source):
                path.write_text(source)
                before = self.snapshot()
                result = self.cli("--coverage-only")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
