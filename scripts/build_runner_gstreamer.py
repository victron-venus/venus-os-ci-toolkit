#!/usr/bin/env python3
"""Backport the H.265 fix; preserve Noble's other browser plugin payloads."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / '.github/containers/runner-gstreamer.json'
WORK = Path('/opt/victron-gstreamer-build')
OUTPUT = Path('/opt/victron-gstreamer')
VERSION = '1.24.2-1ubuntu4'
NEW_VERSION = VERSION + '+victron1'
LIB_PACKAGE = 'libgstreamer-plugins-bad1.0-0'
PACKAGES = (LIB_PACKAGE, 'gstreamer1.0-plugins-bad')
LIBRARY = 'libgstcodecparsers-1.0.so.0.2402.0'
SONAME = 'libgstcodecparsers-1.0.so.0'
TARGET = 'gst-libs/gst/codecparsers/' + LIBRARY
PATCH_TARGET = 'gst-libs/gst/codecparsers/gsth265parser.c'
ORIG = 'gst-plugins-bad1.0_1.24.2.orig.tar.xz'
DEBIAN = 'gst-plugins-bad1.0_' + VERSION + '.debian.tar.xz'
DSC = 'gst-plugins-bad1.0_' + VERSION + '.dsc'
TRIPLETS = {'amd64': 'x86_64-linux-gnu', 'arm64': 'aarch64-linux-gnu'}
MAINTAINER = 'alvit <272257197+4alvit@users.noreply.github.com>'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path, algorithm='sha256'):
    value = (hashlib.md5(usedforsecurity=False) if algorithm == 'md5'
             else hashlib.sha256())
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def run(args, cwd, env, timeout=180):
    # BuildKit owns the stage's outer deadline and resource limits.
    result = subprocess.run(args, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=timeout, check=False)
    print(result.stdout, end='')
    result.check_returncode()
    return result.stdout


def validate_spec(spec):
    require(spec['schema'] == 1 and spec['source_package'] == 'gst-plugins-bad1.0'
            and spec['version'] == VERSION and spec['downstream_version'] == NEW_VERSION,
            'Unreviewed source/package version')
    require(spec['architectures'] == list(TRIPLETS) and spec['library'] == LIBRARY
            and spec['soname'] == SONAME, 'Unreviewed ABI')
    require(set(spec['sources']) == {ORIG, DEBIAN, DSC}, 'Unexpected source inputs')
    require(set(spec['binaries']) == {
        f'{package}_{VERSION}_{arch}.deb' for package in PACKAGES for arch in TRIPLETS},
        'Unexpected binary inputs')
    for name, pin in (spec['sources'] | spec['binaries']).items():
        host = ('https://ports.ubuntu.com/ubuntu-ports' if name.endswith('_arm64.deb')
                else 'https://archive.ubuntu.com/ubuntu')
        require(pin['url'] == host + '/pool/universe/g/gst-plugins-bad1.0/' + name
                and re.fullmatch('[0-9a-f]{64}', pin['sha256'])
                and type(pin['bytes']) is int and 0 < pin['bytes'] < 8 * 1024 * 1024,
                'Unreviewed download')
    require(spec['patch']['path'] == '.github/containers/gstreamer/0001-h265-parser-bounds.patch'
            and spec['patch']['target'] == PATCH_TARGET, 'Unreviewed patch target')


def verify_file(path, pin):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size == pin['bytes']
            and digest(path) == pin['sha256'], 'Pinned input changed: ' + path.name)


def extract_source(archive_path, destination, prefix):
    """The pinned source archives contain regular files and directories only."""
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        require(len(members) < 5000 and sum(m.size for m in members) < 128 * 1024 * 1024,
                'Source expansion exceeds bound')
        seen, files = set(), set()
        for member in members:
            name = member.name.rstrip('/')
            path = PurePosixPath(name)
            require(name and str(path) == name and not path.is_absolute()
                    and '..' not in path.parts and path.parts[0] == prefix,
                    'Unsafe source path')
            require(name not in seen and (member.isdir() or member.isreg())
                    and not member.issparse() and not member.mode & 0o7000,
                    'Unexpected source member')
            seen.add(name)
            if member.isreg():
                files.add(name)
        require(not any(str(parent) in files for name in seen
                        for parent in PurePosixPath(name).parents), 'Source file ancestor')
        for member in members:
            parts = PurePosixPath(member.name.rstrip('/')).parts
            target = destination.joinpath(*(parts if prefix == 'debian' else parts[1:]))
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as src, target.open('xb') as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)


def inventory(root):
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            entry = {'mode': stat.S_IMODE(info.st_mode)}
            if stat.S_ISLNK(info.st_mode):
                entry.update(type='symlink', target=os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                entry.update(type='directory')
            else:
                require(stat.S_ISREG(info.st_mode), 'Unexpected package file type')
                entry.update(type='file', bytes=info.st_size, sha256=digest(path))
            result[relative] = entry
    return dict(sorted(result.items()))


def prepare(inputs, source, spec, env):
    for name, pin in spec['sources'].items():
        verify_file(inputs / name, pin)
    dsc = (inputs / DSC).read_text()
    require('Source: gst-plugins-bad1.0\n' in dsc and 'Version: ' + VERSION + '\n' in dsc,
            'Distro source identity differs')
    for name in (ORIG, DEBIAN):
        pin = spec['sources'][name]
        require(f" {pin['sha256']} {pin['bytes']} {name}\n" in dsc,
                'Distro source checksum differs')
    patch = ROOT / spec['patch']['path']
    require(digest(patch) == spec['patch']['sha256'], 'Backport patch changed')
    source.mkdir()
    extract_source(inputs / ORIG, source, 'gst-plugins-bad-1.24.2')
    extract_source(inputs / DEBIAN, source, 'debian')
    # Preserve the three distro patches too; no dpkg source hook un-applies them later.
    series = (source / 'debian/patches/series').read_text().splitlines()
    require(series == ['02_opencv-data-path.patch', 'pcfile-requires-plugins-good',
                       'Skip-failing-tests.patch'], 'Distro patch series changed')
    for name in series:
        run(['patch', '--batch', '--forward', '--fuzz=0', '-p1', '-i',
             str(source / 'debian/patches' / name)], source, env)
    before = inventory(source)
    require(digest(source / PATCH_TARGET) == spec['patch']['before_sha256'],
            'Original parser changed')
    run(['patch', '--batch', '--forward', '--fuzz=0', '-p1', '-i', str(patch)], source, env)
    after = inventory(source)
    require(set(before) == set(after)
            and {key for key in before if before[key] != after[key]} == {PATCH_TARGET}
            and digest(source / PATCH_TARGET) == spec['patch']['after_sha256'],
            'Backport changed unexpected source')
    return after


def fields(text):
    result = {}
    current = None
    for line in text.splitlines():
        if line.startswith((' ', '\t')):
            require(current is not None, 'Control continuation without field')
            result[current] += '\n' + line
        else:
            key, separator, value = line.partition(': ')
            require(separator and key not in result, 'Invalid/duplicate control field')
            result[key] = value
            current = key
    return result


def update_control(text, package, arch, installed_kib):
    value = fields(text)
    require(value['Package'] == package and value['Version'] == VERSION
            and value['Architecture'] == arch and value['Source'] == 'gst-plugins-bad1.0',
            'Original binary package identity differs')
    value.update(Version=NEW_VERSION, Source='gst-plugins-bad1.0 (' + NEW_VERSION + ')',
                 Maintainer=MAINTAINER)
    value['Installed-Size'] = str(installed_kib)
    if package == PACKAGES[1]:
        old = LIB_PACKAGE + ' (= ' + VERSION + ')'
        require(value['Depends'].count(old) == 1, 'Expected exact paired dependency missing')
        value['Depends'] = value['Depends'].replace(old, LIB_PACKAGE + ' (= ' + NEW_VERSION + ')')
    return ''.join(key + ': ' + data + '\n' for key, data in value.items())


def payload(tree):
    return {key: value for key, value in tree.items()
            if key != 'DEBIAN' and not key.startswith('DEBIAN/')}


def verify_payload(before, after, changed):
    require(set(before) == set(after), 'Package payload paths changed')
    require({name for name in before if before[name] != after[name]} == set(changed),
            'Unrelated plugin payload changed')
    for name in changed:
        require(before[name]['type'] == after[name]['type'] == 'file'
                and before[name]['mode'] == after[name]['mode'], 'Library type/mode changed')


def md5sums(root, tree):
    return ''.join(digest(root / name, 'md5') + '  ' + name + '\n'
                   for name, entry in tree.items() if entry['type'] == 'file')


def check_abi(old_symbols, new_symbols, old_dynamic, new_dynamic):
    def exports(text):
        result = set()
        for line in text.splitlines():
            parts = line.split()
            require(len(parts) >= 2, 'Invalid exported symbol record')
            result.add((parts[0], parts[1]))
        require(result, 'Empty exported ABI')
        return result

    def dynamic(text):
        entries = []
        for line in text.splitlines():
            if '(SONAME)' in line or '(NEEDED)' in line:
                match = re.search(r'\((SONAME|NEEDED)\).*\[([^\]]+)\]\s*$', line)
                require(match is not None, 'Invalid dynamic dependency record')
                entries.append(match.groups())
        require(entries.count(('SONAME', SONAME)) == 1, 'Wrong SONAME')
        return sorted(entries)

    require(exports(old_symbols) == exports(new_symbols), 'Exported ABI changed')
    require(dynamic(old_dynamic) == dynamic(new_dynamic), 'Dynamic ABI/dependencies changed')
    return len(exports(new_symbols))


SMOKE = '''#define GST_USE_UNSTABLE_API
#include <gst/codecparsers/gsth265parser.h>
int main(void) {
  GstH265Parser *parser = gst_h265_parser_new();
  if (!parser) return 1;
  gst_h265_parser_free(parser);
  return 0;
}
'''


def repack(package, arch, original, built, relative, env):
    tree = WORK / package
    run(['dpkg-deb', '--raw-extract', str(original), str(tree)], WORK, env)
    before = inventory(tree)
    before_payload = payload(before)
    old_md5 = (tree / 'DEBIAN/md5sums').read_text().splitlines()
    require(sorted(old_md5) == sorted(md5sums(tree, before_payload).splitlines()),
            'Original package payload checksum differs')
    changed = []
    if package == LIB_PACKAGE:
        require(before_payload[relative]['type'] == 'file', 'Library missing')
        shutil.copyfile(built, tree / relative)
        changed = [relative]
    after_payload = payload(inventory(tree))
    verify_payload(before_payload, after_payload, changed)
    (tree / 'DEBIAN/md5sums').write_text(md5sums(tree, after_payload))
    control = tree / 'DEBIAN/control'
    installed_kib = int(fields(control.read_text())['Installed-Size'])
    for name in changed:
        installed_kib += ((after_payload[name]['bytes'] + 1023) // 1024
                          - (before_payload[name]['bytes'] + 1023) // 1024)
    control.write_text(update_control(control.read_text(), package, arch,
                                      installed_kib))
    after = inventory(tree)
    control_changes = {key for key in before if before[key] != after[key] and key.startswith('DEBIAN/')}
    require(set(before) == set(after) and control_changes <= {'DEBIAN/control', 'DEBIAN/md5sums'},
            'Maintainer scripts/control payload changed')
    deb = OUTPUT / (package + '.deb')
    run(['dpkg-deb', '--build', '--root-owner-group', str(tree), str(deb)], WORK, env)
    verified = WORK / (package + '-verified')
    run(['dpkg-deb', '--raw-extract', str(deb), str(verified)], WORK, env)
    require(inventory(verified) == after, 'Repacked payload differs')
    return {'original_package_sha256': digest(original), 'package_sha256': digest(deb),
            'control': fields(control.read_text()), 'original_payload': before_payload,
            'installed_payload': after_payload, 'changed_payload': changed}


def build():
    spec = json.loads(MANIFEST.read_text())
    validate_spec(spec)
    require((os.getuid(), os.geteuid(), os.getgid(), os.getegid()) == (10001,) * 4,
            'Requires the unprivileged GStreamer builder')
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/home/gstreamer-builder', 'LANG': 'C.UTF-8',
           'LC_ALL': 'C.UTF-8', 'TZ': 'UTC', 'SOURCE_DATE_EPOCH': str(spec['source_date_epoch']),
           'DEB_BUILD_MAINT_OPTIONS': 'hardening=+all',
           'DEB_LDFLAGS_MAINT_APPEND': '-Wl,-O1 -Wl,-z,defs'}
    arch = run(['dpkg', '--print-architecture'], WORK, env).strip()
    require(arch in TRIPLETS and os.environ.get('TARGETARCH') == arch, 'Requires native architecture')
    build_packages = run(['dpkg-query', '-W', '-f=${Package}\t${Version}\t${Architecture}\n'], WORK, env)
    require(not any(OUTPUT.iterdir()), 'Output must be empty')
    inputs = WORK / 'inputs'
    inputs.mkdir()
    downloads = spec['sources'] | {name: pin for name, pin in spec['binaries'].items()
                                   if name.endswith('_' + arch + '.deb')}
    for name, pin in downloads.items():
        run(['curl', '--fail', '--silent', '--show-error', '--location', '--proto', '=https',
             '--proto-redir', '=https', '--connect-timeout', '15', '--max-time', '180',
             '--max-filesize', str(pin['bytes']), '--output', str(inputs / name), pin['url']],
            WORK, env, 190)
        verify_file(inputs / name, pin)
    source = WORK / 'source'
    source_inventory = prepare(inputs, source, spec, env)
    for name in ('CFLAGS', 'CPPFLAGS', 'LDFLAGS'):
        env[name] = run(['dpkg-buildflags', '--get', name], WORK, env).strip()
    builddir = WORK / 'build'
    setup = ['meson', 'setup', str(builddir), str(source), '--buildtype=plain', '--prefix=/usr',
             '--libdir=lib/' + TRIPLETS[arch], '--auto-features=disabled', '--wrap-mode=nodownload',
             '-Dexamples=disabled', '-Dtools=disabled', '-Dtests=disabled', '-Dintrospection=disabled',
             '-Dnls=disabled', '-Dorc=disabled', '-Ddoc=disabled']
    run(setup, WORK, env, 180)
    command = ['ninja', '-j2', '-C', str(builddir), TARGET]
    run(command, WORK, env, 600)
    require(inventory(source) == source_inventory, 'Compiler modified source inputs')
    built = builddir / TARGET
    run(['strip', '--strip-unneeded', str(built)], WORK, env)
    relative = 'usr/lib/' + TRIPLETS[arch] + '/' + LIBRARY
    original_tree = WORK / 'abi-original'
    run(['dpkg-deb', '--extract', str(inputs / f'{LIB_PACKAGE}_{VERSION}_{arch}.deb'),
         str(original_tree)], WORK, env)
    old = original_tree / relative
    require(digest(old) == spec['original_library_sha256'][arch], 'Original library bytes changed')
    observations = []
    for path in (old, built):
        with path.open('rb') as stream:
            header = stream.read(20)
        require(header[:7] == b'\x7fELF\x02\x01\x01' and len(header) == 20
                and int.from_bytes(header[16:18], 'little') == 3
                and int.from_bytes(header[18:20], 'little') == {'amd64': 62, 'arm64': 183}[arch],
                'Library architecture/type differs')
        observations.append((run(['nm', '-D', '--defined-only', '--format=posix', str(path)], WORK, env),
                             run(['readelf', '-d', str(path)], WORK, env)))
    symbol_count = check_abi(observations[0][0], observations[1][0],
                             observations[0][1], observations[1][1])
    smoke = WORK / 'smoke.c'
    smoke.write_text(SMOKE)
    flags = shlex.split(run(['pkg-config', '--cflags', '--libs', 'gstreamer-base-1.0'], WORK, env))
    run(['cc', '-Wall', '-Wextra', '-Werror', '-I', str(source / 'gst-libs'), str(smoke),
         str(built), '-Wl,-rpath,' + str(built.parent), *flags, '-o', str(WORK / 'smoke')], WORK, env)
    run([str(WORK / 'smoke')], WORK, env, 10)
    packages = {package: repack(package, arch, inputs / f'{package}_{VERSION}_{arch}.deb',
                                built, relative, env) for package in PACKAGES}
    notices = OUTPUT / 'notices'
    notices.mkdir()
    for path, name in ((MANIFEST, 'source-manifest.json'), (ROOT / spec['patch']['path'], 'backport.patch'),
                       (source / 'COPYING', 'COPYING'), (source / 'debian/copyright', 'copyright'),
                       (source / 'debian/changelog', 'original-changelog')):
        shutil.copyfile(path, notices / name)
    installed = ''.join(entry['sha256'] + '  /' + name + '\n'
                        for package in packages.values()
                        for name, entry in package['installed_payload'].items() if entry['type'] == 'file')
    (OUTPUT / 'installed.sha256').write_text(installed)
    (notices / 'build-provenance.json').write_text(json.dumps({
        'schema': 1, 'source': spec, 'builder_sha256': digest(Path(__file__)), 'architecture': arch,
        'uid': os.getuid(), 'setup_command': setup, 'build_command': command,
        'build_environment': env, 'build_packages': build_packages,
        'source_inventory_sha256': hashlib.sha256(json.dumps(
            source_inventory, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'source_file_count': len(source_inventory),
        'patched_parser_sha256': digest(source / PATCH_TARGET), 'packages': packages,
        'library_path': relative, 'original_library_sha256': digest(old), 'library_sha256': digest(built),
        'soname': SONAME, 'abi_symbol_count': symbol_count, 'abi_observations': observations,
        'ordinary_parser_allocation_smoke_passed': True, 'full_gstreamer_suite_executed': False,
        'image_scan_accepted': False}, indent=2) + '\n')


if __name__ == '__main__':
    build()
