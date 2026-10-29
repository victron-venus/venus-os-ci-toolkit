"""CA-only OCI conversion: tiny real archives, synthetic certificates, no network."""
import hashlib
import io
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from scripts import runner_image_oci as oci
from scripts import runner_image_site as site
from test_runner_image_artifacts import SOURCE, image


class SiteImages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cert_directory = tempfile.TemporaryDirectory()
        cls.ca = Path(cls.cert_directory.name) / 'synthetic-ca.pem'
        key = Path(cls.cert_directory.name) / 'synthetic-only.key'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-subj', '/CN=Runner conversion synthetic test CA', '-days', '2',
                        '-addext', 'basicConstraints=critical,CA:TRUE',
                        '-keyout', str(key), '-out', str(cls.ca)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        key.unlink()
        cls.ca_raw = cls.ca.read_bytes()
        cls.ca_sha = hashlib.sha256(cls.ca_raw).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.cert_directory.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.parent = self.root / 'parent.oci.tar'
        self.parent_sha, self.parent_size = image(self.parent)
        self.child = self.root / 'child.oci.tar'
        self.disk = mock.patch.object(site.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(
            200 * site.GIB, 10 * site.GIB, 190 * site.GIB))
        self.disk.start()
        self.addCleanup(self.disk.stop)

    def derive(self, **changes):
        args = dict(parent_path=self.parent, parent_sha=self.parent_sha, parent_size=self.parent_size,
                    source=SOURCE, stage='public', ca_path=self.ca, ca_sha=self.ca_sha, output=self.child)
        args.update(changes)
        return site.derive(**args)

    def verify(self, path=None, **changes):
        path = path or self.child
        args = dict(path=path, expected=oci.digest_file(path), size=path.stat().st_size,
                    parent_path=self.parent, parent_sha=self.parent_sha, parent_size=self.parent_size,
                    source=SOURCE, stage='public', ca=self.ca_raw)
        args.update(changes)
        return site.verify_site(**args)

    def test_real_archive_roundtrip_preserves_runtime_and_layers(self):
        before = oci.verify(self.parent, self.parent_sha, self.parent_size, SOURCE, 'public', site.SENTINEL)
        proof = self.derive()
        self.assertEqual(self.verify(), proof)
        self.assertEqual(proof['platform']['runtime_config'], before['platform']['runtime_config'])
        self.assertEqual(proof['platform']['layers'][:-1], before['platform']['layers'])
        self.assertEqual(proof['platform']['diff_ids'][:-1], before['platform']['diff_ids'])
        self.assertEqual(proof['parent']['runtime_manifest'], before['platform']['manifest'])
        self.assertEqual(proof['ca_file_sha256'], self.ca_sha)
        self.assertEqual(proof['changed_paths'], [site.TARGET])
        self.assertTrue(proof['site_configured'])
        for field in ('signed', 'deployable', 'scan_accepted', 'runtime_qualified'):
            self.assertFalse(proof[field])

    def test_deterministic_bytes_and_new_conversion_subject(self):
        first = self.derive()
        second = self.root / 'second.oci.tar'
        self.derive(output=second)
        self.assertEqual(self.child.read_bytes(), second.read_bytes())
        with tarfile.open(self.child) as archive:
            statements = [archive.extractfile(m).read() for m in archive.getmembers()
                          if m.isfile() and m.size < 65536]
        conversion, = [raw for raw in statements if site.PREDICATE.encode() in raw]
        self.assertIn(first['platform']['manifest'][7:].encode(), conversion)
        self.assertNotIn(b'https://slsa.dev/provenance/', conversion)
        self.assertFalse(any(b'https://slsa.dev/provenance/' in raw for raw in statements))

    def test_only_pinned_public_ca_is_accepted(self):
        with self.assertRaisesRegex(ValueError, 'SHA256 differs'):
            self.derive(ca_sha='0' * 64)
        invalid = self.root / 'not-ca.pem'
        for raw in (site.SENTINEL, self.ca_raw * 2, b'-----BEGIN PRIVATE KEY-----\nX\n-----END PRIVATE KEY-----\n'):
            invalid.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, 'public PEM'):
                self.derive(ca_path=invalid, ca_sha=site.sha(raw))
        self.assertFalse(self.child.exists())

    def test_leaf_is_not_accepted_as_ca(self):
        cert, key = self.root / 'leaf.pem', self.root / 'leaf.key'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-subj', '/CN=Synthetic leaf', '-days', '2',
                        '-addext', 'basicConstraints=critical,CA:FALSE',
                        '-keyout', str(key), '-out', str(cert)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        with self.assertRaisesRegex(ValueError, 'not a CA'):
            self.derive(ca_path=cert, ca_sha=oci.digest_file(cert))

    def test_parent_source_stage_hash_and_size_are_mandatory(self):
        for edit in ({'parent_sha': '0' * 64}, {'parent_size': self.parent_size - 1},
                     {'source': 'b' * 40}, {'stage': 'private'}):
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                self.derive(**edit)
        self.assertFalse(self.child.exists())

    def test_private_parent_is_distinct_and_retained(self):
        self.parent_sha, self.parent_size = image(self.parent, stage='private')
        result = self.derive(stage='private')
        self.assertEqual(result['stage'], 'private')
        self.assertEqual(result['parent']['archive_sha256'], self.parent_sha)
        self.assertEqual(oci.digest_file(self.parent), self.parent_sha)
        self.assertEqual(self.verify(stage='private'), result)

    def test_output_and_ca_symlinks_are_not_followed(self):
        link = self.root / 'ca-link'
        link.symlink_to(self.ca)
        with self.assertRaises(OSError):
            self.derive(ca_path=link)
        self.child.symlink_to(self.root / 'missing')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.derive()
        self.assertTrue(self.child.is_symlink())

    def test_existing_output_is_preserved(self):
        self.child.write_bytes(b'preserve')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.derive()
        self.assertEqual(self.child.read_bytes(), b'preserve')

    def test_space_budget_and_minimum_reserve(self):
        with self.assertRaisesRegex(ValueError, '20 GiB'):
            self.derive(reserve_bytes=2 * site.GIB)
        with mock.patch.object(site.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(
                20 * site.GIB, 0, 20 * site.GIB)):
            with self.assertRaisesRegex(ValueError, 'Insufficient space'):
                self.derive()
        self.assertFalse(self.child.exists())
        self.assertFalse(list(self.root.glob('.runner-site-*')))

    def test_child_wrong_ca_and_parent_tamper_fail(self):
        self.derive()
        with self.assertRaisesRegex(ValueError, 'unexpected or missing|differs'):
            self.verify(ca=self.ca_raw + b'\n')
        with self.parent.open('ab') as stream:
            stream.write(b'altered')
        with self.assertRaises(ValueError):
            self.verify()

    def test_wrong_ca_destination_is_rejected_even_with_rehashed_graph(self):
        with mock.patch.object(site, 'TARGET', 'opt/victron-ci-reserve/wrong-ca.crt'):
            self.derive()
        with self.assertRaisesRegex(ValueError, 'unexpected or missing|differs'):
            self.verify()

    def test_changed_converter_identity_is_rejected(self):
        with mock.patch.object(site, 'converter_identity', return_value={
                'runner_image_site.py': '0' * 64, 'runner_image_oci.py': '1' * 64}):
            self.derive()
        with self.assertRaisesRegex(ValueError, 'unexpected or missing|differs'):
            self.verify()

    def test_failed_verification_removes_only_its_owned_partial(self):
        unrelated = self.root / '.runner-site-unrelated.partial'
        unrelated.write_bytes(b'preserve')
        with mock.patch.object(site, 'verify_site', side_effect=ValueError('verification failed')):
            with self.assertRaisesRegex(ValueError, 'verification failed'):
                self.derive()
        self.assertEqual(list(self.root.glob('.runner-site-*')), [unrelated])
        self.assertFalse(self.child.exists())

    def test_reader_detects_concurrent_disk_pressure(self):
        reader = site.ReserveReader(io.BytesIO(b'x' * (site.MIB + 1)), self.root, 20 * site.GIB)
        self.assertEqual(len(reader.read(site.MIB)), site.MIB)
        with mock.patch.object(site.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(
                20 * site.GIB, 0, 20 * site.GIB)):
            with self.assertRaisesRegex(ValueError, 'Disk reserve crossed'):
                reader.read(1)

    def changed_graph(self, edit):
        base, proof, runtime, config = site.parent(self.parent, self.parent_sha, self.parent_size, SOURCE, 'public')
        try:
            edit(runtime, config)
            files, *_ = site.graph(proof, runtime, config, self.ca_raw)
            with tarfile.open(self.child, 'w') as output:
                for name, raw in files.items():
                    info = tarfile.TarInfo(name); info.size = len(raw)
                    output.addfile(info, io.BytesIO(raw))
                for desc in runtime['layers']:
                    name = 'blobs/sha256/' + desc['digest'][7:]
                    info = tarfile.TarInfo(name); info.size = desc['size']
                    output.addfile(info, base.tar.extractfile(base.files[name]))
        finally:
            base.close()

    def test_rehashed_runtime_config_change_is_rejected(self):
        self.changed_graph(lambda runtime, config: config['config'].update(Entrypoint=['/bin/sh']))
        with self.assertRaisesRegex(ValueError, 'unexpected or missing|differs'):
            self.verify()

    def test_rehashed_parent_prefix_change_is_rejected(self):
        def edit(runtime, config):
            runtime['layers'] = []
            config['rootfs']['diff_ids'] = []
        self.changed_graph(edit)
        with self.assertRaisesRegex(ValueError, 'unexpected or missing|differs'):
            self.verify()

    def test_generated_ca_layer_has_single_path_and_deterministic_metadata(self):
        payload, diff_id = site.ca_layer(self.ca_raw)
        with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
            member, = archive.getmembers()
            self.assertEqual(member.name, site.TARGET)
            self.assertTrue(member.isfile())
            self.assertEqual((member.mode, member.uid, member.gid, member.mtime), (0o644, 0, 0, 0))
            self.assertEqual(archive.extractfile(member).read(), self.ca_raw)
        self.assertTrue(diff_id.startswith('sha256:'))

    def test_outer_traversal_links_duplicates_and_blob_tamper_fail(self):
        self.derive()
        clean = self.child.read_bytes()
        for kind in ('traversal', 'symlink', 'hardlink', 'duplicate'):
            self.child.write_bytes(clean)
            with tarfile.open(self.child, 'a') as archive:
                member = tarfile.TarInfo('../outside' if kind == 'traversal' else 'index.json')
                if kind in ('symlink', 'hardlink'):
                    member.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
                    member.name = 'blobs/sha256/' + 'f' * 64
                    member.linkname = '/outside'
                archive.addfile(member, io.BytesIO(b''))
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.verify()

    def test_client_explicitly_uses_the_changed_ca_without_system_store(self):
        bootstrap = (Path(__file__).resolve().parents[1] / 'deploy/runner-reserve/start-runner.sh').read_text()
        self.assertIn('--cacert /' + site.TARGET, bootstrap)
        self.assertIn('https://${RUNNER_RESERVE_NODE_IP}:19999/ready/${RUNNER_RESERVE_POD_UID}', bootstrap)
        self.assertNotIn('update-ca-certificates', bootstrap)


if __name__ == '__main__':
    unittest.main()
