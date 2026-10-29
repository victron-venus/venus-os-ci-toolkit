"""CA-only OCI conversion: tiny real archives, synthetic certificates, no network."""
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
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
        self.local_ca = self.root / 'ca.pem'
        self.local_ca.write_bytes(self.ca_raw)
        self.parent = self.root / 'parent.oci.tar'
        self.parent_sha, self.parent_size = image(self.parent)
        self.child = self.root / 'child.oci.tar'
        self.disk = mock.patch.object(site.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(
            200 * site.GIB, 10 * site.GIB, 190 * site.GIB))
        self.disk.start()
        self.addCleanup(self.disk.stop)

    def derive(self, **changes):
        args = dict(parent_path=self.parent.name, parent_sha=self.parent_sha, parent_size=self.parent_size,
                    source=SOURCE, stage='public', ca_path=self.local_ca.name, ca_sha=self.ca_sha,
                    output=self.child.name, workspace=self.root)
        for key in ('parent_path', 'ca_path', 'output'):
            if key in changes and isinstance(changes[key], Path):
                changes[key] = changes[key].relative_to(self.root)
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
            digest = site.sha(raw)
            with self.assertRaisesRegex(ValueError, 'public PEM'):
                self.derive(ca_path=invalid, ca_sha=digest)
        self.assertFalse(self.child.exists())

    def test_leaf_is_not_accepted_as_ca(self):
        cert, key = self.root / 'leaf.pem', self.root / 'leaf.key'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-subj', '/CN=Synthetic leaf', '-days', '2',
                        '-addext', 'basicConstraints=critical,CA:FALSE',
                        '-keyout', str(key), '-out', str(cert)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        digest = oci.digest_file(cert)
        with self.assertRaisesRegex(ValueError, 'not a CA'):
            self.derive(ca_path=cert, ca_sha=digest)

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
        transport = oci.project_runtime_archive(self.child, result, self.root.resolve() / 'private-runtime.tar')
        self.assertEqual(transport['runtime_manifest'], result['platform']['manifest'])

    def test_output_and_ca_symlinks_are_not_followed(self):
        link = self.root / 'ca-link'
        link.symlink_to(self.ca)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.derive(ca_path=link)
        self.child.symlink_to(self.root / 'missing')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.derive()
        self.assertTrue(self.child.is_symlink())

    def test_existing_output_is_preserved(self):
        self.child.write_bytes(b'preserve')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.derive()
        self.assertEqual(self.child.read_bytes(), b'preserve')

    def test_workspace_rejects_escape_absolute_paths_and_linked_directories(self):
        for path in ('../outside.oci.tar', str(self.root / 'absolute.oci.tar')):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'Workspace-relative'):
                self.derive(output=path)
        linked = self.root / 'linked'
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.derive(output='linked/child.oci.tar')
        self.assertFalse(self.child.exists())

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
                    info = tarfile.TarInfo(name)
                    info.size = len(raw)
                    output.addfile(info, io.BytesIO(raw))
                for desc in runtime['layers']:
                    name = 'blobs/sha256/' + desc['digest'][7:]
                    info = tarfile.TarInfo(name)
                    info.size = desc['size']
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

    def transport(self, **changes):
        args = dict(path=self.child, proof=self.verify(), output=self.root.resolve() / 'runtime.oci.tar')
        args.update(changes)
        return oci.project_runtime_archive(**args)

    def test_transport_preserves_runtime_closure_and_is_deterministic(self):
        proof = self.derive()
        result = self.transport()
        output = self.root / 'runtime.oci.tar'
        second = self.root.resolve() / 'second.oci.tar'
        self.transport(output=second)
        self.assertEqual(output.read_bytes(), second.read_bytes())
        self.assertEqual(oci.digest_file(self.child), proof['archive_sha256'])
        self.assertEqual(result['runtime_manifest'], proof['platform']['manifest'])
        self.assertEqual(result['source_image_index'], proof['image_index'])
        with tarfile.open(output) as projected, tarfile.open(self.child) as source:
            index = json.load(projected.extractfile('index.json'))
            runtime, = index['manifests']
            self.assertEqual(set(runtime), {'mediaType', 'digest', 'size', 'platform'})
            self.assertEqual(runtime['digest'], result['runtime_manifest'])
            manifest = json.load(projected.extractfile('blobs/sha256/' + runtime['digest'][7:]))
            names = {'blobs/sha256/' + d['digest'][7:] for d in
                     [runtime, manifest['config'], *manifest['layers']]}
            self.assertEqual(set(projected.getnames()), names | {'index.json', 'oci-layout'})
            self.assertEqual(len(names), result['blob_count'])
            for member in projected.getmembers():
                self.assertTrue(member.isfile())
                self.assertEqual((member.mode, member.uid, member.gid, member.mtime), (0o644, 0, 0, 0))
                if member.name in names:
                    self.assertEqual(projected.extractfile(member).read(), source.extractfile(member.name).read())
        for field in ('image_imported', 'image_executed', 'deployable', 'runtime_qualified'):
            self.assertFalse(result[field])
        self.assertTrue(result['content_bytes_unchanged'])
        self.assertTrue(result['no_new_build_or_provenance_claim'])

    def test_transport_rejects_source_and_proof_mismatches(self):
        proof = self.derive()
        for field in ('manifest', 'config', 'layers', 'diff_ids'):
            changed = copy.deepcopy(proof)
            if field == 'layers':
                changed['platform'][field][0]['digest'] = 'sha256:' + 'f' * 64
            elif field == 'diff_ids':
                changed['platform'][field][0] = 'sha256:' + 'f' * 64
            else:
                changed['platform'][field] = 'sha256:' + 'f' * 64
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.transport(proof=changed)
        self.child.write_bytes(self.child.read_bytes() + b'tamper')
        with self.assertRaises(ValueError):
            oci.project_runtime_archive(self.child, proof, self.root.resolve() / 'runtime.oci.tar')
        self.assertFalse((self.root / 'runtime.oci.tar').exists())

    def changed_transport_index(self, edit):
        proof = self.verify()
        with tarfile.open(self.child) as source:
            files = {m.name: source.extractfile(m).read() for m in source.getmembers()}
        outer = json.loads(files['index.json'])
        old = 'blobs/sha256/' + outer['manifests'][0]['digest'][7:]
        index = json.loads(files.pop(old))
        edit(index['manifests'][0])
        raw = site.canonical(index)
        digest = hashlib.sha256(raw).hexdigest()
        files['blobs/sha256/' + digest] = raw
        outer['manifests'][0].update(digest='sha256:' + digest, size=len(raw))
        files['index.json'] = site.canonical(outer)
        path = self.root / 'changed-index.oci.tar'
        with tarfile.open(path, 'w') as target:
            for name, raw in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(raw)
                target.addfile(member, io.BytesIO(raw))
        proof.update(archive_sha256=oci.digest_file(path), archive_bytes=path.stat().st_size,
                     image_index='sha256:' + digest)
        return path, proof

    def test_transport_rejects_wrong_platform_and_drops_name_annotations(self):
        self.derive()
        path, proof = self.changed_transport_index(lambda d: d.update(platform={'os': 'linux', 'architecture': 'arm64'}))
        with self.assertRaisesRegex(ValueError, 'not the verified runtime'):
            self.transport(path=path, proof=proof)
        path, proof = self.changed_transport_index(lambda d: d.update(annotations={
            'io.containerd.image.name': 'unexpected.invalid/other:latest'}))
        self.transport(path=path, proof=proof)
        with tarfile.open(self.root / 'runtime.oci.tar') as archive:
            runtime, = json.load(archive.extractfile('index.json'))['manifests']
            self.assertNotIn('annotations', runtime)

    def test_transport_preserves_existing_or_linked_outputs(self):
        self.derive()
        output = self.root.resolve() / 'runtime.oci.tar'
        output.write_bytes(b'preserve')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.transport()
        self.assertEqual(output.read_bytes(), b'preserve')
        output.unlink()
        output.symlink_to(self.root / 'missing')
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            self.transport()
        self.assertTrue(output.is_symlink())
        linked = self.root / 'linked'
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            self.transport(output=linked / 'another.tar')

    def test_transport_bounds_fail_without_leaving_output(self):
        self.derive()
        for edit in ({'reserve_bytes': 1}, {'limit': 512}, {'limit': True}, {'limit': 1 << 40}):
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                self.transport(**edit)
        with mock.patch.object(oci.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(
                20 * site.GIB, 0, 20 * site.GIB)):
            with self.assertRaisesRegex(ValueError, 'disk reserve'):
                self.transport()
        with mock.patch.object(oci.shutil, 'disk_usage', side_effect=[
                shutil._ntuple_diskusage(200 * site.GIB, 0, 200 * site.GIB),
                shutil._ntuple_diskusage(20 * site.GIB, 0, 20 * site.GIB)]):
            with self.assertRaisesRegex(ValueError, 'disk reserve'):
                self.transport()
        self.assertFalse((self.root / 'runtime.oci.tar').exists())
        self.assertFalse(list(self.root.glob('.runner-runtime-*')))

    def test_transport_source_rehash_does_not_allow_unsafe_tar_entries(self):
        proof = self.derive()
        clean = self.child.read_bytes()
        for kind in ('traversal', 'symlink', 'duplicate'):
            self.child.write_bytes(clean)
            with tarfile.open(self.child, 'a') as archive:
                member = tarfile.TarInfo('../escape' if kind == 'traversal' else 'index.json')
                if kind == 'symlink':
                    member.name = 'blobs/sha256/' + 'f' * 64
                    member.type, member.linkname = tarfile.SYMTYPE, '/outside'
                archive.addfile(member, io.BytesIO(b''))
            changed = proof | {'archive_sha256': oci.digest_file(self.child),
                               'archive_bytes': self.child.stat().st_size}
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                oci.project_runtime_archive(self.child, changed, self.root.resolve() / 'runtime.oci.tar')
        self.assertFalse((self.root / 'runtime.oci.tar').exists())

    def test_transport_failed_fsync_and_concurrent_output_preserve_unrelated_files(self):
        proof = self.derive()
        unrelated = self.root / '.runner-runtime-unrelated.partial'
        unrelated.write_bytes(b'keep')
        with mock.patch.object(oci.os, 'fsync', side_effect=OSError('fsync failed')):
            with self.assertRaisesRegex(OSError, 'fsync failed'):
                self.transport()
        self.assertFalse((self.root / 'runtime.oci.tar').exists())
        with mock.patch.object(oci.Archive, 'unchanged', side_effect=ValueError('source changed')):
            with self.assertRaisesRegex(ValueError, 'source changed'):
                oci.project_runtime_archive(self.child, proof, self.root.resolve() / 'runtime.oci.tar')
        self.assertFalse((self.root / 'runtime.oci.tar').exists())
        link = oci.os.link
        def competing_output(source, target):
            Path(target).write_bytes(b'other writer')
            link(source, target)
        with mock.patch.object(oci.os, 'link', side_effect=competing_output):
            with self.assertRaises(FileExistsError):
                self.transport()
        self.assertEqual((self.root / 'runtime.oci.tar').read_bytes(), b'other writer')
        self.assertEqual(list(self.root.glob('.runner-runtime-*')), [unrelated])

    def transport_cli(self, extra=()):
        argv = ['runner_image_site.py', '--workspace', str(self.root), '--parent', self.parent.name,
                '--parent-sha256', self.parent_sha, '--parent-bytes', str(self.parent_size),
                '--source', SOURCE, '--stage', 'public', '--ca', self.local_ca.name,
                '--ca-file-sha256', self.ca_sha, '--output', self.child.name, '--receipt', 'verified.json',
                *extra]
        with mock.patch.object(sys, 'argv', argv), mock.patch('builtins.print'):
            site.main()

    def test_transport_cli_requires_verified_child_and_separate_receipts(self):
        proof = self.derive()
        self.transport_cli(['--verify', '--child-sha256', proof['archive_sha256'],
                            '--child-bytes', str(proof['archive_bytes']), '--runtime-transport', 'runtime.oci.tar',
                            '--transport-receipt', 'transport.json'])
        self.assertEqual(json.loads((self.root / 'verified.json').read_text()), proof)
        result = json.loads((self.root / 'transport.json').read_text())
        self.assertEqual(result['archive_sha256'], oci.digest_file(self.root / 'runtime.oci.tar'))
        self.assertEqual(result['runtime_manifest'], proof['platform']['manifest'])

    def test_transport_cli_rejects_invalid_option_combinations_before_writing(self):
        self.derive()
        for extra in (['--runtime-transport', 'runtime.oci.tar'], ['--transport-receipt', 'transport.json'],
                      ['--runtime-transport', 'runtime.oci.tar', '--transport-receipt', 'transport.json'],
                      ['--verify', '--runtime-transport', 'verified.json', '--transport-receipt', 'transport.json'],
                      ['--verify', '--runtime-transport', '../escape', '--transport-receipt', 'transport.json']):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.transport_cli(extra)
        self.assertFalse((self.root / 'runtime.oci.tar').exists())
        self.assertFalse((self.root / 'verified.json').exists())
        self.assertFalse((self.root / 'transport.json').exists())

    def test_transport_cli_does_not_rewrite_old_converter_identity(self):
        proof = self.derive()
        with mock.patch.object(site, 'converter_identity', return_value={'different': '0' * 64}):
            with self.assertRaisesRegex(ValueError, 'unexpected or missing|differs'):
                self.transport_cli(['--verify', '--child-sha256', proof['archive_sha256'],
                                    '--child-bytes', str(proof['archive_bytes']),
                                    '--runtime-transport', 'runtime.oci.tar', '--transport-receipt', 'transport.json'])
        self.assertFalse((self.root / 'runtime.oci.tar').exists())
        self.assertEqual(oci.digest_file(self.child), proof['archive_sha256'])


if __name__ == '__main__':
    unittest.main()
