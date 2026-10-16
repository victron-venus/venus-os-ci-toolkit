"""Pinned age installation is confined to the CI-owned temporary directory."""

import hashlib
import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/install-age-for-tests.py'
SPEC = importlib.util.spec_from_file_location('scripts.install_age_for_tests', SCRIPT)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def archive_bytes():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for name, data in [('age/age', b'synthetic age'), ('age/age-keygen', b'synthetic keygen'),
                           ('../outside', b'not installed')]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


class AgeInstallationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {'RUNNER_TEMP': str(self.root)}))
        self.network = self.enterContext(patch('urllib.request.urlopen'))

    def test_only_two_fixed_binaries_are_written_inside_base(self):
        archive = archive_bytes()
        self.network.return_value = io.BytesIO(archive)
        with (patch.object(installer.platform, 'system', return_value='Linux'),
              patch.object(installer.platform, 'machine', return_value='x86_64'),
              patch.dict(installer.DIGESTS, {('Linux', 'x86_64'): hashlib.sha256(archive).hexdigest()}),
              patch('sys.stdout', new=io.StringIO())):
            installer.install()
        child = self.root / 'secret-control-age'
        self.assertEqual({p.name for p in self.root.iterdir()}, {'secret-control-age'})
        self.assertEqual({p.name for p in child.iterdir()}, {'age', 'age-keygen'})
        self.assertEqual((child / 'age').read_bytes(), b'synthetic age')
        self.assertEqual((child / 'age-keygen').read_bytes(), b'synthetic keygen')
        for path in (child, child / 'age', child / 'age-keygen'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.network.assert_called_once_with(
            'https://github.com/FiloSottile/age/releases/download/v1.3.2/age-v1.3.2-linux-amd64.tar.gz',
            timeout=60,
        )

    def test_invalid_base_fails_before_network_or_writes(self):
        for raw in ('', '.', 'relative', '/', str(self.root / '..' / 'escape'), str(self.root / 'missing')):
            with self.subTest(raw=raw), patch.dict(os.environ, {'RUNNER_TEMP': raw}):
                with self.assertRaises((ValueError, OSError)):
                    installer.install()
        self.network.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_base_and_ancestor_symlinks_are_refused(self):
        base = self.root / 'base'
        base.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(base, target_is_directory=True)
        nested = base / 'nested'
        nested.mkdir()
        for raw in (alias, alias / 'nested'):
            with self.subTest(raw=raw), patch.dict(os.environ, {'RUNNER_TEMP': str(raw)}):
                with self.assertRaises(OSError):
                    installer.install()
        self.network.assert_not_called()
        self.assertEqual(list(nested.iterdir()), [])

    def test_writable_or_foreign_base_is_refused(self):
        self.root.chmod(0o777)
        with self.assertRaises(ValueError):
            installer.install()
        self.root.chmod(0o700)
        with patch.object(installer.os, 'geteuid', return_value=os.geteuid() + 1):
            with self.assertRaises(ValueError):
                installer.install()
        self.network.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_child_and_child_symlink_are_never_reused(self):
        child = self.root / 'secret-control-age'
        child.mkdir()
        with self.assertRaises(FileExistsError):
            installer.install()
        child.rmdir()
        child.symlink_to(self.root / 'elsewhere', target_is_directory=True)
        with self.assertRaises(FileExistsError):
            installer.install()
        self.network.assert_not_called()
        self.assertFalse((self.root / 'elsewhere').exists())

    def test_bad_digest_leaves_no_executable(self):
        self.network.return_value = io.BytesIO(b'not the pinned archive')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            installer.install()
        self.assertEqual(list((self.root / 'secret-control-age').iterdir()), [])

    def test_cli_destination_argument_is_rejected_before_installation(self):
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), str(self.root / 'outside')],
                                capture_output=True, text=True, timeout=10, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('usage:', result.stderr)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
