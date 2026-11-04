"""Pinned source, archive and downstream build contracts, without native execution."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import time
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('runner_go_tools', ROOT / 'scripts/build_runner_go_tools.py')
TOOLS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOLS)


class RunnerGoToolTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'linux', 'Real image diagnostics run on Linux')
    def test_storage_diagnostic_output_and_lifetime_are_bounded(self):
        with patch.object(TOOLS.os, 'killpg', wraps=os.killpg) as kill:
            self.assertEqual(TOOLS.bounded_readonly_command(
                [sys.executable, '-c', 'print(123)']), b'123\n')
            kill.assert_called_once()
        cases = [
            ('import os,time; os.write(1,b"x"*8192); time.sleep(30)', ValueError),
            # The leader exits immediately while its descendant retains stdout.
            ('import os,time; child=os.fork(); time.sleep(30) if child == 0 else os._exit(0)', TimeoutError),
        ]
        for source, error in cases:
            with self.subTest(error=error), patch.object(TOOLS.os, 'killpg', wraps=os.killpg) as kill:
                with self.assertRaises(error):
                    TOOLS.bounded_readonly_command([sys.executable, '-c', source], maximum=32, seconds=0.5)
                kill.assert_called_once()
                self.assertEqual(kill.call_args.args[1], signal.SIGKILL)

    @unittest.skipUnless(sys.platform == 'linux', 'Real image diagnostics run on Linux')
    def test_successful_diagnostic_terminates_child_that_closed_output(self):
        pid_file = self.root / 'child'
        # The child signals readiness before the parent exits; it has no output
        # pipe left to keep the collector's select loop alive.
        code = ('import os,time\nfrom pathlib import Path\n'
                f'p=Path({str(pid_file)!r})\n'
                'if os.fork() == 0:\n'
                ' os.close(1); os.close(2); p.write_text(str(os.getpid())); time.sleep(5); os._exit(0)\n'
                'while not p.exists(): time.sleep(.005)\n')
        TOOLS.bounded_readonly_command([sys.executable, '-c', code], seconds=1)
        child = int(pid_file.read_text())
        deadline = time.monotonic() + 1
        while True:
            try:
                # A zombie cannot run and no longer owns open resources.
                status = Path(f'/proc/{child}/stat').read_text().rsplit(')', 1)[1].split()[0]
            except FileNotFoundError:
                break
            if status == 'Z':
                break
            if time.monotonic() >= deadline:
                self.fail('Diagnostic left a running descendant')
            time.sleep(.01)

    def test_diagnostic_requires_linux_waitid_and_default_child_handler(self):
        with patch.object(TOOLS.sys, 'platform', 'darwin'), patch.object(TOOLS.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(ValueError, 'Linux waitid'):
                TOOLS.bounded_readonly_command(['unused'])
            launch.assert_not_called()
        capabilities = {name: getattr(os, name, 1) for name in
                        ('waitid', 'P_PID', 'WEXITED', 'WNOHANG', 'WNOWAIT', 'CLD_EXITED')}
        with patch.object(TOOLS.sys, 'platform', 'linux'), patch.dict(TOOLS.os.__dict__, capabilities), \
             patch.object(TOOLS.signal, 'getsignal', return_value=signal.SIG_IGN), \
             patch.object(TOOLS.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(ValueError, 'sole child reaper'):
                TOOLS.bounded_readonly_command(['unused'])
            launch.assert_not_called()
        with patch.object(TOOLS.sys, 'platform', 'linux'), patch.dict(TOOLS.os.__dict__), \
             patch.object(TOOLS.subprocess, 'Popen') as launch:
            TOOLS.os.__dict__.pop('waitid', None)
            with self.assertRaisesRegex(ValueError, 'Linux waitid'):
                TOOLS.bounded_readonly_command(['unused'])
            launch.assert_not_called()

    def test_diagnostic_signals_only_while_leader_is_waitable(self):
        for ownership_lost in (False, True):
            read_fd, write_fd = os.pipe()
            os.close(write_fd)
            stream = os.fdopen(read_fd, 'rb')
            process = SimpleNamespace(pid=12345, returncode=None, stdout=stream, wait=Mock(return_value=0))
            status = SimpleNamespace(si_pid=12345, si_code=1, si_status=0)
            waitid = Mock(return_value=status, side_effect=ChildProcessError if ownership_lost else None)
            capabilities = {name: getattr(os, name, 1) for name in
                            ('P_PID', 'WEXITED', 'WNOHANG', 'WNOWAIT')}
            capabilities.update(waitid=waitid, CLD_EXITED=1)
            def signal_owned(pid, sig):
                process.wait.assert_not_called()
                self.assertEqual((pid, sig), (12345, signal.SIGKILL))
            with patch.object(TOOLS.sys, 'platform', 'linux'), patch.dict(TOOLS.os.__dict__, capabilities), \
                 patch.object(TOOLS.signal, 'getsignal', return_value=signal.SIG_DFL), \
                 patch.object(TOOLS.subprocess, 'Popen', return_value=process), \
                 patch.object(TOOLS.os, 'killpg', side_effect=signal_owned) as kill:
                if ownership_lost:
                    with self.assertRaisesRegex(ValueError, 'ownership lost'):
                        TOOLS.bounded_readonly_command(['unused'])
                    kill.assert_not_called()
                    process.wait.assert_not_called()
                else:
                    self.assertEqual(TOOLS.bounded_readonly_command(['unused']), b'')
                    kill.assert_called_once()
                    process.wait.assert_called_once_with(timeout=5)
            self.assertTrue(stream.closed)

    def test_storage_observations_accept_only_fixed_numeric_paths(self):
        paths = [str(TOOLS.BUILD_ROOT), str(TOOLS.OUTPUT), '/home/go-builder/go/pkg/mod',
                 '/home/go-builder/.cache/go-build']
        raw = ''.join(f'{index}\t{path}\n' for index, path in enumerate(paths)).encode()
        for value, status in ((raw, 'observed'), (b'1\t/arbitrary\n', 'unavailable')):
            stream = io.StringIO()
            with patch.object(TOOLS, 'bounded_readonly_command', return_value=value) as run, \
                 patch('sys.stdout', stream):
                TOOLS.observe_image_storage('containerd', 'before')
            run.assert_called_once_with(['du', '-sk', '--', *paths], maximum=4096)
            record = json.loads(stream.getvalue())
            self.assertEqual(record['status'], status)
            if status == 'observed':
                self.assertEqual(record['kib'], dict(zip(paths, range(4))))
            else:
                self.assertNotIn('kib', record)

    def test_image_build_failure_stays_primary_after_diagnostic_failure(self):
        stream = io.StringIO()
        with patch.object(sys, 'argv', ['build_runner_go_tools.py', 'containerd']), \
             patch.object(TOOLS, 'build', side_effect=RuntimeError('original build failure')), \
             patch.object(TOOLS, 'bounded_readonly_command', side_effect=TimeoutError), \
             patch('sys.stdout', stream):
            with self.assertRaisesRegex(RuntimeError, 'original build failure'):
                TOOLS.main()
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([r['phase'] for r in records], ['before', 'failed'])
        self.assertTrue(all(r['status'] == 'unavailable' for r in records))

    def test_production_openpgp_guard_rejects_legacy_root_and_subpackages(self):
        supported = {'github.com/ProtonMail/go-crypto/openpgp', 'crypto', 'example/main'}
        TOOLS.verify_openpgp_absent(supported)
        for blocked in ('golang.org/x/crypto/openpgp', 'golang.org/x/crypto/openpgp/packet',
                        'golang.org/x/crypto/openpgp/armor'):
            with self.subTest(package=blocked), self.assertRaisesRegex(ValueError, 'GO-2026-5932'):
                TOOLS.verify_openpgp_absent(supported | {blocked})
        with self.assertRaisesRegex(ValueError, 'empty production'):
            TOOLS.verify_openpgp_absent(set())

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
            net_version = 'v0.62.0' if name == 'buildx' else 'v0.60.0'
            self.assertIn('golang.org/x/net ' + net_version, (folder / 'go.mod').read_text())
        self.assertIn('github.com/moby/go-archive v0.3.0',
                      (TOOLS.LOCKS / 'buildx/go.mod').read_text())
        self.assertNotIn('\tgithub.com/docker/docker ', (TOOLS.LOCKS / 'buildx/go.mod').read_text())
        self.assertIn('github.com/cilium/ebpf v0.22.0',
                      (TOOLS.LOCKS / 'containerd/go.mod').read_text())

    def test_downstream_security_revision_matches_embedded_build_identity(self):
        spec = copy.deepcopy(self.manifest['components']['containerd'])
        for number in (1, 2, 10):
            with self.subTest(number=number):
                spec['downstream_version'] = spec['release_tag'] + f'+victron.security.{number}'
                TOOLS.validate_spec('containerd', spec)
                for command in TOOLS.build_commands('containerd', spec, Path('/out')):
                    flags = command[command.index('-ldflags') + 1]
                    self.assertIn(spec['downstream_version'], flags)
                    self.assertIn(spec['commit'] + f'+victron.security.{number}', flags)
        for suffix in ('0', '01', '-1', '1-extra', '1\n'):
            with self.subTest(suffix=suffix):
                spec['downstream_version'] = spec['release_tag'] + '+victron.security.' + suffix
                with self.assertRaisesRegex(ValueError, 'Downstream builds'):
                    TOOLS.validate_spec('containerd', spec)

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

    def test_manifest_client_guard_is_specific_and_requires_evidence(self):
        safe = {'github.com/docker/distribution',
                'github.com/docker/distribution/registry/client/auth',
                'github.com/docker/distribution/registry/client/transport',
                'github.com/distribution/distribution/v3/registry/client'}
        TOOLS.verify_manifest_client_absent(safe)
        for package in (TOOLS.LEGACY_MANIFEST_CLIENT,
                        TOOLS.LEGACY_MANIFEST_CLIENT + ' [example/main.test]'):
            with self.subTest(package=package), \
                    self.assertRaisesRegex(ValueError, 'GHSA-685w-q87j-wqw3'):
                TOOLS.verify_manifest_client_absent(safe | {package})
        with self.assertRaisesRegex(ValueError, 'empty dependency graph'):
            TOOLS.verify_manifest_client_absent(set())

    def test_vulnerable_production_or_unit_graph_cannot_publish_provenance(self):
        safe = {'example/main'}
        unsafe = safe | {TOOLS.LEGACY_MANIFEST_CLIENT}
        for name in ('buildx', 'compose'):
            for production, units in ((unsafe, safe), (safe, unsafe), (safe, set())):
                with self.subTest(name=name, production=production, units=units):
                    destination = self.root / (name + str(len(list(self.root.iterdir()))))
                    result = SimpleNamespace(stdout='\n'.join(
                        json.dumps({'ImportPath': package}) for package in production))
                    with patch.object(TOOLS, 'run', return_value=result), \
                            self.assertRaises(ValueError):
                        TOOLS.preserve_notices(self.root, destination, name, {},
                                               [['go', 'build', './cmd']], 'amd64',
                                               ['go', 'test', './...'], self.root, units)
                    self.assertFalse((destination / 'provenance.json').exists())

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

    def test_empty_unit_selection_cannot_fall_back_to_implicit_package(self):
        cases = [('buildx', []), ('compose', []), ('containerd', []),
                 ('compose', ['example/e2e']),
                 ('containerd', ['example/integration/client'])]
        for name, packages in cases:
            with self.subTest(name=name, packages=packages), \
                    self.assertRaisesRegex(ValueError, 'No upstream unit packages selected'):
                TOOLS.test_packages(name, packages)

    def test_nonroot_build_keeps_packages_with_privileged_test_cases(self):
        module = 'github.com/containerd/containerd/v2'
        retained = [module + '/' + package for package in (
            'internal/cri/server/podsandbox', 'pkg/sys', 'plugins/content/local', 'pkg/shim')]
        packages = [*retained, module + '/integration/client']
        self.assertEqual(TOOLS.test_packages('containerd', packages), retained)

    def test_explicit_builder_paths_survive_environment_sanitizing(self):
        paths = {'HOME': '/home/go-builder', 'GOPATH': '/home/go-builder/go',
                 'GOMODCACHE': '/home/go-builder/go/pkg/mod',
                 'GOCACHE': '/home/go-builder/.cache/go-build'}
        with patch.dict(os.environ, paths):
            for architecture in (None, 'amd64', 'arm64'):
                with self.subTest(architecture=architecture):
                    environment = TOOLS.go_environment(architecture)
                    self.assertEqual({name: environment[name] for name in paths}, paths)
                    self.assertEqual(environment['GOFLAGS'], '-mod=readonly')
                    self.assertEqual(environment['GOTOOLCHAIN'], 'local')

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
