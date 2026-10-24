"""Pinned source, archive and downstream build contracts, without native execution."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('runner_go_tools', ROOT / 'scripts/build_runner_go_tools.py')
TOOLS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOLS)


class RunnerGoToolTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(TOOLS.MANIFEST.read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, entries):
        target = self.root / 'source.tar.gz'
        with tarfile.open(target, 'w:gz') as archive:
            for name, kind, content in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                if kind == tarfile.SYMTYPE:
                    member.linkname = content
                    archive.addfile(member)
                else:
                    data = content.encode()
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
        return target

    def spec(self, archive, links=None):
        return {'root': 'upstream', 'archive_sha256': TOOLS.digest(archive),
                'allowed_symlinks': links or {}}

    def test_manifest_and_separate_lock_origin_bind_every_source(self):
        self.assertEqual(self.manifest['go_version'], 'go1.26.10')
        self.assertEqual(set(self.manifest['components']), set(TOOLS.REPOSITORIES))
        for name, spec in self.manifest['components'].items():
            TOOLS.validate_spec(name, spec)
            folder = TOOLS.LOCKS / name
            origin = json.loads((folder / 'inputs.json').read_text())
            self.assertEqual(origin['source'], {k: spec[k] for k in
                             ('repository', 'release_tag', 'commit', 'archive_sha256')})
            self.assertEqual(origin['fixed_locks'], spec['fixed_locks'])
            self.assertEqual(origin.get('retained_namesgenerator'), spec.get('retained_namesgenerator'))
            self.assertEqual(origin.get('runtime_test_fix'), spec.get('runtime_test_fix'))
            for lock in ('go.mod', 'go.sum'):
                self.assertEqual(TOOLS.digest(folder / lock), spec['fixed_locks'][lock])
            self.assertIn('golang.org/x/net v0.60.0', (folder / 'go.mod').read_text())
        self.assertIn('github.com/moby/go-archive v0.3.0',
                      (TOOLS.LOCKS / 'buildx/go.mod').read_text())
        self.assertNotIn('\tgithub.com/docker/docker ', (TOOLS.LOCKS / 'buildx/go.mod').read_text())

    def runtime_fixture(self):
        path = self.root / 'pkg/shim/shim_test.go'
        path.parent.mkdir(parents=True)
        before = (b'func TestRuntimeWithNonEmptyMaxEnvProcs(t *testing.T) {\n'
                  b'\tt.Setenv("GOMAXPROCS", "not_empty")\n\tsetRuntime()\n'
                  b'\tvar oldGoMaxProcs2 = runtime.GOMAXPROCS(0)\n'
                  b'\tif oldGoMaxProcs2 != runtime.NumCPU() {\n'
                  b'\t\tt.Fatal("the max number CPU should be equal to available CPUs")\n\t}\n}\n')
        after = before.replace(b'{\n', b'{\n\texpected := runtime.GOMAXPROCS(0)\n', 1).replace(
            b'!= runtime.NumCPU()', b'!= expected').replace(
            b'the max number CPU should be equal to available CPUs',
            b'setRuntime should preserve configured GOMAXPROCS')
        path.write_bytes(before)
        spec = {'repository': 'containerd/containerd', 'runtime_test_fix': {
            'before_sha256': hashlib.sha256(before).hexdigest(),
            'after_sha256': hashlib.sha256(after).hexdigest()}}
        return path, spec, before, after

    def test_runtime_correction_preserves_test_and_rechecks_result(self):
        path, spec, _, after = self.runtime_fixture()
        TOOLS.fix_runtime_test(self.root, spec)
        self.assertEqual(path.read_bytes(), after)
        self.assertIn(b't.Setenv("GOMAXPROCS", "not_empty")', after)
        TOOLS.verify_runtime_test(self.root, spec)
        path.write_bytes(after + b'// changed')
        with self.assertRaisesRegex(ValueError, 'changed during build'):
            TOOLS.verify_runtime_test(self.root, spec)

    def test_runtime_correction_rejects_source_or_result_drift_before_write(self):
        path, spec, before, _ = self.runtime_fixture()
        spec['runtime_test_fix']['after_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'result identity'):
            TOOLS.fix_runtime_test(self.root, spec)
        self.assertEqual(path.read_bytes(), before)
        path.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'source identity'):
            TOOLS.fix_runtime_test(self.root, spec)

    def test_runtime_correction_rejects_links_and_wrong_component(self):
        path, spec, before, _ = self.runtime_fixture()
        target = self.root / 'elsewhere'
        target.write_bytes(before)
        path.unlink()
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'source identity'):
            TOOLS.fix_runtime_test(self.root, spec)
        spec['repository'] = 'docker/buildx'
        with self.assertRaisesRegex(ValueError, 'test owner'):
            TOOLS.fix_runtime_test(self.root, spec)
        self.assertEqual(target.read_bytes(), before)

    def test_runtime_correction_is_bound_to_original_source_record(self):
        spec = copy.deepcopy(self.manifest['components']['containerd'])
        spec['runtime_test_fix']['before_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'Lock preparation binding'):
            TOOLS.install_locks(self.root, TOOLS.LOCKS / 'containerd', spec)

    def retained_fixture(self):
        source = self.root / 'source'
        vendor = source / 'vendor/github.com/docker/docker'
        (vendor / 'pkg/namesgenerator').mkdir(parents=True)
        content = {'names-generator.go': b'package namesgenerator\n',
                   'LICENSE': b'original license\n', 'NOTICE': b'original notice\n'}
        for name, data in content.items():
            path = vendor / ('pkg/namesgenerator/' + name if name.endswith('.go') else name)
            path.write_bytes(data)
        importer = source / 'store/util.go'
        importer.parent.mkdir()
        before = b'package store\nimport "github.com/docker/docker/pkg/namesgenerator"\n'
        after = before.replace(b'github.com/docker/docker/pkg/namesgenerator',
                               b'github.com/docker/buildx/internal/namesgenerator')
        importer.write_bytes(before)
        spec = {'repository': 'docker/buildx', 'retained_namesgenerator': {
            'files': {name: hashlib.sha256(data).hexdigest() for name, data in content.items()},
            'import_before_sha256': hashlib.sha256(before).hexdigest(),
            'import_after_sha256': hashlib.sha256(after).hexdigest()}}
        return source, spec, content, after

    def test_retained_utility_is_exact_copy_with_original_runtime_notices(self):
        source, spec, content, after = self.retained_fixture()
        TOOLS.retain_namesgenerator(source, spec)
        self.assertEqual((source / 'store/util.go').read_bytes(), after)
        for name, data in content.items():
            self.assertEqual((source / 'internal/namesgenerator' / name).read_bytes(), data)
        destination = self.root / 'notices'
        TOOLS.retained_utility_notices(source, destination, spec,
                                      {'github.com/docker/buildx/internal/namesgenerator'})
        for name in ('LICENSE', 'NOTICE'):
            self.assertEqual((destination / 'retained/namesgenerator' / name).read_bytes(), content[name])

    def test_changed_retained_utility_fails_before_any_copy(self):
        source, spec, _, _ = self.retained_fixture()
        (source / 'vendor/github.com/docker/docker/NOTICE').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'source identity'):
            TOOLS.retain_namesgenerator(source, spec)
        self.assertFalse((source / 'internal/namesgenerator').exists())

    def test_changed_import_or_result_fails_before_any_copy(self):
        source, spec, _, _ = self.retained_fixture()
        spec['retained_namesgenerator']['import_after_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'import result'):
            TOOLS.retain_namesgenerator(source, spec)
        self.assertFalse((source / 'internal/namesgenerator').exists())
        (source / 'store/util.go').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'import identity'):
            TOOLS.retain_namesgenerator(source, spec)

    def test_retained_utility_rejects_links_and_existing_destination(self):
        source, spec, _, _ = self.retained_fixture()
        (source / 'internal').symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'parent is a symlink'):
            TOOLS.retain_namesgenerator(source, spec)
        (source / 'internal').unlink()
        TOOLS.retain_namesgenerator(source, spec)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            TOOLS.retain_namesgenerator(source, spec)

    def test_legacy_module_or_missing_local_utility_rejects_provenance(self):
        source, spec, _, _ = self.retained_fixture()
        TOOLS.retain_namesgenerator(source, spec)
        for packages in ({'github.com/docker/buildx/internal/namesgenerator',
                          'github.com/docker/docker/pkg/namesgenerator'}, {'example/other'}):
            with self.subTest(packages=packages), self.assertRaisesRegex(ValueError, 'dependency graph'):
                TOOLS.retained_utility_notices(source, self.root / 'notices', spec, packages)

    def test_changed_retained_source_binding_is_rejected(self):
        spec = copy.deepcopy(self.manifest['components']['buildx'])
        spec['retained_namesgenerator']['import_before_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'Lock preparation binding'):
            TOOLS.install_locks(self.root, TOOLS.LOCKS / 'buildx', spec)

    def test_changed_utility_after_build_cannot_claim_original_provenance(self):
        source, spec, _, _ = self.retained_fixture()
        TOOLS.retain_namesgenerator(source, spec)
        (source / 'internal/namesgenerator/names-generator.go').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'content changed'):
            TOOLS.retained_utility_notices(source, self.root / 'notices', spec,
                                          {'github.com/docker/buildx/internal/namesgenerator'})

    def test_changed_import_after_build_cannot_claim_reduced_graph(self):
        source, spec, _, _ = self.retained_fixture()
        TOOLS.retain_namesgenerator(source, spec)
        (source / 'store/util.go').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'import changed during build'):
            TOOLS.retained_utility_notices(source, self.root / 'notices', spec,
                                          {'github.com/docker/buildx/internal/namesgenerator'})

    def test_source_change_without_lock_revalidation_fails(self):
        spec = copy.deepcopy(self.manifest['components']['buildx'])
        spec['commit'] = '0' * 40
        with self.assertRaisesRegex(ValueError, 'Lock source binding'):
            TOOLS.install_locks(self.root, TOOLS.LOCKS / 'buildx', spec)

    def test_changed_fixed_lock_rejected_before_upstream_is_overwritten(self):
        spec = copy.deepcopy(self.manifest['components']['buildx'])
        locks = self.root / 'locks'
        locks.mkdir()
        source = self.root / 'source'
        source.mkdir()
        for lock in ('go.mod', 'go.sum'):
            (source / lock).write_text('original')
            spec['original_locks'][lock] = TOOLS.digest(source / lock)
            (locks / lock).write_text('wrong')
        origin = json.loads((TOOLS.LOCKS / 'buildx/inputs.json').read_text())
        origin['original_locks'] = spec['original_locks']
        (locks / 'inputs.json').write_text(json.dumps(origin))
        with self.assertRaisesRegex(ValueError, 'Reviewed lock identity'):
            TOOLS.install_locks(source, locks, spec)
        self.assertEqual((source / 'go.mod').read_text(), 'original')

    def test_valid_regular_source_and_exact_internal_symlink(self):
        archive = self.archive([('upstream/real', tarfile.REGTYPE, 'data'),
                                ('upstream/link', tarfile.SYMTYPE, 'real')])
        target = self.root / 'unpacked'
        TOOLS.unpack(archive, target, self.spec(archive, {'link': 'real'}))
        self.assertEqual((target / 'link').read_text(), 'data')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            TOOLS.unpack(archive, target, self.spec(archive, {'link': 'real'}))

    def test_unsafe_members_fail_before_destination_exists(self):
        cases = [
            [('upstream/../escape', tarfile.REGTYPE, 'x')],
            [('/upstream/escape', tarfile.REGTYPE, 'x')],
            [('upstream/file', tarfile.REGTYPE, 'x'), ('upstream/file', tarfile.REGTYPE, 'y')],
            [('other/file', tarfile.REGTYPE, 'x')],
            [('upstream/link', tarfile.SYMTYPE, '../escape')],
            [('upstream/device', tarfile.CHRTYPE, '')],
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                archive = self.archive(entries)
                target = self.root / 'unpacked'
                with self.assertRaises(ValueError):
                    TOOLS.unpack(archive, target, self.spec(archive))
                self.assertFalse(target.exists())

    def test_digest_or_oversized_member_rejected(self):
        archive = self.archive([('upstream/file', tarfile.REGTYPE, 'data')])
        spec = self.spec(archive)
        spec['archive_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'digest'):
            TOOLS.unpack(archive, self.root / 'unpacked', spec)
        with patch.object(TOOLS, 'MAX_SOURCE', 1), self.assertRaisesRegex(ValueError, 'limits'):
            TOOLS.unpack(archive, self.root / 'unpacked', self.spec(archive))

    def test_writes_beneath_symlink_rejected(self):
        archive = self.archive([('upstream/alias', tarfile.SYMTYPE, 'real'),
                                ('upstream/alias/child', tarfile.REGTYPE, 'data')])
        with self.assertRaisesRegex(ValueError, 'through a symlink'):
            TOOLS.unpack(archive, self.root / 'out', self.spec(archive, {'alias': 'real'}))

    def test_builds_preserve_features_and_identify_downstream_version(self):
        for name, spec in self.manifest['components'].items():
            commands = TOOLS.build_commands(name, spec, Path('/out'))
            for command in commands:
                self.assertEqual(command[:5], ['go', 'build', '-p', '2', '-trimpath'])
                self.assertIn(spec['downstream_version'], command[command.index('-ldflags') + 1])
            if name == 'containerd':
                self.assertEqual(len(commands), 3)
                self.assertTrue(commands[-1][commands[-1].index('-tags') + 1].endswith(',no_grpc'))
                self.assertIn('static_build', commands[0][commands[0].index('-tags') + 1])
            if name == 'compose':
                self.assertEqual(commands[0][commands[0].index('-tags') + 1], 'e2e')
        env = TOOLS.go_environment('arm64')
        self.assertEqual(env['GOFLAGS'], '-mod=readonly')
        self.assertEqual(env['GOTOOLCHAIN'], 'local')
        self.assertEqual(env['CGO_ENABLED'], '0')
        self.assertEqual(env['GOARCH'], 'arm64')
        with self.assertRaises(ValueError):
            TOOLS.go_environment('unknown')

    def test_unit_scope_matches_upstream_integration_boundaries(self):
        packages = ['example/pkg', 'example/integration/client', 'example/pkg/e2e']
        self.assertEqual(TOOLS.test_packages('containerd', packages), ['example/pkg', 'example/pkg/e2e'])
        self.assertEqual(TOOLS.test_packages('compose', packages), ['example/pkg', 'example/integration/client'])
        self.assertEqual(TOOLS.test_packages('buildx', packages), packages)

    def test_public_downloads_cannot_inherit_private_module_exclusions(self):
        inherited = {name: 'untrusted.invalid' for name in
                     ('GOPRIVATE', 'GONOPROXY', 'GONOSUMDB', 'GOINSECURE',
                      'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')}
        with patch.dict(os.environ, inherited):
            environment = TOOLS.go_environment()
        self.assertFalse(set(inherited) & set(environment))
        self.assertEqual(environment['GOENV'], 'off')
        self.assertEqual(environment['GOPROXY'], 'https://proxy.golang.org')
        self.assertEqual(environment['GOSUMDB'], 'sum.golang.org')

    def test_target_cpu_baselines_do_not_inherit_builder_settings(self):
        with patch.dict(os.environ, {'GOAMD64': 'v4', 'GOARM64': 'v9.5'}):
            for architecture in (None, 'amd64', 'arm64'):
                with self.subTest(architecture=architecture):
                    environment = TOOLS.go_environment(architecture)
                    self.assertEqual(environment['GOAMD64'], 'v1')
                    self.assertEqual(environment['GOARM64'], 'v8.0')

    def test_ci_mode_is_exclusive_and_requires_linux_worker_temp(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            TOOLS.ci_directories('buildx')
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'RUNNER_OS': 'Linux',
                                     'RUNNER_TEMP': str(self.root)}):
            source, output = TOOLS.ci_directories('buildx')
            self.assertEqual(source.parent, output.parent)
            self.assertTrue(source.parent.is_relative_to(self.root))
            with self.assertRaises(FileExistsError):
                TOOLS.ci_directories('buildx')

    def test_source_and_lock_changes_cannot_skip_real_compilation(self):
        for path in ('.github/containers/runner-go-tools.json',
                     '.github/containers/runner-go-tools/buildx/go.sum',
                     '.github/containers/runner-go-tools/compose/inputs.json',
                     'scripts/build_runner_go_tools.py', 'deploy/runner-reserve/Dockerfile',
                     '.github/workflows/contract-tests.yml'):
            self.assertTrue(TOOLS.changed_inputs([path]), path)
        self.assertFalse(TOOLS.changed_inputs(['docs/README.md']))
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'push'}):
            self.assertTrue(TOOLS.ci_needs_build())
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'pull_request', 'BASE_REVISION': '--help'}):
            with self.assertRaisesRegex(ValueError, 'Full PR base'):
                TOOLS.ci_needs_build()


if __name__ == '__main__':
    unittest.main()
