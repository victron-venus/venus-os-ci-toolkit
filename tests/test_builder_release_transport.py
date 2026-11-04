"""Pinned retained assets work without calling expiring Actions endpoints."""
import copy
from dataclasses import FrozenInstanceError
import io
import json
import zipfile
import unittest
from unittest.mock import patch

from scripts import prepare_accepted_builder as accepted
from tests import test_accepted_builder as fixtures


class RetainedBuilderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AcceptedBuilderTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.lock = self.fixture.lock
        self.responses, self.calls = {}, []
        original = self.fixture.fixture('amd64') | self.fixture.fixture('arm64')
        self.config = {'schema': 1, 'kind': 'release', 'repository': accepted.REPOSITORY,
                       'release_id': 77, 'tag': 'v0.1.12', 'source': 'b' * 40, 'architectures': {}}
        provenance = {'repository': accepted.REPOSITORY, 'lock_sha256': accepted.oci.digest_file(accepted.LOCK),
                      'qualification': {'source': self.lock['source'], 'run': self.lock['qualification_run'],
                                        'attempt': 1, 'workflow': self.lock['workflow']},
                      'release': {'source': self.config['source']}, 'architectures': {}}
        self.assets = []
        for arch in ('amd64', 'arm64'):
            spec = self.lock['architectures'][arch]
            self.config['architectures'][arch] = {}
            proof = provenance['architectures'][arch] = {'files': {}}
            with zipfile.ZipFile(io.BytesIO(original[f"actions/artifacts/{spec['artifacts']['image']['id']}/zip"])) as archive:
                for kind in ('oci', 'docker', 'evidence'):
                    raw = (original[f"actions/artifacts/{spec['artifacts']['evidence']['id']}/zip"] if kind == 'evidence'
                           else archive.read('builder.' + kind + '.tar'))
                    suffix = '.evidence.zip' if kind == 'evidence' else '.' + kind + '.tar'
                    item = self.asset('oci-builder-linux-' + arch + suffix, raw)
                    self.config['architectures'][arch][kind] = item
                    proof['files'][item['name']] = {'sha256': item['digest'][7:], 'bytes': item['size']}
        self.config['provenance'] = self.asset('accepted-builder-provenance.json', fixtures.json_bytes(provenance))
        self.release = {'id': 77, 'tag_name': self.config['tag'], 'draft': False, 'prerelease': False,
                        'published_at': '2026-11-04T00:00:00Z', 'assets': self.assets}
        self.responses[('releases/77', False)] = fixtures.json_bytes(self.release)
        self.ref = {'object': {'type': 'commit', 'sha': self.config['source'],
                    'url': f"https://api.github.com/repos/{accepted.REPOSITORY}/git/commits/{self.config['source']}"}}
        self.responses[('git/ref/tags/v0.1.12', False)] = fixtures.json_bytes(self.ref)
        self.fixture.transport_path.write_text(json.dumps(self.config))

    def asset(self, name, raw):
        spec = {'id': 10000 + len(self.assets), 'name': name, 'size': len(raw),
                'digest': 'sha256:' + fixtures.sha(raw)}
        metadata = {**spec, 'state': 'uploaded'}
        self.assets.append(metadata)
        path = 'releases/assets/' + str(spec['id'])
        self.responses[(path, False)] = fixtures.json_bytes(metadata)
        self.responses[(path, True)] = raw
        return spec

    def transport(self, path, output, maximum, check, *, media=False):
        self.calls.append((path, media))
        self.assertFalse(path.startswith('actions/'), 'retained transport must never call Actions')
        raw = self.responses[(path, media)]
        self.assertLessEqual(len(raw), maximum)
        output.write(raw)
        return len(raw), fixtures.sha(raw)

    def acquire(self, arch='amd64', name='input'):
        with patch.object(accepted, 'stream_api', side_effect=self.transport):
            return accepted.acquire(self.lock, arch, self.fixture.root / name, lambda: None)

    def test_both_architectures_use_original_retained_bytes_without_actions(self):
        for arch in ('amd64', 'arm64'):
            proof = self.acquire(arch, arch)
            self.assertEqual(proof['config'], self.lock['architectures'][arch]['config'])
            self.assertTrue((self.fixture.root / arch / accepted.OCI_ARCHIVE).is_file())
            self.assertFalse((self.fixture.root / arch / 'image.zip').exists())
        self.assertTrue(self.calls)
        self.assertTrue(all(not path.startswith('actions/') for path, _ in self.calls))

    def test_wrong_release_identity_never_falls_back(self):
        for field, value in [('id', 99), ('draft', True), ('prerelease', True),
                             ('tag_name', 'v9.9.9'), ('published_at', None), ('assets', [])]:
            with self.subTest(field=field):
                self.responses[('releases/77', False)] = fixtures.json_bytes({**self.release, field: value})
                with self.assertRaises(ValueError):
                    self.acquire(name='bad-' + field)
        self.assertTrue(all(not path.startswith('actions/') for path, _ in self.calls))

    def test_wrong_tag_commit_and_missing_transport_fail_closed(self):
        self.responses[('git/ref/tags/v0.1.12', False)] = fixtures.json_bytes({'object': {'type': 'tag', 'sha': 'c'*40}})
        with self.assertRaisesRegex(ValueError, 'source commit'):
            self.acquire()
        self.fixture.transport_path.unlink()
        with self.assertRaises(FileNotFoundError):
            accepted.read_transport()

    def test_wrong_asset_id_name_hash_and_size_rejected(self):
        for field, value in [('id', 123), ('name', 'other.oci.tar'), ('digest', 'sha256:' + '0'*64), ('size', 1)]:
            with self.subTest(field=field):
                config = copy.deepcopy(self.config)
                config['architectures']['amd64']['oci'][field] = value
                self.fixture.transport_path.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    self.acquire(name='bad-' + field)

    def test_changed_retained_body_and_asset_state_rejected(self):
        spec = self.config['architectures']['amd64']['oci']
        path = 'releases/assets/' + str(spec['id'])
        original = self.responses[(path, True)]
        self.responses[(path, True)] = b'corrupt'
        with self.assertRaisesRegex(ValueError, 'bytes differ'):
            self.acquire(name='bad-body')
        self.responses[(path, True)] = original
        self.responses[(path, False)] = fixtures.json_bytes({**spec, 'state': 'new'})
        with self.assertRaisesRegex(ValueError, 'metadata changed'):
            self.acquire(name='bad-state')

    def test_consumer_receipt_is_bound_to_selected_transport(self):
        with patch.object(accepted, 'stream_api', side_effect=self.transport):
            receipt = accepted.prepare(self.lock, 'amd64', self.fixture.root / 'prepared', lambda: None)
        receipt.update(import_verified=True, import_tag_absent_before_load=True, import_started=True,
                       import_cleanup={'absent': True})
        accepted.check_receipt(receipt, self.lock, 'amd64')
        self.fixture.transport_path.write_text('{"schema":1,"kind":"actions"}')
        with self.assertRaisesRegex(ValueError, 'receipt differs'):
            accepted.check_receipt(receipt, self.lock, 'amd64')

    def test_api_does_not_allow_latest_arbitrary_urls_or_binary_metadata(self):
        for path, media in [('releases/latest', False), ('https://example.invalid/file', False),
                            ('actions/runs/12', True), ('git/ref/tags/v0.1.12', True)]:
            with self.subTest(path=path), patch.object(accepted.subprocess, 'Popen') as process, self.assertRaises(ValueError):
                accepted.stream_api(path, io.BytesIO(), 100, lambda: None, media=media)
            process.assert_not_called()

    def test_snapshot_hash_and_selection_are_from_the_same_immutable_bytes(self):
        raw = self.fixture.transport_path.read_bytes()
        snapshot = accepted.read_transport()
        self.fixture.transport_path.write_text('{"schema":1,"kind":"actions"}')
        selection = snapshot.selection
        selection['kind'] = 'actions'
        self.assertEqual(snapshot.receipt(), {'lock_sha256': fixtures.sha(raw), 'selection': self.config})
        with self.assertRaises(FrozenInstanceError):
            snapshot.raw = b'changed'
        with self.assertRaisesRegex(ValueError, 'transport changed'):
            accepted.require_transport_unchanged(snapshot)

    def test_last_release_download_transport_drift_fails_before_materialization(self):
        last = 'releases/assets/' + str(self.config['architectures']['amd64']['evidence']['id'])
        def drift(path, output, maximum, check, *, media=False):
            result = self.transport(path, output, maximum, check, media=media)
            if path == last and media:
                self.fixture.transport_path.write_text('{"schema":1,"kind":"actions"}')
            return result
        with patch.object(accepted, 'stream_api', side_effect=drift), \
             patch.object(accepted.oci, 'materialize') as materialize, \
             self.assertRaisesRegex(ValueError, 'transport changed'):
            accepted.prepare(self.lock, 'amd64', self.fixture.root / 'drift', lambda: None)
        materialize.assert_not_called()
        self.assertTrue(all(not path.startswith('actions/') for path, _ in self.calls))

    def test_transport_drift_during_materialization_fails_before_receipt(self):
        real = accepted.oci.materialize
        def drift(*args):
            result = real(*args)
            self.fixture.transport_path.write_text('{"schema":1,"kind":"actions"}')
            return result
        with patch.object(accepted, 'stream_api', side_effect=self.transport), \
             patch.object(accepted.oci, 'materialize', side_effect=drift), \
             self.assertRaisesRegex(ValueError, 'transport changed'):
            accepted.prepare(self.lock, 'amd64', self.fixture.root / 'drift', lambda: None)

    def test_transport_drift_after_prepare_prevents_import(self):
        destination = self.fixture.root / 'prepared'
        with patch.object(accepted, 'stream_api', side_effect=self.transport):
            receipt = accepted.prepare(self.lock, 'amd64', destination, lambda: None)
        self.fixture.transport_path.write_text('{"schema":1,"kind":"actions"}')
        with patch.object(accepted.image, 'begin_import') as begin, \
             self.assertRaisesRegex(ValueError, 'changed before import'):
            accepted.load(None, destination, self.lock['architectures']['amd64'], receipt)
        begin.assert_not_called()
