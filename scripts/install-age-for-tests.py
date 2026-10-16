#!/usr/bin/env python3
"""Install pinned official age binaries for synthetic tests; never system-wide."""

import hashlib
import io
from pathlib import Path
import platform
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


def install(destination):
    system, machine = platform.system(), platform.machine()
    expected = DIGESTS[(system, machine)]
    architecture = 'arm64' if machine in ('arm64', 'aarch64') else 'amd64'
    name = f'age-v{VERSION}-{system.lower()}-{architecture}.tar.gz'
    url = f'https://github.com/FiloSottile/age/releases/download/v{VERSION}/{name}'
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = response.read(40 * 1024 * 1024 + 1)
    if hashlib.sha256(archive).hexdigest() != expected:
        raise ValueError('age release archive digest mismatch')
    destination = Path(destination)
    destination.mkdir(mode=0o700, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        for binary in ('age', 'age-keygen'):
            member = tar.getmember('age/' + binary)
            if not member.isfile() or member.size > 30 * 1024 * 1024:
                raise ValueError('unexpected age archive member')
            with tar.extractfile(member) as source, (destination / binary).open('xb') as output:
                output.write(source.read())
            (destination / binary).chmod(0o700)
    print(f'Installed age {VERSION}; release archive SHA-256 verified')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: install-age-for-tests.py NEW_PRIVATE_DIRECTORY')
    install(sys.argv[1])
