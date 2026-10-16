#!/usr/bin/env python3
"""Install pinned official age binaries for synthetic tests; never system-wide."""

import hashlib
import io
import os
from pathlib import Path
import platform
import stat
import sys
import tarfile
import urllib.request


VERSION = '1.3.2'
# GitHub release asset SHA-256 digests, verified when updating this pin.
DIGESTS = {
    ('Darwin', 'arm64'): 'e2020b073c44f692685a24d6abc378817eb81ffaaf49fd0531ef8565f767f2f5',
    ('Darwin', 'x86_64'): '1d1e4bc66e1427edad7739ae7616157de0e79db8b6d2a1497d7d9925fb06a539',
    ('Linux', 'aarch64'): '6b8dc4333c53a5a57c9e5834e3a48f92605d7154014cd07269ff3327db5d37f4',
    ('Linux', 'x86_64'): 'cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10',
}


def runner_temp():
    """Open the CI-owned base without following any directory symlinks."""
    raw = os.environ.get('RUNNER_TEMP', '')
    path = Path(raw)
    if not path.is_absolute() or len(path.parts) < 2 or '..' in path.parts:
        raise ValueError('RUNNER_TEMP must be an absolute existing directory')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError('RUNNER_TEMP must be owned and not writable by others')
        return fd
    except BaseException:
        os.close(fd)
        raise


def download_archive():
    system, machine = platform.system(), platform.machine()
    expected = DIGESTS[(system, machine)]
    architecture = 'arm64' if machine in ('arm64', 'aarch64') else 'amd64'
    name = f'age-v{VERSION}-{system.lower()}-{architecture}.tar.gz'
    url = f'https://github.com/FiloSottile/age/releases/download/v{VERSION}/{name}'
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = response.read(40 * 1024 * 1024 + 1)
    if hashlib.sha256(archive).hexdigest() != expected:
        raise ValueError('age release archive digest mismatch')
    return archive


def write_binaries(archive, directory):
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        for binary in ('age', 'age-keygen'):
            member = tar.getmember('age/' + binary)
            if not member.isfile() or member.size > 30 * 1024 * 1024:
                raise ValueError('unexpected age archive member')
            fd = os.open(binary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700, dir_fd=directory)
            with os.fdopen(fd, 'wb') as output, tar.extractfile(member) as source:
                output.write(source.read())
                os.fchmod(output.fileno(), 0o700)


def install():
    base = runner_temp()
    try:
        # Claim only this fixed child before network access; never overwrite a prior install.
        os.mkdir('secret-control-age', mode=0o700, dir_fd=base)
        directory = os.open('secret-control-age', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base)
        try:
            write_binaries(download_archive(), directory)
        finally:
            os.close(directory)
    finally:
        os.close(base)
    print(f'Installed age {VERSION}; release archive SHA-256 verified')


if __name__ == '__main__':
    if len(sys.argv) != 1:
        raise SystemExit('usage: RUNNER_TEMP=EXISTING_DIRECTORY python install-age-for-tests.py')
    install()
