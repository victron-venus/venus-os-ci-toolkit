"""Reject untrusted tool-generation values before running the Go toolchain."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = yaml.safe_load((ROOT / 'actions/setup-go/action.yml').read_text())
INSTALL = next(step['run'] for step in ACTION['runs']['steps'] if step['name'] == 'Install common tools')


class GoToolInstallationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('go'), 'Go is required for the checksum boundary test')
    def test_tampered_module_is_rejected_before_tool_installation(self):
        with tempfile.TemporaryDirectory(prefix='go action ') as temporary:
            root = Path(temporary)
            module = 'golang.org/x/vuln'
            version = 'v1.8.0'
            proxy = root / 'proxy'
            versions = proxy / module / '@v'
            versions.mkdir(parents=True)
            upstream_mod = f'module {module}\n\ngo 1.20\n'
            (versions / f'{version}.mod').write_text(upstream_mod)
            (versions / f'{version}.info').write_text('{"Version":"v1.8.0","Time":"2026-01-01T00:00:00Z"}')
            with zipfile.ZipFile(versions / f'{version}.zip', 'w') as archive:
                archive.writestr(f'{module}@{version}/go.mod', upstream_mod)
                archive.writestr(f'{module}@{version}/cmd/govulncheck/main.go', 'package main\nfunc main() {}\n')
            tools = root / 'action' / 'tools' / 'govulncheck'
            tools.mkdir(parents=True)
            (tools / 'go.mod').write_text(f'module example.invalid/fixture\n\ngo 1.20\n\nrequire {module} {version}\n')
            (tools / 'go.sum').write_text(f'{module} {version} h1:{"A" * 43}=\n')
            binaries = root / 'installed'
            env = dict(os.environ, GITHUB_ACTION_PATH=str(root / 'action'), GOLANGCI_LINT_MAJOR='1',
                       GOTOOLCHAIN='local', GOWORK='off', GOPROXY=proxy.as_uri(), GOSUMDB='off',
                       GOPRIVATE='', GONOSUMDB='', GONOPROXY='', GOFLAGS='',
                       GOMODCACHE=str(root / 'module-cache'), GOBIN=str(binaries))
            result = subprocess.run(['bash', '-e', '-c', INSTALL], env=env, cwd=root,
                                    text=True, capture_output=True, timeout=30, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('checksum mismatch', result.stderr)
            self.assertEqual(list(binaries.glob('*')), [])

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
