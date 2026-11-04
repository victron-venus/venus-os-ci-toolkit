"""Preserve accepted builder archives in the existing versioned toolkit release."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

if __package__:
    from . import prepare_accepted_builder as accepted
else:
    import prepare_accepted_builder as accepted

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = 'accepted-builder-provenance.json'
CHECKSUMS = 'SHA256SUMS'
RESERVE = 2 * 1024**3
START_FREE = 4 * 1024**3
require = accepted.require


def release_identity(environment):
    require(environment.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'Hosted release worker required')
    require(environment.get('GITHUB_REPOSITORY') == accepted.REPOSITORY, 'Canonical release repository required')
    source = environment.get('GITHUB_SHA', '')
    require(re.fullmatch(r'[0-9a-f]{40}', source), 'Exact release source required')
    result = {'source': source}
    for name, key in [('GITHUB_RUN_ID', 'run'), ('GITHUB_RUN_ATTEMPT', 'attempt')]:
        value = environment.get(name, '')
        require(re.fullmatch(r'[1-9]\d*', value, flags=re.ASCII), 'Exact release run identity required')
        result[key] = int(value)
    return result


def file_record(path):
    require(path.is_file() and not path.is_symlink(), 'Release asset must be regular')
    return {'sha256': accepted.oci.digest_file(path), 'bytes': path.stat().st_size}


def acquire_architecture(lock, arch, destination, check, snapshot):
    """Verify one architecture and describe its three original release files."""
    proof = accepted.acquire(lock, arch, destination, check, snapshot=snapshot)
    spec = lock['architectures'][arch]
    files, payloads = {}, {}
    for local, suffix in [(accepted.OCI_ARCHIVE, '.oci.tar'),
                          (accepted.DOCKER_ARCHIVE, '.docker.tar'),
                          ('evidence.zip', '.evidence.zip')]:
        name = 'oci-builder-linux-' + arch + suffix
        payloads[name] = destination / local
        files[name] = file_record(payloads[name])
        if local == 'evidence.zip':
            expected = {'sha256': spec['artifacts']['evidence']['digest'][7:],
                        'bytes': spec['artifacts']['evidence']['size_in_bytes']}
        else:
            kind = 'oci' if local == accepted.OCI_ARCHIVE else 'docker'
            expected = {'sha256': spec[kind]['archive_sha256'],
                        'bytes': spec[kind]['archive_bytes']}
        require(files[name] == expected, 'Verified publication bytes changed')
    record = {
        'files': files, 'artifacts': spec['artifacts'],
        'manifest': proof['manifest'], 'config': proof['config'],
        'diff_ids': proof['diff_ids'], 'verification': 'original OCI and Docker exports verified'}
    return payloads, record


def stage(output, lock, release, check):
    """Acquire both architectures serially; retain exact bytes, never execute images."""
    require(output.is_dir() and not output.is_symlink(), 'Source release directory required')
    source_archives = list(output.glob('venus-os-ci-toolkit-*.tar.gz'))
    require(len(source_archives) == 1 and
            {p.name for p in output.iterdir()} == {source_archives[0].name, CHECKSUMS},
            'Expected only the verified source archive and its checksums')
    source_archive = source_archives[0]
    source_record = file_record(source_archive)
    checksums = output / CHECKSUMS
    require(file_record(checksums)['bytes'] <= 4096, 'Source checksum inventory oversized')
    require(checksums.read_text() == source_record['sha256'] + '  ' + source_archive.name + '\n',
            'Source archive checksum differs')
    accepted.validate_lock(lock)
    snapshot = accepted.read_transport()
    provenance = {'schema': 1, 'repository': accepted.REPOSITORY, 'release': release,
                  'qualification': {'source': lock['source'], 'run': lock['qualification_run'],
                                    'attempt': lock['attempt'], 'workflow': lock['workflow']},
                  'lock_sha256': accepted.oci.digest_file(accepted.LOCK), 'architectures': {},
                  'transport': accepted.transport_receipt(snapshot)}
    created = []
    committed = False
    try:
        with tempfile.TemporaryDirectory(prefix='.accepted-builder-', dir=output.parent) as temporary:
            scratch = Path(temporary)
            payloads = {}
            for arch in ('amd64', 'arm64'):
                check()
                destination = scratch / arch
                files, record = acquire_architecture(lock, arch, destination, check, snapshot)
                payloads.update(files)
                provenance['architectures'][arch] = record
            metadata = scratch / PROVENANCE
            metadata.write_text(json.dumps(provenance, sort_keys=True, indent=2) + '\n')
            require(metadata.stat().st_size <= accepted.MAX_METADATA, 'Provenance oversized')
            payloads[PROVENANCE] = metadata
            check()
            require(file_record(source_archive) == source_record, 'Source archive changed during staging')
            records = {source_archive.name: source_record}
            expected_files = {name: record for item in provenance['architectures'].values()
                              for name, record in item['files'].items()}
            expected_files[PROVENANCE] = file_record(metadata)
            for name, path in payloads.items():
                records[name] = file_record(path)
                require(records[name] == expected_files[name], 'Verified publication bytes changed before staging')
                # Both directories share the parent filesystem; exclusive link never overwrites.
                target = output / name
                os.link(path, target)
                created.append(target)
            checksum_file = scratch / CHECKSUMS
            checksum_file.write_text(''.join(value['sha256'] + '  ' + name + '\n'
                                             for name, value in sorted(records.items())))
            accepted.require_transport_unchanged(snapshot)
            checksum_file.replace(checksums)
            committed = True
        accepted.require_transport_unchanged(snapshot)
        return provenance
    except BaseException:
        # Checksum replacement commits the complete set. A later scratch-cleanup
        # error must propagate without deleting assets referenced by that inventory.
        if not committed:
            for path in reversed(created):
                path.unlink()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    identity = release_identity(os.environ)
    output = args.output.resolve(strict=True)
    require(shutil.disk_usage(output).free >= START_FREE, 'Insufficient space for retained release archives')
    deadline = time.monotonic() + 20 * 60
    def check():
        require(time.monotonic() < deadline, 'Builder release staging timed out')
        require(shutil.disk_usage(output).free >= RESERVE, 'Release staging space reserve reached')
    stage(output, accepted.read_lock(), identity, check)


if __name__ == '__main__':
    main()
