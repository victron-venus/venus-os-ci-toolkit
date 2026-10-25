#!/usr/bin/env python3
"""Rebuild Ubuntu's libbpf1 with one pinned upstream security backport."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / '.github/containers/runner-libbpf.json'
WORK = Path('/opt/victron-libbpf-build')
OUTPUT = Path('/opt/victron-libbpf')
ORIG = 'libbpf_1.3.0.orig.tar.gz'
DEBIAN = 'libbpf_1.3.0-2build2.debian.tar.xz'
DSC = 'libbpf_1.3.0-2build2.dsc'
PATCH = '0001-restrict-core-poisoning.patch'
SOURCE_OPTIONS = ['--no-unapply-patches']
BUILD_COMMAND = ['dpkg-buildpackage', '-us', '-uc', '-b', '-rfakeroot', '-j2',
                 '--source-option=--no-unapply-patches']


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def environment(spec):
    # No inherited compiler flags, preload hooks, credentials or proxy settings.
    return {'PATH': '/usr/bin:/bin', 'HOME': '/home/libbpf-builder',
            'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TZ': 'UTC',
            'DEB_BUILD_OPTIONS': 'parallel=2', 'DPKG_GENSYMBOLS_CHECK_LEVEL': '4',
            'SOURCE_DATE_EPOCH': str(spec['source_date_epoch'])}


def run(args, cwd, env, timeout=180):
    try:
        return subprocess.run(args, cwd=cwd, env=env, check=True, text=True,
                              stdout=subprocess.PIPE, timeout=timeout).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        output = error.stdout or ''
        print(output.decode(errors='replace') if isinstance(output, bytes) else output, end='')
        raise


def validate_spec(spec):
    require(spec['schema'] == 1 and spec['source_package'] == 'libbpf', 'Unexpected manifest')
    require(spec['ubuntu_source_version'] == '1.3.0-2build2'
            and spec['upstream_version'] == '1.3.0', 'Re-review new upstream sources')
    require(spec['downstream_source_version'] == spec['ubuntu_source_version'] + '+victron1'
            and spec['downstream_binary_version'] == '1:' + spec['downstream_source_version'],
            'Downstream package version must retain the distro epoch and identify the backport')
    require(spec['soname'] == 'libbpf.so.1' and spec['library'] == 'libbpf.so.1.3.0'
            and spec['architectures'] == ['amd64', 'arm64'], 'Unexpected ABI or architectures')
    require(set(spec['sources']) == {ORIG, DEBIAN, DSC}, 'Unexpected source inputs')
    for name, source in spec['sources'].items():
        require(source['url'] == 'https://archive.ubuntu.com/ubuntu/pool/main/libb/libbpf/' + name,
                'Unexpected source origin')
        require(re.fullmatch('[0-9a-f]{64}', source['sha256'])
                and 0 < source['bytes'] <= 2 * 1024 * 1024, 'Invalid source pin')
    require(spec['patch']['path'] == '.github/containers/libbpf/' + PATCH
            and spec['patch']['target'] == 'src/relo_core.c', 'Unexpected patch target')


def verify_inputs(inputs, spec):
    for name, source in spec['sources'].items():
        path = inputs / name
        require(path.is_file() and not path.is_symlink()
                and path.stat().st_size == source['bytes'] and digest(path) == source['sha256'],
                'Source archive identity changed: ' + name)
    dsc = (inputs / DSC).read_text()
    require('Source: libbpf\n' in dsc and 'Version: 1.3.0-2build2\n' in dsc,
            'Unexpected Ubuntu source record')
    for name in (ORIG, DEBIAN):
        source = spec['sources'][name]
        require(f" {source['sha256']} {source['bytes']} {name}\n" in dsc,
                'Source record checksum differs')


def extract(archive_path, destination, prefix):
    """These two pinned archives contain only directories and ordinary files."""
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        require(len(members) <= 500 and sum(m.size for m in members) <= 10 * 1024 * 1024,
                'Source archive exceeds reviewed bounds')
        seen, files = set(), set()
        for member in members:
            name = member.name.rstrip('/')
            path = PurePosixPath(name)
            require(name and str(path) == name and not path.is_absolute()
                    and '..' not in path.parts and path.parts[0] == prefix,
                    'Unsafe source member path')
            require(name not in seen and (member.isdir() or member.isreg())
                    and not member.issparse() and not member.mode & 0o7000,
                    'Unexpected source member type or duplicate')
            seen.add(name)
            if member.isreg():
                files.add(name)
        require(not any(str(parent) in files for name in seen
                        for parent in PurePosixPath(name).parents), 'File is a member ancestor')
        for member in members:
            path = PurePosixPath(member.name.rstrip('/'))
            parts = path.parts[1:] if prefix != 'debian' else path.parts
            target = destination.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as src, target.open('xb') as out:
                    shutil.copyfileobj(src, out)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)


def snapshot(root):
    return {str(p.relative_to(root)): (digest(p), p.stat().st_mode & 0o777)
            for p in root.rglob('*') if p.is_file()}


def prepare(inputs, source, spec, patch_path, env):
    verify_inputs(inputs, spec)
    require(not source.exists() and not source.is_symlink(), 'Source tree already exists')
    require(patch_path.is_file() and not patch_path.is_symlink()
            and digest(patch_path) == spec['patch']['sha256'], 'Backport identity changed')
    source.mkdir()
    extract(inputs / ORIG, source, 'libbpf-1.3.0')
    extract(inputs / DEBIAN, source, 'debian')
    before = snapshot(source)
    require(before['src/relo_core.c'][0] == spec['patch']['before_sha256'],
            'Original relocation source changed')
    require(before['src/libbpf.map'][0] == spec['abi']['map_sha256']
            and before['debian/libbpf1.symbols'][0] == spec['abi']['debian_symbols_sha256'],
            'Original ABI declarations changed')
    patches = source / 'debian/patches'
    patches.mkdir()
    shutil.copyfile(patch_path, patches / PATCH)
    (patches / 'series').write_text(PATCH + '\n')
    # Use the distro's quilt-format source hook; it owns its .pc bookkeeping.
    run(['dpkg-source', *SOURCE_OPTIONS, '--before-build', '.'], source, env)
    after = snapshot(source)
    original_after = {name: after[name] for name in before}
    require({name for name in before if before[name] != original_after[name]} == {'src/relo_core.c'}
            and after['src/relo_core.c'][0] == spec['patch']['after_sha256'],
            'Backport changed unexpected source bytes or modes')
    require(all(name in before or name.startswith('.pc/') or name in {
        'debian/patches/' + PATCH, 'debian/patches/series'} for name in after),
        'Backport added unexpected source files')
    changelog = source / 'debian/changelog'
    entry = (f"libbpf ({spec['downstream_source_version']}) noble; urgency=medium\n\n"
             '  * Backport upstream 394ae398337c5f87e567f6cd63b937fc2b2f6ddc\n'
             '    for CVE-2026-98626; retain the libbpf1 ABI and distro build rules.\n\n'
             ' -- alvit <272257197+4alvit@users.noreply.github.com>  Sun, 25 Oct 2026 00:00:00 +0000\n\n')
    changelog.write_text(entry + changelog.read_text())
    return before


def check_exports(symbols_text, nm_text):
    expected = {line.split()[0] for line in symbols_text.splitlines() if line.startswith(' ')}
    actual = set()
    for line in nm_text.splitlines():
        if not line.strip():
            continue
        symbol = line.split()[-1].replace('@@', '@')
        if symbol.startswith('LIBBPF_') and '@' not in symbol:
            symbol += '@' + symbol
        actual.add(symbol)
    require(expected and actual == expected, 'Versioned public ABI symbols changed')
    return len(expected)


def check_elf(path, arch):
    with path.open('rb') as stream:
        header = stream.read(20)
    machine = {'amd64': 62, 'arm64': 183}[arch]
    require(header[:7] == b'\x7fELF\x02\x01\x01' and len(header) == 20
            and int.from_bytes(header[16:18], 'little') == 3
            and int.from_bytes(header[18:20], 'little') == machine,
            'Shared library ELF architecture/type changed')


def check_soname(dynamic, expected):
    lines = [line for line in dynamic.splitlines() if '(SONAME)' in line]
    require(len(lines) == 1 and lines[0].count('(SONAME)') == 1,
            'Missing or duplicate SONAME field')
    line = lines[0]
    _, opening, value = line.partition('(SONAME)')[2].partition('[')
    name, closing, tail = value.partition(']')
    require(opening and closing and line.count('[') == line.count(']') == 1
            and name == expected and not tail.strip(), 'Malformed or changed SONAME')


SMOKE = r'''
#include <string.h>
#include "libbpf.h"
#include "btf.h"
int main(void) {
    struct btf *types;
    int id;
    if (libbpf_major_version() != 1 || libbpf_minor_version() != 3 ||
        strcmp(libbpf_version_string(), "v1.3")) return 1;
    types = btf__new_empty();
    if (libbpf_get_error(types)) return 2;
    id = btf__add_int(types, "ordinary_u32", 4, 0);
    if (id != 1 || btf__type_cnt(types) != 2 ||
        btf__find_by_name(types, "ordinary_u32") != id) return 3;
    btf__free(types);
    return 0;
}
'''


def build():
    spec = json.loads(MANIFEST.read_text())
    validate_spec(spec)
    require((os.getuid(), os.geteuid(), os.getgid(), os.getegid()) == (10001,) * 4,
            'Source builds require the unprivileged libbpf-builder identity')
    env = environment(spec)
    arch = run(['dpkg', '--print-architecture'], WORK, env).strip()
    require(arch in spec['architectures'] and os.environ.get('TARGETARCH') == arch,
            'Build must be native for the requested architecture')
    inputs, source = WORK / 'inputs', WORK / 'source'
    inputs.mkdir()
    require(not any(OUTPUT.iterdir()), 'Output must be empty')
    for name, pin in spec['sources'].items():
        run(['curl', '--fail', '--silent', '--show-error', '--location',
             '--proto', '=https', '--proto-redir', '=https', '--connect-timeout', '15',
             '--max-time', '180', '--max-filesize', str(pin['bytes']),
             '--output', str(inputs / name), pin['url']], WORK, env, 190)
    original = prepare(inputs, source, spec, ROOT / spec['patch']['path'], env)
    build_log = run(BUILD_COMMAND, source, env, 600)
    print(build_log, end='')
    require(digest(source / 'src/relo_core.c') == spec['patch']['after_sha256']
            and digest(source / 'src/libbpf.map') == spec['abi']['map_sha256']
            and digest(source / 'debian/libbpf1.symbols') == spec['abi']['debian_symbols_sha256'],
            'Build changed pinned source or ABI files')
    deb = WORK / f"libbpf1_{spec['downstream_source_version']}_{arch}.deb"
    fields = run(['dpkg-deb', '-f', str(deb), 'Package', 'Version', 'Architecture'], WORK, env)
    require(fields.splitlines() == ['Package: libbpf1',
            'Version: ' + spec['downstream_binary_version'], 'Architecture: ' + arch],
            'Built package identity changed')
    package = WORK / 'package'
    run(['dpkg-deb', '-x', str(deb), str(package)], WORK, env)
    multiarch = run(['dpkg-architecture', '-qDEB_HOST_MULTIARCH'], WORK, env).strip()
    relative = Path('usr/lib') / multiarch / spec['library']
    library = package / relative
    check_elf(library, arch)
    dynamic = run(['readelf', '-d', str(library)], WORK, env)
    check_soname(dynamic, spec['soname'])
    symbols = run(['nm', '-D', '--defined-only', str(library)], WORK, env)
    count = check_exports((source / 'debian/libbpf1.symbols').read_text(), symbols)
    smoke = WORK / 'smoke.c'
    smoke.write_text(SMOKE)
    run(['cc', '-Wall', '-Wextra', '-Werror', '-I', str(source / 'src'), str(smoke),
         str(library), '-Wl,-rpath,' + str(library.parent), '-o', str(WORK / 'smoke')], WORK, env)
    run([str(WORK / 'smoke')], WORK, env, 10)
    notices = OUTPUT / 'notices'
    notices.mkdir()
    for name in ('LICENSE', 'LICENSE.BSD-2-Clause', 'LICENSE.LGPL-2.1', 'debian/copyright',
                 'debian/changelog', 'debian/libbpf1.symbols', 'src/libbpf.map'):
        shutil.copyfile(source / name, notices / name.replace('/', '-'))
    shutil.copyfile(ROOT / spec['patch']['path'], notices / PATCH)
    shutil.copyfile(MANIFEST, notices / 'source-manifest.json')
    buildinfo = WORK / f"libbpf_{spec['downstream_source_version']}_{arch}.buildinfo"
    shutil.copyfile(buildinfo, notices / 'package.buildinfo')
    shutil.copyfile(deb, OUTPUT / 'libbpf1.deb')
    (OUTPUT / 'installed.sha256').write_text(digest(library) + '  /' + str(relative) + '\n')
    provenance = {'source': spec, 'architecture': arch, 'uid': os.getuid(),
                  'build_command': BUILD_COMMAND,
                  'original_source_files': original, 'abi_symbol_count': count,
                  'buildinfo_sha256': digest(buildinfo),
                  'symbol_proof_sha256': hashlib.sha256(symbols.encode()).hexdigest(),
                  'soname': spec['soname'], 'library_sha256': digest(library),
                  'package_sha256': digest(deb), 'package_fields': fields.splitlines(),
                  'ordinary_userspace_smoke_passed': True, 'kernel_bpf_operations': False}
    (notices / 'build-provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    (notices / 'build.log').write_text(build_log)
    (notices / 'dynamic-symbols.txt').write_text(symbols)


if __name__ == '__main__':
    build()
