#!/usr/bin/env python3
"""Compose a local candidate from a captured template; never install or restart."""
import argparse
import hashlib
import os
from pathlib import Path

FRAGMENT = Path(__file__).with_name('containerd-exclusive-runtime.toml.fragment')


def compose(original, expected_sha256):
    if hashlib.sha256(original).hexdigest() != expected_sha256:
        raise ValueError('Captured template changed; obtain a fresh reviewed snapshot')
    if b'victron-private-exclusive' in original:
        raise ValueError('Private handler already exists; do not append another definition')
    # Preserve the entire existing template, including dynamic K3s runtime blocks.
    return original + b'\n' + FRAGMENT.read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error('Output must be a separate new local candidate')
    candidate = compose(args.input.read_bytes(), args.expected_sha256)
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as target:
        target.write(candidate)
    print('Wrote local candidate only; no host configuration was changed')


if __name__ == '__main__':
    main()
