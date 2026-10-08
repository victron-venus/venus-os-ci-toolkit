"""Execute the reusable install boundary and propagate real lint failures."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
STEPS = yaml.safe_load((ROOT / '.github/workflows/terraform-ci.yml').read_text())['jobs']['ci']['steps']


def step_script(name):
    return next(step['run'] for step in STEPS if step['name'] == name)


class TFLintContracts(unittest.TestCase):
    def test_terraform_setup_matches_upstream_action_inputs(self):
        # Exact pinned setup-terraform action.yml declares underscored inputs:
        # https://github.com/hashicorp/setup-terraform/blob/dfe3c3f87815947d99a8997f908cb6525fc44e9e/action.yml
        setup = next(step for step in STEPS if step['name'] == 'Set up Terraform')
        self.assertEqual(set(setup['with']), {'terraform_version', 'terraform_wrapper'})
        self.assertFalse(setup['with']['terraform_wrapper'])

    def test_wrong_runtime_version_fails_before_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = root / 'terraform'
            command.write_text(f'#!{sys.executable}\nprint(\'{{"terraform_version": "1.16.5"}}\')\n')
            command.chmod(0o755)
            env = dict(os.environ, PATH=f'{root}{os.pathsep}{os.environ["PATH"]}')
            for requested, succeeds in [('1.16.5', True), ('1.15.7', False), ('<1.17.0', True)]:
                with self.subTest(requested=requested):
                    env['REQUESTED_TERRAFORM_VERSION'] = requested
                    result = subprocess.run(['bash', '-e', '-c', step_script('Verify selected Terraform version')],
                                            env=env, text=True, capture_output=True, timeout=10, check=False)
                    self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                    if not succeeds:
                        self.assertIn('does not match', result.stderr)

    def test_corrupt_download_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / 'bin'
            commands.mkdir()
            (commands / 'curl').write_text(
                f'#!{sys.executable}\nimport pathlib, sys\n'
                'pathlib.Path(sys.argv[sys.argv.index("--output") + 1]).write_bytes(b"corrupt archive")\n'
            )
            (commands / 'unzip').write_text(
                f'#!{sys.executable}\nimport os, pathlib\n'
                'pathlib.Path(os.environ["UNZIP_MARKER"]).write_text("unexpected extraction")\n'
            )
            for command in commands.iterdir():
                command.chmod(0o755)
            marker = root / 'unzip-called'
            path_file = root / 'github-path'
            env = dict(os.environ, PATH=f'{commands}{os.pathsep}{os.environ["PATH"]}',
                       RUNNER_TEMP=temporary, RUNNER_ARCH='X64', GITHUB_PATH=str(path_file), UNZIP_MARKER=str(marker))
            result = subprocess.run(['bash', '-e', '-c', step_script('Install checksum-verified TFLint')],
                                    env=env, text=True, capture_output=True, timeout=10, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('FAILED', result.stdout + result.stderr)
            self.assertFalse(marker.exists())
            self.assertFalse(path_file.exists())
            self.assertEqual(list(root.glob('tflint.*/tflint.zip')), [])

    def test_unsupported_architecture_fails_before_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(['bash', '-e', '-c', step_script('Install checksum-verified TFLint')],
                                    env=dict(os.environ, RUNNER_ARCH='RISCV64', RUNNER_TEMP=temporary),
                                    text=True, capture_output=True, timeout=10, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Unsupported TFLint runner architecture.', result.stderr)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_lint_runs_after_init_and_its_failure_reaches_ci(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / 'calls.jsonl'
            command = root / 'tflint'
            command.write_text(
                f'#!{sys.executable}\nimport json, os, pathlib, sys\n'
                'with pathlib.Path(os.environ["CALL_LOG"]).open("a") as stream:\n'
                '    stream.write(json.dumps(sys.argv[1:]) + "\\n")\n'
                'sys.exit(0 if "--init" in sys.argv else 2)\n'
            )
            command.chmod(0o755)
            result = subprocess.run(['bash', '-e', '-c', step_script('TFLint')],
                                    env=dict(os.environ, PATH=f'{root}{os.pathsep}{os.environ["PATH"]}', CALL_LOG=str(log)),
                                    text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 2)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(calls, [['--init', '--enable-plugin=terraform'], ['--enable-plugin=terraform']])


if __name__ == '__main__':
    unittest.main()
