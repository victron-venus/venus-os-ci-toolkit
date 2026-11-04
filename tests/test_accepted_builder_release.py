"""Release the verified original archives without rebuilding or running images."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_accepted_builder as accepted
from scripts import publish_verified as publisher
from scripts import stage_accepted_builder_release as release
from scripts import version_plan, version_receipt
from tests import test_accepted_builder as fixtures


class AcceptedBuilderReleaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AcceptedBuilderTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.responses = self.fixture.fixture('amd64') | self.fixture.fixture('arm64')
        self.lock = self.fixture.lock
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / 'release-dist'
        self.output.mkdir()
        source = self.output / 'venus-os-ci-toolkit-0.1.12-beta.1.tar.gz'
        source.write_bytes(b'verified source fixture')
        self.source_name = source.name
        self.initial_checksums = hashlib.sha256(source.read_bytes()).hexdigest() + '  ' + source.name + '\n'
        (self.output / release.CHECKSUMS).write_text(self.initial_checksums)
        self.identity = {'source': 'a' * 40, 'run': 88, 'attempt': 1}
        self.calls = []

    def transport(self, path, output, maximum, check):
        self.calls.append(path)
        check()
        raw = self.responses[path]
        self.assertLessEqual(len(raw), maximum)
        output.write(raw)
        return len(raw), hashlib.sha256(raw).hexdigest()

    def stage(self):
        with patch.object(accepted, 'stream_api', side_effect=self.transport):
            return release.stage(self.output, self.lock, self.identity, lambda: None)

    def test_exact_archives_evidence_and_complete_checksum_inventory(self):
        with patch.object(accepted.image, 'begin_import', side_effect=AssertionError('must not load')), \
             patch.object(accepted.oci, 'materialize', side_effect=AssertionError('must not materialize')):
            result = self.stage()
        self.assertEqual(result['release'], self.identity)
        self.assertEqual(result['qualification']['source'], self.lock['source'])
        self.assertNotEqual(result['release']['source'], result['qualification']['source'])
        for arch in ('amd64', 'arm64'):
            spec = self.lock['architectures'][arch]
            for kind in ('oci', 'docker'):
                path = self.output / f'oci-builder-linux-{arch}.{kind}.tar'
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), spec[kind]['archive_sha256'])
            evidence = self.output / f'oci-builder-linux-{arch}.evidence.zip'
            self.assertEqual(hashlib.sha256(evidence.read_bytes()).hexdigest(), spec['artifacts']['evidence']['digest'][7:])
        sums = dict(line.split('  ')[::-1] for line in (self.output / release.CHECKSUMS).read_text().splitlines())
        self.assertEqual(set(sums), {p.name for p in self.output.iterdir()} - {release.CHECKSUMS})
        for name, digest in sums.items():
            self.assertEqual(hashlib.sha256((self.output / name).read_bytes()).hexdigest(), digest)
        self.assertFalse(list(self.root.glob('.accepted-builder-*')))
        amd = f"actions/artifacts/{self.lock['architectures']['amd64']['artifacts']['image']['id']}/zip"
        arm = f"actions/artifacts/{self.lock['architectures']['arm64']['artifacts']['image']['id']}/zip"
        self.assertLess(self.calls.index(amd), self.calls.index(arm))

    def test_corrupt_second_architecture_leaves_original_source_only(self):
        spec = self.lock['architectures']['arm64']['artifacts']['image']
        self.responses[f"actions/artifacts/{spec['id']}/zip"] = b'corrupted'
        with self.assertRaisesRegex(ValueError, 'bytes differ'):
            self.stage()
        self.assertEqual({p.name for p in self.output.iterdir()}, {self.source_name, release.CHECKSUMS})
        self.assertEqual((self.output / release.CHECKSUMS).read_text(), self.initial_checksums)
        self.assertFalse(list(self.root.glob('.accepted-builder-*')))

    def test_changed_architecture_or_identity_cannot_be_published(self):
        self.lock['architectures']['arm64']['config'] = self.lock['architectures']['amd64']['config']
        with self.assertRaises(ValueError):
            self.stage()
        self.assertEqual(len(list(self.output.iterdir())), 2)

    def test_partial_publication_copy_is_removed_on_error(self):
        real = release.os.link
        calls = []
        def fail_second(source, target):
            calls.append(target)
            if len(calls) == 2:
                raise OSError('disk full')
            real(source, target)
        with patch.object(release.os, 'link', side_effect=fail_second), self.assertRaises(OSError):
            self.stage()
        self.assertEqual(len(list(self.output.iterdir())), 2)
        self.assertEqual((self.output / release.CHECKSUMS).read_text(), self.initial_checksums)

    def test_source_tamper_and_unexpected_assets_fail_before_network(self):
        (self.output / self.source_name).write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'checksum differs'):
            self.stage()
        self.assertEqual(self.calls, [])
        (self.output / 'extra').write_bytes(b'foreign')
        with self.assertRaisesRegex(ValueError, 'only the verified'):
            self.stage()
        self.assertEqual((self.output / 'extra').read_bytes(), b'foreign')

    def test_cleanup_error_after_checksum_commit_preserves_complete_set_and_error(self):
        temporary_directory = tempfile.TemporaryDirectory
        class CleanupError:
            def __init__(self, *args, **kwargs):
                self.directory = temporary_directory(*args, **kwargs)
            def __enter__(self):
                return self.directory.__enter__()
            def __exit__(self, *args):
                self.directory.__exit__(*args)
                raise OSError('injected scratch cleanup failure')
        with patch.object(release.tempfile, 'TemporaryDirectory', CleanupError), \
             self.assertRaisesRegex(OSError, 'injected scratch cleanup failure'):
            self.stage()
        sums = dict(line.split('  ')[::-1] for line in (self.output / release.CHECKSUMS).read_text().splitlines())
        self.assertEqual(len(sums), 8)
        self.assertEqual(set(sums), {p.name for p in self.output.iterdir()} - {release.CHECKSUMS})
        for name, digest in sums.items():
            self.assertEqual(hashlib.sha256((self.output / name).read_bytes()).hexdigest(), digest)

    def test_first_architecture_mutation_during_second_acquisition_is_rejected(self):
        acquire = accepted.acquire
        def mutate(lock, arch, destination, check):
            result = acquire(lock, arch, destination, check)
            if arch == 'arm64':
                (destination.parent / 'amd64' / accepted.OCI_ARCHIVE).write_bytes(b'changed later')
            return result
        with patch.object(accepted, 'acquire', side_effect=mutate), \
             self.assertRaisesRegex(ValueError, 'changed before staging'):
            self.stage()
        self.assertEqual(len(list(self.output.iterdir())), 2)

    def test_hosted_release_identity_is_separate_and_exact(self):
        environment = {'RUNNER_ENVIRONMENT': 'github-hosted', 'GITHUB_REPOSITORY': accepted.REPOSITORY,
                       'GITHUB_SHA': self.identity['source'], 'GITHUB_RUN_ID': '88', 'GITHUB_RUN_ATTEMPT': '1'}
        self.assertEqual(release.release_identity(environment), self.identity)
        for key, value in [('RUNNER_ENVIRONMENT', 'self-hosted'), ('GITHUB_SHA', 'main'),
                           ('GITHUB_RUN_ID', '../88'), ('GITHUB_REPOSITORY', 'fork/repo')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                release.release_identity({**environment, key: value})

    def test_exact_two_publisher_mappings_no_latest_and_no_overwrite(self):
        mapping = json.loads((release.ROOT / '.release-policy.json').read_text())['container_assets']
        expected = {f'oci-builder-linux-{a}.oci.tar': f'ghcr.io/victron-venus/oci-builder-{a}'
                    for a in ('amd64', 'arm64')}
        self.assertEqual(mapping, expected)
        self.stage()
        policy = {'container_assets': mapping}
        manifest = {'version': '0.1.12', 'source_policy': {'data': copy.deepcopy(policy)}}
        with patch.object(publisher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"Tags":[]}', '')):
            commands = publisher.container_commands(policy, manifest, self.output, latest=False)
        self.assertEqual(len(commands), 2)
        for command, (name, repository) in zip(commands, expected.items()):
            self.assertEqual(command, ['skopeo', 'copy', '--all', '--preserve-digests',
                                      'oci-archive:' + str(self.output / name), 'docker://' + repository + ':0.1.12'])
        with patch.object(publisher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"Tags":["0.1.12"]}', '')), \
             self.assertRaisesRegex(publisher.ReleaseError, 'already exists'):
            publisher.container_commands(policy, manifest, self.output, latest=False)
        policy['container_assets'] = {next(iter(mapping)): 'ghcr.io/victron-venus/wrong'}
        with self.assertRaisesRegex(publisher.ReleaseError, 'differs'):
            publisher.container_commands(policy, manifest, self.output, latest=False)

    def test_existing_version_receipt_covers_all_original_payloads(self):
        self.stage()
        (self.root / 'SOURCE_VERSION').write_text('0.1.12\n')
        policy = {'repository': accepted.REPOSITORY, 'mode': 'release',
                  'version_file': 'SOURCE_VERSION', 'versioning': {
                      'schema': 1, 'promotion': 'promote-bytes',
                      'files': [{'path': 'SOURCE_VERSION', 'format': 'text', 'value': 'package'}]}}
        plan = version_plan.create_plan('0.1.12', 'rc', 1, self.identity['source'], policy, 88)
        files = version_plan.sync_versions(self.root, policy, plan)
        plan_path = self.root / '.release-plan.json'
        inputs_path = self.root / '.release-inputs.json'
        plan_path.write_text(json.dumps(plan))
        inputs_path.write_text(json.dumps({'source_sha': plan['source_sha'],
            'plan_sha256': version_plan.plan_digest(plan), 'files': files,
            'effective_inputs_sha256': version_plan.effective_inputs_digest(files)}))
        receipt_path = self.output / 'release-inputs-package.json'
        with patch.object(version_receipt, 'capture_toolchain', return_value={'python': '3.12.0'}):
            version_receipt.create_receipt(plan_path, inputs_path, self.output, receipt_path)
        def payloads():
            return [{'name': p.name, 'size': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                    for p in self.output.iterdir()]
        verified = version_receipt.verify_receipts(self.output, plan, payloads(), policy)
        self.assertEqual(len(verified), 1)
        (self.output / 'oci-builder-linux-arm64.oci.tar').write_bytes(b'changed')
        changed_payloads = payloads()
        with self.assertRaisesRegex(ValueError, 'does not match'):
            version_receipt.verify_receipts(self.output, plan, changed_payloads, policy)
