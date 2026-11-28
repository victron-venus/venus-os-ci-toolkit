"""The pre-dispatch check reports stale identity pins without an image build."""

import copy
import io
import json
import lzma
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from scripts import build_runner_image_artifacts as build
from scripts import check_runner_scan_policy as check


def packages_text(name, version, architecture='amd64'):
    return ('Package: ' + name + '\n'
            'Architecture: ' + architecture + '\n'
            'Version: ' + version + '\n'
            'Filename: pool/main/' + name + '\n'
            '\n')


def scan_report(name, version, release, arch='amd64', source=None):
    source = source or version
    return {'Results': [{
        'Target': '/input/image (ubuntu 24.04)', 'Class': 'os-pkgs', 'Type': 'ubuntu',
        'Packages': [{'Name': name, 'Version': version, 'Release': release, 'Arch': arch,
                      'SrcName': name, 'SrcVersion': source,
                      'SrcRelease': release}],
    }]}


class ScanPolicyCheckTests(unittest.TestCase):
    def setUp(self):
        self.pins = check.policy_pins(build.SCAN_POLICY)
        self.assertEqual([pin['component'] for pin in self.pins], ['headers'])
        self.pin = self.pins[0]
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def run_check(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = check.main([str(argument) for argument in arguments])
        return status, stdout.getvalue(), stderr.getvalue()

    def stale_version(self):
        return self.pin['version'].split('-')[0] + '-999.999'

    def write_index(self, version, name='Packages.xz'):
        path = self.root / name
        path.write_bytes(lzma.compress(packages_text(self.pin['package'], version).encode()))
        return path

    def test_current_local_index_is_accepted(self):
        status, stdout, stderr = self.run_check(
            '--no-network', '--ubuntu-index', self.write_index(self.pin['version']))
        self.assertEqual(status, 0, stderr)
        self.assertIn(self.pin['package'] + ' ' + self.pin['version'], stdout)
        self.assertIn('match every checked source', stdout)

    def test_stale_local_index_names_both_versions(self):
        stale = self.stale_version()
        status, stdout, stderr = self.run_check(
            '--no-network', '--ubuntu-index', self.write_index(stale))
        self.assertEqual(status, 1)
        self.assertIn(stale, stderr)
        self.assertIn(self.pin['version'], stderr)
        self.assertIn('RUNNER_SCAN_APPLICABILITY.json', stderr)
        self.assertIn('finding_identity_changed', stderr)
        self.assertNotIn('match every checked source', stdout + stderr)

    def test_index_without_the_pinned_package_is_not_a_pass(self):
        path = self.root / 'Packages'
        path.write_text(packages_text('unrelated-package', '1.0'))
        status, stdout, stderr = self.run_check('--no-network', '--ubuntu-index', path)
        self.assertEqual(status, 2)
        self.assertIn('unverified:', stderr)
        self.assertNotIn('match every checked source', stdout)

    def test_unreadable_index_is_not_a_pass(self):
        path = self.root / 'Packages.xz'
        path.write_bytes(b'not an xz stream')
        status, stdout, stderr = self.run_check('--no-network', '--ubuntu-index', path)
        self.assertEqual(status, 2)
        self.assertIn('could not read', stderr)
        self.assertNotIn('match every checked source', stdout)

    def test_pockets_choose_the_highest_version_before_comparing(self):
        paths = [self.write_index('6.8.0-31.31', 'noble.xz'),
                 self.write_index(self.pin['version'], 'updates.xz')]
        arguments = ['--no-network']
        for path in paths:
            arguments += ['--ubuntu-index', path]
        status, stdout, stderr = self.run_check(*arguments)
        self.assertEqual(status, 0, stderr)
        self.assertIn('noble.xz', stdout)
        self.assertIn('updates.xz', stdout)

    def test_retained_report_with_the_newer_identity_is_stale(self):
        report = self.root / 'scan.json'
        version, release = self.stale_version().split('-', 1)
        report.write_text(json.dumps(scan_report(self.pin['package'], version, release)))
        status, stdout, stderr = self.run_check(
            '--no-network', '--scan-report', report)
        self.assertEqual(status, 1)
        self.assertIn(self.stale_version(), stderr)
        self.assertIn('records ' + self.pin['package'], stderr)

    def test_retained_report_with_the_pinned_identity_is_accepted(self):
        report = self.root / 'scan.json'
        upstream, release = self.pin['version'].split('-', 1)
        report.write_text(json.dumps(scan_report(self.pin['package'], upstream, release)))
        status, stdout, stderr = self.run_check(
            '--no-network', '--scan-report', report)
        self.assertEqual(status, 0, stderr)
        self.assertIn(str(report), stdout)

    def test_report_without_the_package_is_not_a_pass(self):
        report = self.root / 'scan.json'
        report.write_text(json.dumps(scan_report('unrelated-package', '1.0', '1')))
        status, stdout, stderr = self.run_check(
            '--no-network', '--scan-report', report)
        self.assertEqual(status, 2)
        self.assertIn('has no ' + self.pin['package'], stderr)

    def test_network_sources_are_checked_by_default(self):
        text = packages_text(self.pin['package'], self.stale_version())
        with mock.patch.object(check, 'fetch_index', return_value=text) as fetch:
            status, stdout, stderr = self.run_check()
        self.assertEqual(status, 1)
        self.assertEqual(fetch.call_count, len(check.DEFAULT_SOURCES))
        self.assertIn(self.stale_version(), stderr)

    def test_failed_download_is_unverified_rather_than_stale(self):
        with mock.patch.object(check, 'fetch_index', side_effect=OSError('offline')):
            status, stdout, stderr = self.run_check()
        self.assertEqual(status, 2)
        self.assertIn('could not download', stderr)
        self.assertNotIn('mismatch:', stderr)

    def test_no_network_without_indexes_checks_only_the_documents(self):
        status, stdout, stderr = self.run_check('--no-network')
        self.assertEqual(status, 0, stderr)
        self.assertIn('indexes were not checked', stdout)

    def test_document_pin_disagreement_is_reported(self):
        policy = copy.deepcopy(build.SCAN_POLICY)
        policy['identities']['headers']['version'] = self.stale_version()
        pins, problems = check.check_policy(
            policy, {'header_provenance': {'package': self.pin['package'],
                                           'version': self.pin['version'],
                                           'architecture': self.pin['architecture']}},
            check.DOCKERFILE.read_text())
        self.assertEqual(len(pins), 1)
        self.assertTrue(any('header_provenance' in problem for problem in problems))

    def test_unexpected_base_image_is_reported(self):
        _, problems = check.check_policy(
            build.SCAN_POLICY, {'header_provenance': {}}, 'FROM scratch\n')
        self.assertTrue(any('base image' in problem for problem in problems))

    def test_policy_block_must_agree_with_the_identity(self):
        policy = copy.deepcopy(build.SCAN_POLICY)
        policy['headers']['version'] = self.stale_version()
        _, problems = check.check_policy(policy, {'header_provenance': {}}, check.DOCKERFILE.read_text())
        self.assertTrue(any('block pins' in problem for problem in problems))

    def test_version_ordering_follows_the_release_pockets(self):
        self.assertGreater(check.version_key('6.8.0-151.151'), check.version_key('6.8.0-147.147'))
        self.assertGreater(check.version_key('2.8.4ubuntu0.1'), check.version_key('2.8.3'))
        self.assertEqual(check.version_key('6.8.0-147.147'), check.version_key('6.8.0-147.147'))


if __name__ == '__main__':
    unittest.main()
