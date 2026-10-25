#!/usr/bin/env python3
"""Install signed, pinned browser packages into the disposable reserve image."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / '.github/containers/runner-browsers.json'
SNAPSHOTS = LOCK.parent / 'runner-browsers'
PROVENANCE = Path('/usr/local/share/victron-browsers')
MAX_METADATA = 2 * 1024 * 1024
CHANNELS = {'chrome', 'chrome-beta', 'msedge', 'msedge-beta'}
ORIGINS = {'google': 'https://dl.google.com/linux/chrome/deb/',
           'edge': 'https://packages.microsoft.com/repos/edge/'}


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(args, timeout=60):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE,
                          timeout=timeout).stdout


def metadata(pin, root):
    name = pin['path']
    require(PurePosixPath(name).name == name and name not in ('', '.', '..'),
            'Metadata must be a direct child')
    path = root / name
    require(path.is_file() and not path.is_symlink(), 'Missing metadata file')
    require(type(pin['bytes']) is int and 0 < pin['bytes'] <= MAX_METADATA,
            'Invalid metadata size')
    with path.open('rb') as stream:
        data = stream.read(MAX_METADATA + 1)
    require(len(data) == pin['bytes'] and hashlib.sha256(data).hexdigest() == pin['sha256'],
            'Metadata digest/size mismatch')
    return path, data


def verify_signature(status, fingerprint):
    valid = [line.split() for line in status.splitlines() if line.startswith('[GNUPG:] VALIDSIG ')]
    require(len(valid) == 1 and valid[0][-1] == fingerprint, 'Unexpected Release signer')
    require(not any('[GNUPG:] ' + code in status for code in
                    ('BADSIG', 'ERRSIG', 'REVKEYSIG', 'EXPKEYSIG', 'EXPSIG')),
            'Invalid Release signature')


def release_index(release, name):
    section = False
    matches = []
    for line in release.splitlines():
        if line and not line[0].isspace():
            section = line == 'SHA256:'
        elif section:
            fields = line.split()
            require(len(fields) == 3, 'Malformed Release SHA256 row')
            if fields[2] == name:
                matches.append((fields[0], int(fields[1])))
    require(len(matches) == 1, 'Missing or ambiguous signed package index')
    return matches[0]


def package_rows(text):
    rows = []
    for paragraph in text.strip().split('\n\n'):
        row = {}
        for line in paragraph.splitlines():
            if line.startswith((' ', '\t')):
                continue  # No selected identity field is a continuation.
            key, separator, value = line.partition(': ')
            require(separator and key not in row, 'Malformed package metadata')
            row[key] = value
        rows.append(row)
    return rows


def bind_package(package, rows):
    selected = [row for row in rows if row.get('Package') == package['name']
                and row.get('Version') == package['version']
                and row.get('Architecture') == package['architecture']]
    require(len(selected) == 1, 'Missing or ambiguous exact package')
    row = selected[0]
    require((row['Filename'], row['SHA256'], int(row['Size']), int(row['Installed-Size']) * 1024)
            == (package['filename'], package['sha256'], package['bytes'], package['installed_bytes']),
            'Package differs from signed index')
    path = package['filename']
    require(path.startswith('pool/') and all(part not in ('', '.', '..') for part in path.split('/'))
            and re.fullmatch(r'[a-zA-Z0-9_./+~-]+', path), 'Invalid package URL path')
    require(type(package['bytes']) is int and 0 < package['bytes'] <= 256 * 1024 * 1024,
            'Package exceeds download bound')


def validate_lock(lock):
    require(type(lock.get('schema')) is int and lock['schema'] == 1, 'Unknown browser lock schema')
    require(lock['distribution'] == 'ubuntu24.04', 'Unexpected browser distribution')
    require(set(lock['vendors']) == set(ORIGINS), 'Unexpected vendor set')
    require(len(lock['packages']) == 4 and {p['channel'] for p in lock['packages']} == CHANNELS
            and len({p['name'] for p in lock['packages']}) == 4,
            'Incomplete branded browser set')
    for package in lock['packages']:
        require(package['architecture'] == 'amd64' and package['vendor'] in ORIGINS,
                'Unexpected browser architecture/vendor')
        require(re.fullmatch(r'[a-z][a-z0-9-]+', package['name']), 'Invalid package name')
        require(re.fullmatch(r'[0-9]+(?:\.[0-9]+){3}-[0-9]+', package['version']), 'Invalid package version')
        require(package['executable'].startswith(('/opt/google/', '/opt/microsoft/')),
                'Unexpected browser path')
    architectures = lock['architectures']
    require(set(architectures) == {'amd64', 'arm64'}
            and architectures['amd64']['full_branded'] is True
            and set(architectures['amd64']['channels']) == CHANNELS
            and architectures['arm64']['full_branded'] is False
            and architectures['arm64']['channels'] == [], 'Incorrect architecture capability')
    dependencies = lock['playwright']['dependencies']
    require(dependencies == sorted(set(dependencies)) and len(dependencies) == 79
            and all(re.fullmatch(r'[a-z0-9][a-z0-9.+-]+', name) for name in dependencies),
            'Invalid native dependency union')


def authenticate(lock, snapshots=SNAPSHOTS):
    validate_lock(lock)
    for vendor, identity in lock['vendors'].items():
        require(identity['origin'] == ORIGINS[vendor], 'Unexpected repository origin')
        files = {key: metadata(pin, snapshots) for key, pin in identity['files'].items()}
        require(set(files) == {'release', 'signature', 'key', 'packages'}, 'Incomplete signature chain')
        with tempfile.TemporaryDirectory(prefix='browser-signature-') as directory:
            status = run(['gpgv', '--homedir', directory, '--status-fd', '1', '--keyring',
                          str(files['key'][0].resolve()), str(files['signature'][0]), str(files['release'][0])])
        verify_signature(status, identity['fingerprint'])
        digest, size = release_index(files['release'][1].decode(), identity['index_path'])
        index = files['packages'][1]
        require(len(index) == size and hashlib.sha256(index).hexdigest() == digest,
                'Index differs from signed Release')
        rows = package_rows(index.decode())
        for package in lock['packages']:
            if package['vendor'] == vendor:
                bind_package(package, rows)


def selected_packages(lock, architecture):
    require(architecture in ('amd64', 'arm64'), 'Unsupported native architecture')
    return lock['packages'] if architecture == 'amd64' else []


def updater_paths():
    # Vendor postinst scripts create these outside the package file list.
    names = ('google-chrome', 'google-chrome-beta', 'microsoft-edge', 'microsoft-edge-beta')
    return [Path(parent) / (name + suffix) for parent, suffix in
            (('/etc/apt/sources.list.d', '.list'), ('/etc/apt/sources.list.d', '.sources'),
             ('/etc/cron.daily', '')) for name in names]


def disable_updaters(paths):
    removed = []
    for path in paths:
        if not path.exists() and not path.is_symlink():
            continue
        require(path.is_symlink() or path.is_file(), 'Unexpected vendor updater object')
        removed.append({'path': str(path), 'symlink': os.readlink(path) if path.is_symlink() else None,
                        'sha256': None if path.is_symlink() else sha(path)})
        path.unlink()
    return removed


def installed_record(package):
    identity = run(['dpkg-query', '-W', '-f=${Package}\t${Version}\t${Architecture}\t${Status}',
                    package['name']]).strip().split('\t')
    require(identity == [package['name'], package['version'], 'amd64', 'install ok installed'],
            'Installed browser identity mismatch')
    executable = Path(package['executable'])
    require(executable.is_file() and not executable.is_symlink(), 'Missing browser executable')
    version = run([str(executable), '--version']).strip()
    require(package['version'].rsplit('-', 1)[0] in version.split(), 'Browser version mismatch')
    return {**package, 'executable_sha256': sha(executable), 'version_output': version}


def install(lock, architecture):
    authenticate(lock)
    packages = selected_packages(lock, architecture)
    paths = updater_paths() if packages else []
    require(all(not p.exists() and not p.is_symlink() for p in paths), 'Pre-existing vendor updater')
    defaults = [Path('/etc/default') / name for name in
                ('google-chrome', 'google-chrome-beta', 'microsoft-edge', 'microsoft-edge-beta')] if packages else []
    require(all(not p.exists() and not p.is_symlink() for p in defaults), 'Pre-existing browser defaults')
    require(not PROVENANCE.exists(), 'Browser provenance already exists')
    with tempfile.TemporaryDirectory(prefix='runner-browsers-') as directory:
        work = Path(directory)
        archives = []
        for package in packages:
            archive = work / (package['name'] + '.deb')
            url = ORIGINS[package['vendor']] + package['filename']
            run(['curl', '--fail', '--silent', '--show-error', '--location', '--proto', '=https',
                 '--proto-redir', '=https', '--connect-timeout', '15', '--max-time', '240',
                 '--max-filesize', str(package['bytes']), url, '--output', str(archive)], timeout=250)
            require(archive.stat().st_size == package['bytes'] and sha(archive) == package['sha256'],
                    'Browser archive digest/size mismatch')
            identity = run(['dpkg-deb', '--show', '--showformat=${Package}\t${Version}\t${Architecture}',
                            str(archive)]).split('\t')
            require(identity == [package['name'], package['version'], 'amd64'], 'Archive identity mismatch')
            archives.append(str(archive))
        # The reviewed vendor postinst scripts honor these settings. No moving
        # vendor repository is enabled during later private-image apt steps.
        for path in defaults:
            path.write_text('repo_add_once="false"\nrepo_reenable_on_distupgrade="false"\n')
        run(['apt-get', 'update'], timeout=300)
        run(['apt-get', 'install', '-y', '--no-install-recommends',
             *lock['playwright']['dependencies'], *archives], timeout=900)
        removed = disable_updaters(paths)
        records = [installed_record(package) for package in packages]
        dependencies = run(['dpkg-query', '-W', '-f=${Package}\t${Version}\t${Architecture}\t${Status}\n',
                            *lock['playwright']['dependencies']]).splitlines()
        require(len(dependencies) == 79 and all(line.endswith('\tinstall ok installed')
                                               for line in dependencies), 'Incomplete native dependencies')
        PROVENANCE.mkdir(parents=True)
        shutil.copyfile(LOCK, PROVENANCE / 'source-lock.json')
        (PROVENANCE / 'installed.json').write_text(json.dumps({
            'schema': 1, 'architecture': architecture, 'lock_sha256': sha(LOCK),
            'capability': lock['architectures'][architecture], 'packages': records,
            'native_dependencies': dependencies, 'disabled_updaters': removed,
            'functional_browser_qualification': 'pending', 'managed_browser_binaries_included': False,
        }, indent=2, sort_keys=True) + '\n')
    run(['apt-get', 'clean'])


def qualify(architecture):
    record = json.loads((PROVENANCE / 'installed.json').read_text())
    require(record['architecture'] == architecture, 'Qualification architecture mismatch')
    if architecture == 'arm64':
        result = {'schema': 1, 'architecture': architecture, 'full_branded_qualified': False,
                  'reason': 'unsupported four-channel capability', 'gvisor_qualified': False}
    else:
        require(architecture == 'amd64', 'Unsupported qualification architecture')
        toolchains = json.loads((ROOT / 'scripts/browser-toolchains.json').read_text())
        version = toolchains['current_playwright']
        require(version == '1.64.0', 'Review browser qualification on toolchain change')
        metadata = toolchains['playwright'][version]['packages']
        require(set(metadata) == {'playwright', 'playwright-core'}, 'Unexpected qualification dependency set')
        package = {'name': 'runner-browser-probe', 'version': '1.0.0', 'private': True,
                   'dependencies': {'playwright': version}}
        lock = {'name': package['name'], 'version': package['version'], 'lockfileVersion': 3,
                'requires': True, 'packages': {'': package}}
        for name, pin in metadata.items():
            lock['packages']['node_modules/' + name] = {
                'version': version, 'resolved': f'https://registry.npmjs.org/{name}/-/{name}-{version}.tgz',
                'integrity': pin['integrity'],
                **({'dependencies': {'playwright-core': version}} if name == 'playwright' else {})}
        with tempfile.TemporaryDirectory(prefix='browser-probe-') as directory:
            work = Path(directory)
            work.chmod(0o755)
            for name, data in [('package.json', package), ('package-lock.json', lock)]:
                (work / name).write_text(json.dumps(data) + '\n')
            cache = work / 'cache'
            cache.mkdir()
            for path in [work, *work.iterdir()]:
                os.chown(path, 1001, 1001)
            run(['timeout', '--signal=TERM', '--kill-after=15s', '240s', 'runuser', '-u', 'runner', '--',
                 'env', 'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1', 'npm_config_cache=' + str(cache),
                 'npm', 'ci', '--prefix', str(work), '--ignore-scripts', '--omit=optional',
                 '--no-audit', '--no-fund'], timeout=260)
            result = json.loads(run(['timeout', '--signal=TERM', '--kill-after=15s', '180s',
                                     'runuser', '-u', 'runner', '--', 'node',
                                     '/opt/victron-ci-reserve/qualify-browsers.cjs',
                                     str(work), str(PROVENANCE / 'installed.json')], timeout=200))
            require(result['full_branded_qualified'] is True and result['uid'] == 1001,
                    'Browser probe did not qualify all channels')
    (PROVENANCE / 'qualification.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')


def main():
    require(__debug__, 'Optimized Python is unsupported')
    require(os.getuid() == 0, 'Image installation requires root')
    require(sys.argv[1:] in ([], ['--qualify']), 'Unknown installer arguments')
    architecture = run(['dpkg', '--print-architecture']).strip()
    if sys.argv[1:]:
        qualify(architecture)
    else:
        install(json.loads(LOCK.read_text()), architecture)


if __name__ == '__main__':
    main()
