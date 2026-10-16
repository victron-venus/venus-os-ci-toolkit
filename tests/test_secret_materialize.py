import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.secret_control.materialize import MaterializationError, materialize_bytes


class MaterializeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.dest = self.root / 'bundle-v1'
        self.payload = b'synthetic\x00binary\xffpayload'
        self.digest = hashlib.sha256(self.payload).hexdigest()

    def put(self, **kw):
        return materialize_bytes(kw.get('payload', self.payload), kw.get('destination', self.dest),
                                 kw.get('expected_sha256', self.digest))

    def test_binary_success_and_identical_retry(self):
        self.assertEqual(self.put()['status'], 'created')
        self.assertEqual(self.dest.read_bytes(), self.payload)
        self.assertEqual(self.dest.stat().st_mode & 0o777, 0o600)
        before = self.dest.stat().st_ino
        self.assertEqual(self.put()['status'], 'unchanged')
        self.assertEqual(self.dest.stat().st_ino, before)

    def test_digest_mismatch_never_creates_file(self):
        with self.assertRaises(MaterializationError):
            self.put(expected_sha256='0' * 64)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_conflicting_output_is_preserved(self):
        self.dest.write_bytes(b'older value')
        self.dest.chmod(0o600)
        with self.assertRaises(MaterializationError):
            self.put()
        self.assertEqual(self.dest.read_bytes(), b'older value')

    def test_symlink_output_is_not_followed(self):
        outside = self.root / 'outside'
        outside.write_bytes(b'unchanged')
        self.dest.symlink_to(outside)
        with self.assertRaises(MaterializationError):
            self.put()
        self.assertEqual(outside.read_bytes(), b'unchanged')

    def test_symlink_parent_is_not_followed(self):
        actual = self.root / 'actual'
        actual.mkdir(mode=0o700)
        alias = self.root / 'alias'
        alias.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(MaterializationError):
            self.put(destination=alias / 'bundle')
        self.assertEqual(list(actual.iterdir()), [])

    def test_insecure_parent_is_rejected(self):
        self.root.chmod(0o755)
        with self.assertRaises(MaterializationError):
            self.put()
        self.assertFalse(self.dest.exists())

    def test_insecure_existing_output_is_rejected(self):
        self.dest.write_bytes(self.payload)
        self.dest.chmod(0o644)
        with self.assertRaises(MaterializationError):
            self.put()

    def test_existing_hard_link_is_rejected(self):
        self.dest.write_bytes(self.payload)
        self.dest.chmod(0o600)
        os.link(self.dest, self.root / 'alias')
        with self.assertRaises(MaterializationError):
            self.put()

    def test_creation_race_cannot_overwrite_other_writer(self):
        original_link = os.link

        def competing_writer(src, dst, **kwargs):
            self.dest.write_bytes(b'competing value')
            return original_link(src, dst, **kwargs)

        with patch('tools.secret_control.materialize.os.link', side_effect=competing_writer):
            with self.assertRaises(MaterializationError):
                self.put()
        self.assertEqual(self.dest.read_bytes(), b'competing value')
        self.assertEqual([p.name for p in self.root.iterdir()], ['bundle-v1'])

    def test_parent_traversal_is_refused(self):
        with self.assertRaises(MaterializationError):
            self.put(destination=self.root / '..' / 'unexpected')


if __name__ == '__main__':
    unittest.main()
