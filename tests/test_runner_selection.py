"""Keep runner adoption bounded and the reusable call graph free of hosted bootstrap jobs."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HARDEN_RUNNER = "step-security/harden-runner@" + next(
    pin["digest"] for pin in json.loads((ROOT / ".github/action-pins.json").read_text())
    if pin["packageName"] == "step-security/harden-runner"
)
sys.path.insert(0, str(ROOT / "scripts"))
from runner_selection import ACTIVE_WORKFLOWS, render_consumer, render_workflows, runner_labels  # noqa: E402 - Import the source scripts after adding their directory.


class RunnerSelectionTests(unittest.TestCase):
    def test_shared_workflows_and_nested_classifier_use_canonical_routing(self):
        for path, expected in render_workflows().items():
            self.assertEqual(path.read_text(), expected, path.name)
            workflow = yaml.load(expected, Loader=yaml.BaseLoader)
            for job in workflow["jobs"].values():
                if "runs-on" in job:
                    self.assertIn("vars.CI_RUNNER_MODE", job["runs-on"])
                elif job.get("uses") == "./.github/workflows/change-scope.yml":
                    self.assertNotIn("runner", job.get("with", {}))
        classifier = yaml.load((ROOT / ".github/workflows/change-scope.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(classifier["jobs"]["scope"]["runs-on"], runner_labels(explicit_input="runner", scalar=True))
        self.assertNotIn("deploy", ACTIVE_WORKFLOWS)

    def test_no_checkout_in_metadata_jobs(self):
        for name in ("auto-merge.yml", "auto-approve-reusable.yml", "coderabbit-review-reusable.yml", "coderabbit-autofix-reusable.yml"):
            workflow = yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
            for job in workflow["jobs"].values():
                for step in job["steps"]:
                    if "uses" in step:
                        self.assertEqual(step["uses"], HARDEN_RUNNER)
                    self.assertNotIn("checkout", step.get("run", ""))


class ConsumerRunnerAdoptionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workflows = self.root / ".github/workflows"
        self.workflows.mkdir(parents=True)
        self.policy = {"repository": "owner/project", "mode": "validation-only", "single_entry_ci": True,
                       "validation_workflows": ["ci.yml"], "visibility": "public"}
        (self.root / ".release-policy.json").write_text(json.dumps(self.policy))
        self.source = """# Keep repository explanation and commands.
on: {workflow_call: {}}
permissions: {contents: read}
jobs:
  tests:
    runs-on: ubuntu-latest # Runner purpose.
    timeout-minutes: 10
    steps:
      - run: |
          echo 'runs-on: ubuntu-latest is a literal'
          pytest --cov-fail-under=83
  native:
    strategy: {matrix: {os: [ubuntu-24.04-arm, windows-latest]}}
    runs-on: ${{ matrix.os }}
    steps: [{run: 'echo keep platform'}]
"""
        (self.workflows / "ci.yml").write_text(self.source)
        (self.workflows / "quality-gate.yml").write_text("""on: {workflow_call: {}}
permissions: {contents: read}
jobs:
  check:
    uses: ./.github/workflows/ci.yml
  gate:
    runs-on: ubuntu-latest
    steps: [{run: 'echo existing gate'}]
""")
        (self.root / "VERSION").write_text("1.2.3\n")
        (self.root / "release-client.py").write_text("# Old reviewed client.\n")

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/install_release.py"), str(self.root), *args], capture_output=True, text=True, check=False)

    def test_cli_changes_only_runner_nodes_and_check_is_read_only(self):
        before = self.snapshot()
        result = self.cli("--runners-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.snapshot()
        self.assertEqual(set(after), set(before))
        for name in before:
            if not name.endswith('.yml'):
                self.assertEqual(after[name], before[name])
        original = yaml.load(self.source, Loader=yaml.BaseLoader)
        updated = yaml.load((self.workflows / 'ci.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(updated['jobs']['tests']['runs-on'], runner_labels())
        updated['jobs']['tests']['runs-on'] = 'ubuntu-latest'
        self.assertEqual(updated, original)
        for comment in ('# Keep repository explanation and commands.', '# Runner purpose.'):
            self.assertIn(comment, (self.workflows / 'ci.yml').read_text())
        result = self.cli('--runners-only', '--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(), after)

    def test_recursive_calls_and_aliases_fail_before_any_write(self):
        path = self.workflows / 'ci.yml'
        for source in (self.source.replace('  tests:\n', '  tests: &shared\n'),
                       self.source + '  cycle:\n    uses: ./.github/workflows/quality-gate.yml\n'):
            path.write_text(source)
            before = self.snapshot()
            result = self.cli('--runners-only')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.snapshot(), before)

    def test_release_adapter_selects_separate_pool_and_retains_commands(self):
        self.policy['mode'] = 'release'
        (self.workflows / 'release-pipeline.yml').write_text(self.source)
        (self.workflows / 'release-build.yml').write_text(self.source)
        files = render_consumer(self.root, self.policy)
        for name in ('release-build.yml', 'release-pipeline.yml'):
            after = yaml.load(files['.github/workflows/' + name], Loader=yaml.BaseLoader)
            self.assertEqual(after['jobs']['tests']['runs-on'], runner_labels('release'))
            self.assertIn('pytest --cov-fail-under=83', after['jobs']['tests']['steps'][0]['run'])

    def test_declared_release_and_ci_shared_adapter_conflict_is_rejected(self):
        self.policy['mode'] = 'release'
        (self.workflows / 'release-pipeline.yml').write_text(self.source)
        (self.workflows / 'release-build.yml').write_text('jobs:\n  common:\n    uses: ./.github/workflows/common.yml\n')
        (self.workflows / 'ci.yml').write_text('jobs:\n  common:\n    uses: ./.github/workflows/common.yml\n')
        (self.workflows / 'common.yml').write_text(self.source)
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            render_consumer(self.root, self.policy)

    def test_workflow_symlink_cannot_write_outside_selected_repository(self):
        path = self.workflows / 'ci.yml'
        path.unlink()
        target = self.root / 'preserved.yml'
        target.write_text(self.source)
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            render_consumer(self.root, self.policy)
        self.assertEqual(target.read_text(), self.source)


if __name__ == '__main__':
    unittest.main()
