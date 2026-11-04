"""Stage bounded, original runner proof files separately from image archives."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat

if __package__:
    from . import runner_image_oci as oci
else:
    import runner_image_oci as oci

MIB = 1024**2
TOTAL_LIMIT = 192 * MIB
LOG_LIMIT = 4 * MIB
FILES = {name: 8 * MIB for name in (
    'build-receipt.json', 'oci-verification.json', 'scan-summary.json',
    'scan-input.json', 'scan-database.json', 'scan-file-evidence.json',
    'scan-acceptance.json', 'cgroup-verification.json',
    'disk-observations.jsonl', 'cgroup-observations.jsonl')}
FILES['scan.json'] = 128 * MIB
FILES['build.log'] = LOG_LIMIT


def copy_proof(source, destination, name, limit, remaining):
    """Reject special files; preserve full proofs and explicitly mark log tails."""
    fd = os.open(source / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        oci.require(stat.S_ISREG(before.st_mode), 'Evidence must be a regular file')
        offset = max(0, before.st_size - limit) if name == 'build.log' else 0
        length = before.st_size - offset
        oci.require(0 <= length <= min(limit, remaining), 'Evidence exceeds byte limit')
        stream.seek(offset)
        size, digest = 0, hashlib.sha256()
        with (destination / name).open('xb') as target:
            while block := stream.read(min(MIB, length - size + 1)):
                size += len(block)
                oci.require(size <= length, 'Evidence grew during staging')
                target.write(block)
                digest.update(block)
        oci.require(size == length and
                    oci.identity(os.fstat(stream.fileno())) == oci.identity(before) and
                    oci.identity((source / name).stat(follow_symlinks=False)) == oci.identity(before),
                    'Evidence changed during staging')
    return {'sha256': digest.hexdigest(), 'bytes': size,
            'original_bytes': before.st_size, 'offset': offset, 'complete': offset == 0}


def stage(source, destination, complete):
    oci.require(source.is_dir() and not source.is_symlink(), 'Invalid evidence source')
    oci.require(not destination.parent.is_symlink(), 'Invalid evidence destination')
    destination.mkdir(mode=0o700, exist_ok=False)
    inventory, missing, total = {}, [], 0
    try:
        for name, limit in FILES.items():
            try:
                (source / name).lstat()
            except FileNotFoundError:
                # A failed build may never produce later proofs. Other IO errors fail closed.
                missing.append(name)
                continue
            inventory[name] = copy_proof(source, destination, name, limit, TOTAL_LIMIT - total)
            total += inventory[name]['bytes']
        oci.require('build-receipt.json' in inventory, 'Build receipt is required')
        oci.require(not complete or not missing, 'Successful build is missing proof files')
        receipt = {'schema': 1, 'requested_complete': complete, 'missing': missing,
                   'proofs_complete': not missing, 'payload_bytes': total, 'files': inventory}
        (destination / 'evidence-inventory.json').write_text(json.dumps(receipt, indent=2) + '\n')
        return receipt
    except BaseException:
        shutil.rmtree(destination)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('public', 'private'))
    parser.add_argument('--complete', choices=('true', 'false'), required=True)
    args = parser.parse_args()
    temporary = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    suffix = args.stage + '-' + os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT']
    # Fixed workflow values, but reject path components even when invoked manually.
    oci.require(all(value.isascii() and value.isdecimal() for value in
                    (os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_RUN_ATTEMPT'])), 'Invalid run identity')
    stage(temporary / ('runner-image-' + suffix),
          temporary / ('runner-image-evidence-' + suffix), args.complete == 'true')


if __name__ == '__main__':
    main()
