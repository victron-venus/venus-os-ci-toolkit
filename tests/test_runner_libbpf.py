"""Source-custody/package contracts; no kernel or malformed BPF execution."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runner_libbpf', ROOT / 'scripts/build_runner_libbpf.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def archive(path, entries):
    with tarfile.open(path, 'w:gz') as out:
        for name, payload, kind in entries:
            member = tarfile.TarInfo(name)
            member.mode = 0o644
            member.type = kind
            if kind == tarfile.REGTYPE:
                member.size = len(payload)
                out.addfile(member, io.BytesIO(payload))
            else:
                member.linkname = payload.decode()
                out.addfile(member)


class RunnerLibbpfTests(unittest.TestCase):
    def setUp(self):
        self.spec = json.loads(m.MANIFEST.read_text())

    def test_manifest_and_patch_are_bound(self):
        m.validate_spec(self.spec)
        patch_file = ROOT / self.spec['patch']['path']
        self.assertEqual(m.digest(patch_file), self.spec['patch']['sha256'])
        lines = patch_file.read_text().splitlines()
        changed = '\n'.join(line for line in lines if line[:1] in '+-'
                            and not line.startswith(('---', '+++'))) + '\n'
        self.assertEqual(sha(changed.encode()),
                         '2b0c433143ef9aff9eb01464bded66d2e2adda125a2666c46b58359b5d519219')
        self.assertEqual([line for line in lines if line.startswith('+++')],
                         ['+++ b/src/relo_core.c'])

    def test_versions_and_abi_cannot_silently_change(self):
        for key, value in [('downstream_binary_version', '1:1.8.0'),
                           ('ubuntu_source_version', '1.8.0'), ('soname', 'libbpf.so.2'),
                           ('architectures', ['amd64'])]:
            with self.subTest(key=key):
                changed = {**self.spec, key: value}
                with self.assertRaises(ValueError):
                    m.validate_spec(changed)

    def test_clean_build_environment_preserves_security_build_checks(self):
        with patch.dict(os.environ, {'CFLAGS': '-O0', 'LD_PRELOAD': 'evil',
                                     'DEB_BUILD_OPTIONS': 'nocheck', 'HTTPS_PROXY': 'invalid'}):
            env = m.environment(self.spec)
        self.assertEqual(env['DEB_BUILD_OPTIONS'], 'parallel=2')
        self.assertEqual(env['DPKG_GENSYMBOLS_CHECK_LEVEL'], '4')
        self.assertFalse({'CFLAGS', 'LD_PRELOAD', 'HTTPS_PROXY'} & env.keys())

    def test_package_hooks_keep_the_exact_patched_tree_for_postbuild_proof(self):
        self.assertEqual(m.SOURCE_OPTIONS, ['--no-unapply-patches'])
        self.assertEqual(m.BUILD_COMMAND, [
            'dpkg-buildpackage', '-us', '-uc', '-b', '-rfakeroot', '-j2',
            '--source-option=--no-unapply-patches'])

    def test_root_build_is_rejected_before_commands(self):
        with patch.object(m.os, 'getuid', return_value=0), patch.object(m, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'unprivileged'):
                m.build()
            run.assert_not_called()

    def test_regular_archive_and_executable_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            path = base / 'a.tar.gz'
            archive(path, [('libbpf-1.3.0/src/a.c', b'ok', tarfile.REGTYPE)])
            m.extract(path, base / 'out', 'libbpf-1.3.0')
            self.assertEqual((base / 'out/src/a.c').read_bytes(), b'ok')
            self.assertEqual((base / 'out/src/a.c').stat().st_mode & 0o777, 0o644)

    def test_archive_rejects_links_traversal_duplicates_and_file_ancestors(self):
        cases = [
            [('libbpf-1.3.0/a', b'../escape', tarfile.SYMTYPE)],
            [('libbpf-1.3.0/a', b'libbpf-1.3.0/b', tarfile.LNKTYPE)],
            [('libbpf-1.3.0/../escape', b'x', tarfile.REGTYPE)],
            [('/libbpf-1.3.0/a', b'x', tarfile.REGTYPE)],
            [('other/a', b'x', tarfile.REGTYPE)],
            [('libbpf-1.3.0/a', b'x', tarfile.REGTYPE)] * 2,
            [('libbpf-1.3.0/a', b'x', tarfile.REGTYPE),
             ('libbpf-1.3.0/a/b', b'x', tarfile.REGTYPE)],
        ]
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                path = base / 'bad.tar.gz'
                archive(path, entries)
                with self.assertRaises(ValueError):
                    m.extract(path, base / 'out', 'libbpf-1.3.0')
                self.assertFalse((base / 'out').exists())

    def test_archive_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            archive(base / 'a.tar.gz', [('libbpf-1.3.0/a', b'new', tarfile.REGTYPE)])
            (base / 'out').mkdir()
            (base / 'out/a').write_bytes(b'old')
            with self.assertRaises(FileExistsError):
                m.extract(base / 'a.tar.gz', base / 'out', 'libbpf-1.3.0')
            self.assertEqual((base / 'out/a').read_bytes(), b'old')

    def test_versioned_export_set_accepts_exact_and_rejects_abi_change(self):
        symbols = 'libbpf.so.1 libbpf1 #MINVER#\n LIBBPF_1.3.0@LIBBPF_1.3.0 1:1.3.0\n api@LIBBPF_1.3.0 1:1.3.0\n#MISSING: old@LIBBPF_0.0.1 0.1\n'
        nm = '0000 A LIBBPF_1.3.0\n0001 T api@@LIBBPF_1.3.0\n'
        self.assertEqual(m.check_exports(symbols, nm), 2)
        for changed in [nm.replace('api', 'other'), nm.replace('0001 T api@@LIBBPF_1.3.0\n', ''),
                        nm + '0002 T extra@@LIBBPF_1.3.0\n']:
            with self.assertRaises(ValueError):
                m.check_exports(symbols, changed)

    def test_elf_requires_native_architecture_and_shared_library(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'library'
            for arch, number in [('amd64', 62), ('arm64', 183)]:
                header = b'\x7fELF\x02\x01\x01' + b'\0' * 9 + (3).to_bytes(2, 'little') + number.to_bytes(2, 'little')
                path.write_bytes(header)
                m.check_elf(path, arch)
                with self.assertRaises(ValueError):
                    m.check_elf(path, 'arm64' if arch == 'amd64' else 'amd64')
                path.write_bytes(header[:16] + (2).to_bytes(2, 'little') + header[18:])
                with self.assertRaises(ValueError):
                    m.check_elf(path, arch)

    def test_soname_requires_one_complete_exact_field(self):
        line = ' 0x000000000000000e (SONAME) Library soname: [libbpf.so.1]'
        for text in (line, 'Dynamic section:\n' + line + '\n 0x0 (NULL) 0x0\n', line + '  \t'):
            with self.subTest(valid=text):
                m.check_soname(text, 'libbpf.so.1')
        for text in ('', line.replace('(SONAME)', '(NEEDED)'), line.replace('libbpf.so.1', 'libbpf.so.2'),
                     line + '\n' + line, line.replace('(SONAME)', '(SONAME) (SONAME)'),
                     line.replace('[', ''), line.replace(']', ''), line.replace('[', '[['),
                     line.replace(']', ']]'), line + ' trailing', line.replace('libbpf.so.1', '')):
            with self.subTest(invalid=text), self.assertRaises(ValueError):
                m.check_soname(text, 'libbpf.so.1')

    def test_digest_streams_instead_of_loading_entire_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'data'
            path.write_bytes(b'x' * (2 * 1024 * 1024 + 7))
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('not streaming')):
                self.assertEqual(m.digest(path), sha(b'x' * (2 * 1024 * 1024 + 7)))

    def test_real_quilt_prepare_requires_expected_changed_file_only(self):
        # Simulate only dpkg-source applying a tiny ordinary text patch; the real
        # pinned Ubuntu source/patch is also checked separately before release.
        for unexpected in (False, True):
            with self.subTest(unexpected=unexpected), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                inputs = base / 'inputs'
                inputs.mkdir()
                archive(inputs / m.ORIG, [(f'libbpf-1.3.0/{name}', payload, tarfile.REGTYPE)
                                         for name, payload in [('src/relo_core.c', b'before'),
                                                               ('src/libbpf.map', b'map')]])
                archive(inputs / m.DEBIAN, [('debian/libbpf1.symbols', b'symbols', tarfile.REGTYPE),
                                           ('debian/changelog', b'old changelog', tarfile.REGTYPE)])
                patch_file = base / 'patch'
                patch_file.write_bytes(b'patch')
                spec = json.loads(json.dumps(self.spec))
                spec['patch'].update(sha256=sha(b'patch'), before_sha256=sha(b'before'), after_sha256=sha(b'after'))
                spec['abi'] = {'map_sha256': sha(b'map'), 'debian_symbols_sha256': sha(b'symbols')}
                def apply(args, cwd, env):
                    self.assertEqual(args, ['dpkg-source', '--no-unapply-patches', '--before-build', '.'])
                    (cwd / 'src/relo_core.c').write_bytes(b'after')
                    if unexpected:
                        (cwd / 'src/libbpf.map').write_bytes(b'changed')
                    return ''
                with patch.object(m, 'verify_inputs'), patch.object(m, 'run', side_effect=apply):
                    if unexpected:
                        with self.assertRaisesRegex(ValueError, 'unexpected source'):
                            m.prepare(inputs, base / 'source', spec, patch_file, {})
                    else:
                        m.prepare(inputs, base / 'source', spec, patch_file, {})
                        self.assertTrue((base / 'source/debian/changelog').read_text().endswith('old changelog'))
                        self.assertEqual((base / 'source/debian/patches/series').read_text(), m.PATCH + '\n')

    def test_bad_patch_digest_fails_before_source_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            path = base / 'patch'
            path.write_bytes(b'wrong')
            with patch.object(m, 'verify_inputs'), patch.object(m, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'Backport identity'):
                    m.prepare(base, base / 'source', self.spec, path, {})
                self.assertFalse((base / 'source').exists())
                run.assert_not_called()

    def test_source_size_and_digest_are_both_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            for name in self.spec['sources']:
                (base / name).write_bytes(b'wrong')
            with self.assertRaisesRegex(ValueError, 'Source archive identity'):
                m.verify_inputs(base, self.spec)

    def test_docker_build_and_final_install_contract(self):
        lines = (ROOT / 'deploy/runner-reserve/Dockerfile').read_text().replace('\\\n', ' ').splitlines()
        stage = identity = None
        builds = []
        for line in lines:
            words = shlex.split(line, comments=True)
            if not words:
                continue
            if words[0] == 'FROM':
                stage = words[-1] if len(words) > 3 and words[-2] == 'AS' else None
                identity = None
            elif words[0] == 'USER':
                identity = words[1]
            elif words[0] == 'RUN' and 'scripts/build_runner_libbpf.py' in words:
                builds.append((stage, identity, words))
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0][:2], ('patched-libbpf', '10001:10001'))
        self.assertEqual(identity, '1001:1001')
        text = '\n'.join(lines)
        self.assertIn('dpkg -i /tmp/victron-libbpf/libbpf1.deb', text)
        self.assertIn(self.spec['downstream_binary_version'], text)
        self.assertIn('sha256sum --check /tmp/victron-libbpf/installed.sha256', text)
        self.assertNotIn('apt-mark hold', text)


if __name__ == '__main__':
    unittest.main()
