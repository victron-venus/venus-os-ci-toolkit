"""Immutable builder consumption without Docker, network or executable downloads."""

import copy
import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from scripts import build_runner_image_artifacts as runner
from scripts import prepare_accepted_builder as accepted
from tests.test_oci_builder import packed


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return json.dumps(value, sort_keys=True).encode()


def zipped(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
    return output.getvalue()


class AcceptedBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.lock = accepted.read_lock()
        self.spec = self.lock['architectures']['amd64']

    def run_metadata(self):
        return {'id': self.lock['qualification_run'], 'head_sha': self.lock['source'],
                'run_attempt': 1, 'event': 'workflow_dispatch', 'status': 'completed',
                'conclusion': 'success', 'head_branch': 'main', 'path': accepted.WORKFLOW,
                'repository': {'full_name': accepted.REPOSITORY},
                'head_repository': {'full_name': accepted.REPOSITORY}}

    def metadata(self, expected):
        return {**expected, 'expired': False, 'expires_at': '2099-01-01T00:00:00Z',
                'workflow_run': {'id': self.lock['qualification_run'], 'head_sha': self.lock['source']}}

    def fixture(self):
        files = {name: b'fixed fixture bytes ' + name.encode() for name in (
            'usr/bin/buildkitd', 'usr/bin/buildctl', 'usr/bin/buildkit-runc',
            'usr/bin/oci-cgroup-reader', 'usr/share/licenses/oci-tools/runc/UPSTREAM-LICENSE')}
        layer = packed(files)
        runtime = {'Entrypoint': accepted.image.ENTRYPOINT, 'User': '0:0',
                   'Env': ['PATH=/usr/bin'], 'Volumes': {'/var/lib/buildkit': {}}}
        config = json_bytes({'os': 'linux', 'architecture': 'amd64', 'config': runtime,
                             'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + sha(layer)]}})
        manifest = json_bytes({'schemaVersion': 2,
            'config': {'mediaType': accepted.oci.CONFIG, 'digest': 'sha256:' + sha(config), 'size': len(config)},
            'layers': [{'mediaType': accepted.oci.LAYER, 'digest': 'sha256:' + sha(layer), 'size': len(layer)}]})
        index = json_bytes({'schemaVersion': 2, 'manifests': [{'mediaType': accepted.oci.MANIFEST,
                            'digest': 'sha256:' + sha(manifest), 'size': len(manifest)}]})
        oci_raw = packed({'oci-layout': json_bytes({'imageLayoutVersion': '1.0.0'}), 'index.json': index,
            'blobs/sha256/' + sha(config): config, 'blobs/sha256/' + sha(manifest): manifest,
            'blobs/sha256/' + sha(layer): layer})
        docker = packed({'manifest.json': json_bytes([{'Config': 'config.json',
            'RepoTags': [self.spec['embedded_import_tag']], 'Layers': ['layer.tar']}]),
            'config.json': config, 'layer.tar': layer})
        self.spec.update(config='sha256:' + sha(config), manifest='sha256:' + sha(manifest),
            oci={'archive_bytes': len(oci_raw), 'archive_sha256': sha(oci_raw),
                 'diff_ids': ['sha256:' + sha(layer)], 'files': {k: sha(v) for k, v in files.items()}},
            docker={'archive_bytes': len(docker), 'archive_sha256': sha(docker)})
        evidence_name = 'oci-builder-amd64/evidence/acceptance.json'
        evidence = json_bytes({'source': self.lock['source'], 'architecture': 'amd64',
            'qualified': True, 'runtime_passed': True,
            'oci': {'config': self.spec['config'], 'manifest': self.spec['manifest']}})
        self.spec['evidence_members'] = {evidence_name: sha(evidence)}
        blobs = {'image': zipped({'builder.oci.tar': oci_raw, 'builder.docker.tar': docker}),
                 'evidence': zipped({evidence_name: evidence})}
        responses = {f"actions/runs/{self.lock['qualification_run']}": json_bytes(self.run_metadata())}
        for kind, blob in blobs.items():
            spec = self.spec['artifacts'][kind]
            spec.update(size_in_bytes=len(blob), digest='sha256:' + sha(blob))
            path = f"actions/artifacts/{spec['id']}"
            responses[path] = json_bytes(self.metadata(spec))
            responses[path + '/zip'] = blob
        return responses

    def test_exact_provenance_and_expiry_are_required(self):
        accepted.validate_run(self.lock, self.run_metadata())
        for key, wrong in [('head_sha', '0' * 40), ('run_attempt', 2), ('conclusion', 'failure'),
                           ('event', 'pull_request'), ('id', 7), ('path', '.github/workflows/other.yml'),
                           ('repository', {'full_name': 'fork/repo'}), ('head_branch', 'feature')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                accepted.validate_run(self.lock, {**self.run_metadata(), key: wrong})
        spec = self.spec['artifacts']['image']
        for key, wrong in [('expired', True), ('id', 7), ('digest', 'sha256:' + '0' * 64),
                           ('size_in_bytes', 1), ('expires_at', '2000-01-01T00:00:00Z'),
                           ('workflow_run', {'id': 7, 'head_sha': self.lock['source']})]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                accepted.validate_artifact(self.lock, spec, {**self.metadata(spec), key: wrong})

    def test_actual_archive_verification_and_layout_materialization(self):
        responses = self.fixture()
        def transport(path, output, maximum, check):
            raw = responses[path]
            self.assertLessEqual(len(raw), maximum)
            output.write(raw)
            return len(raw), sha(raw)
        destination = self.root / 'inputs'
        with patch.object(accepted, 'stream_api', side_effect=transport):
            receipt = accepted.prepare(self.lock, 'amd64', destination, lambda: None)
        self.assertEqual(receipt['config'], self.spec['config'])
        self.assertEqual(receipt['files'], self.spec['oci']['files'])
        self.assertTrue((destination / 'layout/index.json').is_file())
        self.assertFalse((destination / 'image.zip').exists())
        self.assertFalse((destination / 'builder.oci.tar').exists())
        self.assertTrue((destination / 'builder.docker.tar').is_file())

    def test_corrupt_download_never_reaches_verifier_or_import(self):
        responses = self.fixture()
        responses[f"actions/artifacts/{self.spec['artifacts']['image']['id']}/zip"] = b'corrupted'
        def transport(path, output, maximum, check):
            raw = responses[path]
            output.write(raw)
            return len(raw), sha(raw)
        with patch.object(accepted, 'stream_api', side_effect=transport), \
             patch.object(accepted.image, 'verify_oci') as verify, self.assertRaisesRegex(ValueError, 'bytes differ'):
            accepted.prepare(self.lock, 'amd64', self.root / 'bad', lambda: None)
        verify.assert_not_called()

    def test_zip_members_reject_aliases_links_directories_encryption_and_duplicates(self):
        cases = [('../escape', 0, 0), ('/absolute', 0, 0), ('x//y', 0, 0),
                 ('x\\y', 0, 0), ('x/', 0, 0), ('x', stat.S_IFLNK | 0o777, 0),
                 ('x', stat.S_IFREG | 0o600, 1)]
        for name, mode, flags in cases:
            entry = zipfile.ZipInfo(name)
            entry.external_attr = mode << 16
            entry.flag_bits = flags
            archive = Mock()
            archive.infolist.return_value = [entry]
            with self.subTest(name=name, mode=mode, flags=flags), self.assertRaises(ValueError):
                accepted.members(archive, {name}, 100)
        archive.infolist.return_value = [zipfile.ZipInfo('x'), zipfile.ZipInfo('x')]
        with self.assertRaises(ValueError):
            accepted.members(archive, {'x'}, 100)

    def test_inner_hash_and_evidence_mutation_are_rejected(self):
        responses = self.fixture()
        image_path = self.root / 'image.zip'
        image_path.write_bytes(responses[f"actions/artifacts/{self.spec['artifacts']['image']['id']}/zip"])
        bad = copy.deepcopy(self.spec)
        bad['oci']['archive_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'digest differs'):
            accepted.unpack_images(image_path, self.root, bad, lambda: None)
        evidence_path = self.root / 'evidence.zip'
        evidence_path.write_bytes(responses[f"actions/artifacts/{self.spec['artifacts']['evidence']['id']}/zip"])
        self.spec['evidence_members']['oci-builder-amd64/evidence/acceptance.json'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            accepted.verify_evidence(evidence_path, self.lock, 'amd64')

    def test_transport_enforces_byte_bound_and_reaps_child(self):
        real_popen = subprocess.Popen
        children = []
        def child(*args, **kwargs):
            process = real_popen([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"x" * 20000)'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            children.append(process)
            return process
        with patch.object(accepted.subprocess, 'Popen', side_effect=child), \
             self.assertRaisesRegex(ValueError, 'byte limit'):
            accepted.stream_api('actions/artifacts/1/zip', io.BytesIO(), 100, lambda: None)
        self.assertIsNotNone(children[0].poll())

    def test_failed_import_is_reconciled_but_foreign_image_is_never_removed(self):
        supervisor, record = Mock(env={}), {'architecture': 'amd64'}
        absent = subprocess.CompletedProcess([], 1, '[]',
            'Error: No such image: ' + self.spec['embedded_import_tag'])
        supervisor.run.side_effect = TimeoutError('load timed out after daemon accepted bytes')
        with patch.object(accepted.image.subprocess, 'run', return_value=absent), self.assertRaises(TimeoutError):
            accepted.load(supervisor, self.root, self.spec, record)
        self.assertTrue(record['import_started'])
        foreign = subprocess.CompletedProcess([], 0, json.dumps([{'Id': 'sha256:' + '0' * 64}]), '')
        with patch.object(accepted.image.subprocess, 'run', return_value=foreign) as command, \
             self.assertRaisesRegex(ValueError, 'tag changed'):
            accepted.cleanup(supervisor, self.spec, record)
        self.assertEqual(command.call_count, 1)
        owned = subprocess.CompletedProcess([], 0, json.dumps([{'Id': self.spec['config']}]), '')
        with patch.object(accepted.image.subprocess, 'run', side_effect=[owned, Mock()]) as command:
            accepted.cleanup(supervisor, self.spec, record)
        self.assertEqual(command.call_args.args[0], ['docker', 'image', 'rm', self.spec['embedded_import_tag']])
        self.assertTrue(record['import_cleanup']['removed'])

    def test_manifest_only_accepts_reviewed_image_lock(self):
        manifest = runner.document(runner.MANIFEST)
        runner.validate_manifest(manifest)
        for key, value in [('image', 'sha256:' + '0' * 64), ('kind', 'arbitrary'), ('lock_sha256', '0' * 64)]:
            changed = copy.deepcopy(manifest)
            changed['tools']['buildkit'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                runner.validate_manifest(changed)

    def test_public_command_requires_exact_additional_context(self):
        manifest = runner.document(runner.MANIFEST)
        identity = {'source_commit': 'a' * 40, 'repository': accepted.REPOSITORY}
        with self.assertRaisesRegex(ValueError, 'Verified builder layout'):
            runner.build_command('public', manifest, identity, 'owned', self.root, self.root / 'out')
        command = runner.build_command('public', manifest, identity, 'owned', self.root, self.root / 'out',
                                       tools_layout=self.root / 'verified-layout')
        self.assertIn('accepted-tools=oci-layout://' + str(self.root / 'verified-layout') + '@' + self.spec['manifest'], command)
        self.assertNotIn('--push', command)
        dockerfile = (runner.ROOT / 'deploy/runner-reserve/Dockerfile').read_text()
        self.assertIn('COPY --from=accepted-tools /usr/bin/buildkit-runc /usr/local/bin/runc', dockerfile)

    def test_final_runc_inventory_cannot_be_missing_partial_or_old(self):
        packages = [{'Name': n, **({'Version': v} if v is not None else {})}
                    for n, v in self.spec['runc_packages']]
        row = {'Class': 'lang-pkgs', 'Type': 'gobinary', 'Target': 'usr/local/bin/runc', 'Packages': packages}
        self.assertEqual(runner.runc_packages({'Results': [row]}), runner.SCAN_POLICY['runc_packages'])
        for rows in ([], [row, row], [{**row, 'Packages': packages[:-1]}],
                     [{**row, 'Packages': [{'Name': 'golang.org/x/net', 'Version': 'v0.55.0'}]}]):
            self.assertNotEqual(runner.runc_packages({'Results': rows}), runner.SCAN_POLICY['runc_packages'])
        self.assertEqual(runner.SCAN_POLICY['runc']['sha256'], self.spec['oci']['files']['usr/bin/buildkit-runc'])
        self.assertFalse(any(rule[0] == 'runc' for rule in runner.SCAN_POLICY['advisories'].values()))


if __name__ == '__main__':
    unittest.main()
