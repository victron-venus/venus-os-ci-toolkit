#!/usr/bin/env python3
"""Compose a local candidate from a captured template; never install or restart."""
import argparse
import hashlib
import os
from pathlib import Path
import stat

FRAGMENT = Path(__file__).with_name('containerd-exclusive-runtime.toml.fragment')
CANDIDATE_NAME = 'config.toml.tmpl.candidate'


def compose(original, expected_sha256):
    if hashlib.sha256(original).hexdigest() != expected_sha256:
        raise ValueError('Captured template changed; obtain a fresh reviewed snapshot')
    if b'victron-private-exclusive' in original:
        raise ValueError('Private handler already exists; do not append another definition')
    # Preserve the entire existing template, including dynamic K3s runtime blocks.
    return original + b'\n' + FRAGMENT.read_bytes()


def write_candidate(snapshot, expected_sha256):
    """Write only a fixed sibling in the operator's protected snapshot directory."""
    snapshot = Path(snapshot)
    directory = os.open(snapshot.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        owner = os.fstat(directory)
        if owner.st_uid != os.geteuid() or stat.S_IMODE(owner.st_mode) != 0o700:
            raise ValueError('Snapshot directory must be owned by this user with mode 0700')
        source = os.open(snapshot.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(source).st_mode):
                raise ValueError('Snapshot must be a regular file, not a symlink or device')
            with os.fdopen(source, 'rb', closefd=False) as captured:
                candidate = compose(captured.read(), expected_sha256)
        finally:
            os.close(source)
        # A fixed name and anchored directory descriptor prevent path redirection.
        target = os.open(CANDIDATE_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
        with os.fdopen(target, 'wb') as output:
            output.write(candidate)
    finally:
        os.close(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    args = parser.parse_args()
    write_candidate(args.input, args.expected_sha256)
    print(f'Wrote {CANDIDATE_NAME} beside the snapshot; no host configuration was changed')


if __name__ == '__main__':
    main()
