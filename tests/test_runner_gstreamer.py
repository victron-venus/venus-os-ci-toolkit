"""Ordinary source, package and ABI checks; no malformed media is executed."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.util.spec_from_file_location('runner_gstreamer', ROOT / 'scripts/build_runner_gstreamer.py')
m = importlib.util.module_from_spec(loader)
loader.loader.exec_module(m)


def archive(path, entries):
    with tarfile.open(path, 'w') as out:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.mode = 0o644
            if kind == tarfile.REGTYPE:
                info.size = len(data)
                out.addfile(info, io.BytesIO(data))
            else:
                info.linkname = data.decode()
                out.addfile(info)


def control(package, arch='amd64'):
    return (f'Package: {package}\nSource: gst-plugins-bad1.0\nVersion: {m.VERSION}\n'
            f'Architecture: {arch}\nMaintainer: Original <original@example.invalid>\n'
            'Installed-Size: 10\nDepends: libc6 (>= 2.38)'
            + (f', {m.LIB_PACKAGE} (= {m.VERSION})' if package == m.PACKAGES[1] else '')
            + '\nMulti-Arch: same\nDescription: Synthetic ordinary fixture\n continued line\n')


def gstreamer_dpkg_commands():
    text = (ROOT / 'deploy/runner-reserve/Dockerfile').read_text().replace('\\\n', ' ')
    return [shlex.split(line.removeprefix('RUN ').split('&&', 1)[0])
            for line in text.splitlines()
            if line.startswith('RUN dpkg ') and 'victron-gstreamer/' in line]


class GstreamerTests(unittest.TestCase):
    def setUp(self):
        self.spec = json.loads(m.MANIFEST.read_text())

    def test_manifest_and_only_parser_patch(self):
        m.validate_spec(self.spec)
        path = ROOT / self.spec['patch']['path']
        self.assertEqual(m.digest(path), self.spec['patch']['sha256'])
        self.assertEqual([x for x in path.read_text().splitlines() if x.startswith('+++ ')],
                         ['+++ b/' + m.PATCH_TARGET])
        self.assertEqual(len(self.spec['patch']['upstream_commits']), 2)

    def test_reject_unreviewed_version_arch_origin_and_partial_pair(self):
        for field, value in [('version', '1.26.1'), ('architectures', ['amd64']), ('soname', 'other')]:
            spec = {**self.spec, field: value}
            with self.assertRaises(ValueError):
                m.validate_spec(spec)
        spec = copy.deepcopy(self.spec)
        spec['sources'][m.ORIG]['url'] = 'https://example.invalid/source'
        with self.assertRaises(ValueError):
            m.validate_spec(spec)
        spec = copy.deepcopy(self.spec)
        del spec['binaries'][next(iter(spec['binaries']))]
        with self.assertRaises(ValueError):
            m.validate_spec(spec)

    def test_root_rejected_before_any_command(self):
        with patch.object(m.os, 'getuid', return_value=0), patch.object(m, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'unprivileged'):
                m.build()
            run.assert_not_called()

    def test_extract_normal_file_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive(root / 'source.tar', [('src/a', b'ordinary source', tarfile.REGTYPE)])
            m.extract_source(root / 'source.tar', root / 'out', 'src')
            self.assertEqual((root / 'out/a').read_bytes(), b'ordinary source')
            with self.assertRaises(FileExistsError):
                m.extract_source(root / 'source.tar', root / 'out', 'src')

    def test_extract_rejects_links_traversal_duplicates_and_file_ancestors(self):
        cases = [[('src/a', b'../x', tarfile.SYMTYPE)],
                 [('src/a', b'src/b', tarfile.LNKTYPE)],
                 [('src/../a', b'x', tarfile.REGTYPE)],
                 [('/src/a', b'x', tarfile.REGTYPE)],
                 [('src/a', b'x', tarfile.REGTYPE)] * 2,
                 [('src/a', b'x', tarfile.REGTYPE), ('src/a/b', b'y', tarfile.REGTYPE)]]
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive(root / 'source.tar', entries)
                with self.assertRaises(ValueError):
                    m.extract_source(root / 'source.tar', root / 'out', 'src')

    def test_input_hash_and_size_both_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'archive'
            path.write_bytes(b'known')
            pin = {'bytes': 5, 'sha256': hashlib.sha256(b'known').hexdigest()}
            m.verify_file(path, pin)
            path.write_bytes(b'other')
            with self.assertRaises(ValueError):
                m.verify_file(path, pin)

    def test_control_preserves_other_dependencies_and_description(self):
        for package in m.PACKAGES:
            old = m.fields(control(package))
            new = m.fields(m.update_control(control(package), package, 'amd64', 11))
            self.assertEqual(new['Version'], m.NEW_VERSION)
            self.assertEqual(new['Source'], 'gst-plugins-bad1.0 (' + m.NEW_VERSION + ')')
            for key in old.keys() - {'Version', 'Source', 'Maintainer', 'Installed-Size', 'Depends'}:
                self.assertEqual(old[key], new[key])
            self.assertEqual(new['Depends'], old['Depends'].replace(m.VERSION, m.NEW_VERSION))

    def test_control_rejects_wrong_arch_version_and_unpaired_dependency(self):
        for text in [control(m.PACKAGES[1], 'arm64'),
                     control(m.PACKAGES[1]).replace(m.VERSION, 'other'),
                     control(m.PACKAGES[1]).replace(' (= ', ' (>= ')]:
            with self.assertRaises(ValueError):
                m.update_control(text, m.PACKAGES[1], 'amd64', 10)
        with self.assertRaises(ValueError):
            m.fields(control(m.PACKAGES[0]) + 'Version: duplicate\n')

    def test_payload_only_expected_library_change_and_modes(self):
        old = {'library': {'type': 'file', 'mode': 0o644, 'sha256': 'old'},
               'plugin': {'type': 'file', 'mode': 0o644, 'sha256': 'unchanged'}}
        new = copy.deepcopy(old)
        new['library']['sha256'] = 'fixed'
        m.verify_payload(old, new, ['library'])
        for key, value in [('mode', 0o755), ('type', 'symlink')]:
            bad = copy.deepcopy(new)
            bad['library'][key] = value
            with self.assertRaises(ValueError):
                m.verify_payload(old, bad, ['library'])
        new['plugin']['sha256'] = 'changed'
        with self.assertRaises(ValueError):
            m.verify_payload(old, new, ['library'])

    def test_inventory_does_not_follow_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'real').write_bytes(b'original')
            (root / 'link').symlink_to('real')
            result = m.inventory(root)
            self.assertEqual(result['link'], {'type': 'symlink',
                                             'mode': (root / 'link').lstat().st_mode & 0o777,
                                             'target': 'real'})
            self.assertEqual(result['real']['sha256'], hashlib.sha256(b'original').hexdigest())

    def test_abi_ignores_addresses_but_not_missing_symbols_types_or_dependencies(self):
        old = 'api T 0100 20\nother D 1000 8\n'
        new = 'other D 2000 8\napi T 0300 30\n'
        dynamic = f' 0x0 (SONAME) Library soname: [{m.SONAME}]\n 0x0 (NEEDED) Shared library: [libc.so.6]\n'
        self.assertEqual(m.check_abi(old, new, dynamic, dynamic), 2)
        for bad in ['api T 0300 30\n', new.replace('api T', 'api D'), new + 'extra T 0040 1\n']:
            with self.assertRaises(ValueError):
                m.check_abi(old, bad, dynamic, dynamic)
        for bad in [dynamic.replace(m.SONAME, 'changed'), dynamic.replace('libc.so.6', 'libnew.so'), '']:
            with self.assertRaises(ValueError):
                m.check_abi(old, new, dynamic, bad)

    def test_preflight_and_runtime_keep_the_same_exact_locale_payload(self):
        commands = gstreamer_dpkg_commands()
        option = '--path-include=/usr/share/locale/*/LC_MESSAGES/gst-plugins-bad-1.0.mo'
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][:3], ['dpkg', option, '--unpack'])
        self.assertEqual(commands[1][:3], ['dpkg', option, '-i'])
        self.assertEqual([Path(p).name for p in commands[0][3:]],
                         [package + '.deb' for package in m.PACKAGES])
        self.assertEqual([Path(p).name for p in commands[1][3:]],
                         [package + '.deb' for package in m.PACKAGES])
        text = (ROOT / 'deploy/runner-reserve/Dockerfile').read_text()
        stage = text.split(' AS patched-gstreamer\n', 1)[1].split('\nFROM ', 1)[0]
        self.assertIn('USER 10001:10001\nRUN umask 022', stage)
        self.assertLess(stage.index('python3 scripts/build_runner_gstreamer.py'), stage.index('USER 0:0'))
        self.assertIn('sha256sum --check /opt/victron-gstreamer/installed.sha256', stage)
        self.assertIn('sha256sum --check /tmp/victron-gstreamer/installed.sha256', text)
        self.assertNotIn('--ignore-missing', text)

    @unittest.skipUnless(shutil.which('dpkg') and shutil.which('dpkg-deb') and os.geteuid() != 0,
                         'Requires unprivileged dpkg and dpkg-deb for isolated unpack test')
    def test_real_dpkg_locale_filter_preserves_only_gstreamer_translations(self):
        # The 44 names omitted by the Ubuntu image's locale filter in the failed
        # build. Contents are harmless synthetic bytes, with no scripts/deps.
        languages = ('af ast az bg ca cs da de el en_GB eo es eu fi fr fur gl hr hu id it ja ka ky lt lv '
                     'mt nb nl or pl pt_BR ro ru sk sl sq sr sv tr uk vi zh_CN zh_TW').split()
        self.assertEqual(len(languages), 44)
        option = gstreamer_dpkg_commands()[0][1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            package = root / 'package'
            (package / 'DEBIAN').mkdir(parents=True)
            (package / 'DEBIAN').chmod(0o755)
            (package / 'DEBIAN/control').write_text(
                'Package: victron-locale-fixture\nVersion: 1\nArchitecture: all\n'
                'Maintainer: Fixture <fixture@example.invalid>\nDescription: Inert locale fixture\n')
            expected = {f'usr/share/locale/{lang}/LC_MESSAGES/gst-plugins-bad-1.0.mo':
                        ('synthetic locale ' + lang).encode() for lang in languages}
            sibling = 'usr/share/locale/fr/LC_MESSAGES/unrelated.mo'
            ordinary = 'usr/share/victron-locale-fixture/ordinary.txt'
            for name, data in {**expected, sibling: b'unrelated locale', ordinary: b'ordinary data'}.items():
                path = package / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            self.assertEqual(sorted(p.name for p in (package / 'DEBIAN').iterdir()), ['control'])
            deb = root / 'fixture.deb'
            env = {'PATH': os.environ['PATH'], 'LC_ALL': 'C', 'HOME': str(root)}
            def run(command):
                result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run(['dpkg-deb', '--build', '--root-owner-group', str(package), str(deb)])
            for include in (False, True):
                target = root / ('included' if include else 'baseline')
                target.mkdir()
                command = ['dpkg', '--root=' + str(target), '--admindir=' + str(target / 'var/lib/dpkg'),
                           '--log=' + str(target / 'dpkg.log'), '--force-not-root', '--no-triggers',
                           '--path-exclude=/usr/share/locale/*']
                if include:
                    command.append(option)
                run([*command, '--unpack', str(deb)])
                self.assertEqual((target / ordinary).read_bytes(), b'ordinary data')
                self.assertFalse((target / sibling).exists())
                for name, data in expected.items():
                    if include:
                        self.assertEqual((target / name).read_bytes(), data)
                    else:
                        self.assertFalse((target / name).exists())

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'Requires dpkg-deb for ordinary package roundtrip')
    def test_real_synthetic_deb_roundtrip_preserves_plugins_and_scripts(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            out = root / 'out'
            out.mkdir()
            built = root / 'fixed-library'
            built.write_bytes(b'fixed synthetic library')
            env = {'PATH': os.environ['PATH'], 'LC_ALL': 'C', 'SOURCE_DATE_EPOCH': '1792886400'}
            relative = 'usr/lib/test/' + m.LIBRARY
            for package in m.PACKAGES:
                original = root / ('original-' + package)
                (original / 'DEBIAN').mkdir(parents=True)
                (original / 'DEBIAN').chmod(0o755)
                (original / 'DEBIAN/control').write_text(control(package))
                script = original / 'DEBIAN/postinst'
                script.write_text('#!/bin/sh\nexit 99\n')
                script.chmod(0o755)
                payload_path = relative if package == m.LIB_PACKAGE else 'usr/lib/test/plugin.so'
                (original / payload_path).parent.mkdir(parents=True)
                (original / payload_path).write_bytes(b'original synthetic payload')
                (original / 'DEBIAN/md5sums').write_text(m.md5sums(original, m.payload(m.inventory(original))))
                deb = root / (package + '-original.deb')
                subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(original), str(deb)],
                               check=True, stdout=subprocess.DEVNULL, env=env)
                with patch.object(m, 'WORK', root), patch.object(m, 'OUTPUT', out):
                    result = m.repack(package, 'amd64', deb, built, relative, env)
                self.assertEqual(result['control']['Version'], m.NEW_VERSION)
                self.assertEqual(result['changed_payload'], [relative] if package == m.LIB_PACKAGE else [])
                self.assertEqual((root / package / 'DEBIAN/postinst').read_bytes(), script.read_bytes())


if __name__ == '__main__':
    unittest.main()
