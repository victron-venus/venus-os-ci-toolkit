"""Reject untrusted tool-generation values before running the Go toolchain."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = yaml.safe_load((ROOT / 'actions/setup-go/action.yml').read_text())
INSTALL = next(step['run'] for step in ACTION['runs']['steps'] if step['name'] == 'Install common tools')


class GoToolInstallationTests(unittest.TestCase):
    def test_unknown_generation_never_invokes_go_or_shell_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            go = root / 'go'
            marker = root / 'executed'
            go.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n')
            go.chmod(0o755)
            for value in ('', 'latest', '3', '1; touch executed', '$(touch executed)', '2\n1'):
                with self.subTest(value=value):
                    result = subprocess.run(
                        ['bash', '-e', '-c', INSTALL], cwd=root,
                        env=dict(os.environ, PATH=f'{root}{os.pathsep}{os.environ["PATH"]}',
                                 GOLANGCI_LINT_MAJOR=value),
                        text=True, capture_output=True, timeout=5, check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('golangci-lint-major must be 1 or 2.', result.stderr)
                    self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
