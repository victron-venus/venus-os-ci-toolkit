"""Exercise the real uv requirements fallback with a local, fully offline package index."""

import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import venv
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/python-ci.yml').read_text())
INSTALL = next(step['run'] for step in WORKFLOW['jobs']['ci']['steps'] if step.get('name') == 'Install dependencies')


class HashedRequirementsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(shutil.which("uv"), "Install the pinned uv test runtime")
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='hashed requirements ')))
        self.consumer = self.root / 'consumer'
        self.consumer.mkdir()
        self.packages = self.root / 'packages'
        self.packages.mkdir()
        environment = self.root / 'environment'
        venv.EnvBuilder(with_pip=False).create(environment)
        binaries = environment / ('Scripts' if os.name == 'nt' else 'bin')
        self.python = binaries / ('python.exe' if os.name == 'nt' else 'python')
        self.env = dict(os.environ, PATH=f'{binaries}{os.pathsep}{os.environ["PATH"]}',
                        UV_NO_INDEX='true', UV_OFFLINE='true', UV_FIND_LINKS=self.packages.as_uri(),
                        UV_PYTHON_DOWNLOADS='never', UV_CACHE_DIR=str(self.root / 'cache'))
        self.wheel = self.packages / 'pip_fixture-1.0.0-py3-none-any.whl'
        with zipfile.ZipFile(self.wheel, 'w') as wheel:
            wheel.writestr('pip_fixture.py', 'VALUE = 42\n')
            metadata = 'pip_fixture-1.0.0.dist-info'
            wheel.writestr(metadata + '/METADATA', 'Metadata-Version: 2.1\nName: pip-fixture\nVersion: 1.0.0\n')
            wheel.writestr(metadata + '/WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
            wheel.writestr(metadata + '/RECORD', '')

    def install(self, requirement):
        (self.consumer / 'requirements.txt').write_text(requirement + '\n')
        return subprocess.run(['bash', '-e', '-c', INSTALL], cwd=self.consumer, env=self.env,
                              capture_output=True, text=True, timeout=30, check=False)

    def test_hash_locked_wheel_is_installed(self):
        digest = hashlib.sha256(self.wheel.read_bytes()).hexdigest()
        result = self.install(f'pip-fixture==1.0.0 --hash=sha256:{digest}')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([str(self.python), '-c', 'import pip_fixture; assert pip_fixture.VALUE == 42'],
                                cwd=self.consumer, env=self.env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_hash_is_rejected(self):
        result = self.install('pip-fixture==1.0.0')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('hash', result.stderr.lower())

    def test_source_requirements_cannot_execute_build_backend(self):
        self.wheel.unlink()
        archive = self.packages / 'pip_fixture-1.0.0.tar.gz'
        marker = self.root / 'executed'
        files = {
            'pyproject.toml': '[build-system]\nrequires=[]\nbuild-backend="backend"\nbackend-path=["."]\n',
            'backend.py': (f'from pathlib import Path\nPath({str(marker)!r}).touch()\n'
                           'def get_requires_for_build_wheel(config_settings=None): return []\n'
                           'def build_wheel(*args, **kwargs): raise RuntimeError("backend executed")\n'),
            'PKG-INFO': 'Metadata-Version: 2.1\nName: pip-fixture\nVersion: 1.0.0\n',
        }
        with tarfile.open(archive, 'w:gz') as package:
            for name, content in files.items():
                payload = content.encode()
                entry = tarfile.TarInfo('pip_fixture-1.0.0/' + name)
                entry.size = len(payload)
                package.addfile(entry, io.BytesIO(payload))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        editable = self.root / 'editable'
        editable.mkdir()
        for name, content in files.items():
            (editable / name).write_text(content)
        requirements = {
            'index source': f'pip-fixture==1.0.0 --hash=sha256:{digest}',
            'direct source': f'pip-fixture @ {archive.as_uri()} --hash=sha256:{digest}',
            'requirements option': f'--no-binary=:all:\npip-fixture==1.0.0 --hash=sha256:{digest}',
            'editable source': f'-e {editable.as_uri()}',
        }
        for case, requirement in requirements.items():
            with self.subTest(case=case):
                result = self.install(requirement)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertFalse(marker.exists(), result.stderr)
                self.assertRegex(result.stderr.lower(), r'disabled|hash')

    def test_packaged_project_requires_locked_project_mode(self):
        (self.consumer / 'pyproject.toml').write_text('[project]\nname="example"\nversion="1.0.0"\n')
        result = self.install('pip-fixture==1.0.0')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('must set use-uv-lock: true', result.stderr)
        self.assertNotIn('require-hashes', result.stderr)


if __name__ == '__main__':
    unittest.main()
