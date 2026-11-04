"""Consume immutable, qualified builder bytes on a disposable hosted worker."""

import datetime as dt
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import stat
import subprocess
import time
import zipfile

if __package__:
    from . import oci_builder_image as image
    from . import runner_image_oci as oci
else:
    import oci_builder_image as image
    import runner_image_oci as oci

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / '.github/oci-tools/accepted-builder.json'
REPOSITORY = 'victron-venus/venus-os-ci-toolkit'
WORKFLOW = '.github/workflows/oci-builder-qualification.yml'
MAX_IMAGE_ZIP = 768 * 1024**2
MAX_METADATA = 2 * 1024**2
MAX_EVIDENCE = 32 * 1024**2
SHA = r'[0-9a-f]{64}'
SHA_PREFIX = 'sha256:'
OCI_ARCHIVE = 'builder.oci.tar'
DOCKER_ARCHIVE = 'builder.docker.tar'
require = oci.require


def validate_lock(lock):
    require(lock.get('schema') == 1 and lock.get('repository') == REPOSITORY and
            lock.get('workflow') == WORKFLOW and lock.get('attempt') == 1,
            'Unexpected accepted builder contract')
    require(re.fullmatch(r'[0-9a-f]{40}', lock.get('source', '')) and
            type(lock.get('qualification_run')) is int and lock['qualification_run'] > 0,
            'Exact accepted builder source and run required')
    require(set(lock.get('architectures', {})) == {'amd64', 'arm64'}, 'Both native inputs required')
    for arch, value in lock['architectures'].items():
        for key in ('config', 'manifest'):
            require(re.fullmatch(SHA_PREFIX + SHA, value[key]), 'Invalid accepted image digest')
        require(value['embedded_import_tag'] == f"local/oci-proof:{lock['qualification_run']}-{arch}",
                'Unexpected embedded import tag')
        for kind, limit in (('image', MAX_IMAGE_ZIP), ('evidence', 8 * 1024**2)):
            artifact = value['artifacts'][kind]
            require(type(artifact['id']) is int and artifact['id'] > 0 and
                    artifact['name'] == f'oci-builder-{kind}-{arch}' and
                    re.fullmatch(SHA_PREFIX + SHA, artifact['digest']) and
                    type(artifact['size_in_bytes']) is int and 0 < artifact['size_in_bytes'] <= limit,
                    'Invalid accepted artifact')
        for kind in ('oci', 'docker'):
            require(type(value[kind]['archive_bytes']) is int and
                    0 < value[kind]['archive_bytes'] <= image.MAX_ROOTFS and
                    re.fullmatch(SHA, value[kind]['archive_sha256']), 'Invalid inner archive')
        files = value['oci']['files']
        require({'usr/bin/buildkitd', 'usr/bin/buildctl', 'usr/bin/buildkit-runc',
                 'usr/bin/oci-cgroup-reader'} <= set(files), 'Accepted binary inventory missing')
        require(any(n.startswith('usr/share/licenses/oci-tools/') for n in files), 'Notices missing')
        require(all(re.fullmatch(SHA, v) for v in files.values()), 'Invalid file hash')


def read_lock():
    raw = LOCK.read_bytes()
    require(len(raw) <= MAX_METADATA, 'Accepted lock oversized')
    value = oci.json_document(raw)
    validate_lock(value)
    return value


def validate_run(lock, run):
    require(run.get('id') == lock['qualification_run'] and run.get('head_sha') == lock['source'] and
            run.get('run_attempt') == lock['attempt'] and run.get('event') == 'workflow_dispatch' and
            run.get('status') == 'completed' and run.get('conclusion') == 'success' and
            run.get('head_branch') == 'main' and run.get('path') == WORKFLOW and
            run.get('repository', {}).get('full_name') == REPOSITORY and
            run.get('head_repository', {}).get('full_name') == REPOSITORY,
            'Accepted qualification provenance differs')


def validate_artifact(lock, expected, actual):
    require(all(actual.get(k) == expected[k] for k in ('id', 'name', 'size_in_bytes', 'digest')) and
            actual.get('expired') is False and
            actual.get('workflow_run', {}).get('id') == lock['qualification_run'] and
            actual.get('workflow_run', {}).get('head_sha') == lock['source'],
            'Artifact expired, changed or belongs to another source/run')
    expiry = dt.datetime.fromisoformat(actual['expires_at'].replace('Z', '+00:00'))
    require(expiry > dt.datetime.now(dt.timezone.utc), 'Accepted artifact has expired')


def stream_api(path, output, maximum, check):
    """Bound both time and bytes; never buffer an image or expose auth in argv."""
    require(re.fullmatch(r'actions/(runs|artifacts)/\d+(?:/zip)?', path, flags=re.ASCII), 'Unexpected API path')
    process = subprocess.Popen(['gh', 'api', f'repos/{REPOSITORY}/{path}'],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size, digest = 0, hashlib.sha256()
    deadline = time.monotonic() + 180
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                check()
                remaining = deadline - time.monotonic()
                require(remaining > 0, 'Artifact request timed out')
                if not selector.select(min(remaining, 5)):
                    continue
                block = os.read(process.stdout.fileno(), 1024**2)
                if not block:
                    break
                size += len(block)
                require(size <= maximum, 'Artifact response exceeds byte limit')
                output.write(block)
                digest.update(block)
        require(process.wait(timeout=10) == 0, 'Artifact request failed')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stdout.close()
    return size, digest.hexdigest()


def api(path, check):
    output = io.BytesIO()
    stream_api(path, output, MAX_METADATA, check)
    return oci.json_document(output.getvalue())


def download(lock, expected, target, check):
    validate_artifact(lock, expected, api(f"actions/artifacts/{expected['id']}", check))
    with target.open('xb') as output:
        size, digest = stream_api(f"actions/artifacts/{expected['id']}/zip", output,
                                  expected['size_in_bytes'], check)
    require(size == expected['size_in_bytes'] and SHA_PREFIX + digest == expected['digest'],
            'Downloaded artifact bytes differ')


def members(archive, expected_names, maximum):
    entries = archive.infolist()
    require(len(entries) == len(expected_names) and {e.filename for e in entries} == set(expected_names),
            'Unexpected or duplicate ZIP member')
    require(sum(e.file_size for e in entries) <= maximum, 'ZIP expansion exceeds limit')
    for entry in entries:
        name = entry.filename
        mode = entry.external_attr >> 16
        require(name == str(PurePosixPath(name)) and not name.startswith('/') and
                '\\' not in name and '..' not in PurePosixPath(name).parts and
                not entry.is_dir() and not entry.flag_bits & 1 and
                stat.S_IFMT(mode) in (0, stat.S_IFREG), 'Unsafe ZIP member')
    return entries


def unpack_images(path, destination, expected, check):
    wanted = {OCI_ARCHIVE: expected['oci'], DOCKER_ARCHIVE: expected['docker']}
    maximum = sum(v['archive_bytes'] for v in wanted.values())
    with zipfile.ZipFile(path) as archive:
        entries = members(archive, wanted, maximum)
        for entry in entries:
            spec = wanted[entry.filename]
            require(entry.file_size == spec['archive_bytes'], 'Inner archive size differs')
            size, digest = 0, hashlib.sha256()
            with archive.open(entry) as source, (destination / entry.filename).open('xb') as output:
                while block := source.read(1024**2):
                    check()
                    size += len(block)
                    require(size <= spec['archive_bytes'], 'Inner archive grew')
                    output.write(block)
                    digest.update(block)
            require(size == spec['archive_bytes'] and digest.hexdigest() == spec['archive_sha256'],
                    'Inner archive digest differs')


def verify_evidence(path, lock, arch):
    spec = lock['architectures'][arch]
    with zipfile.ZipFile(path) as archive:
        entries = members(archive, spec['evidence_members'], MAX_EVIDENCE)
        documents = {}
        for entry in entries:
            require(entry.file_size <= MAX_EVIDENCE, 'Evidence member oversized')
            raw = archive.read(entry)
            require(hashlib.sha256(raw).hexdigest() == spec['evidence_members'][entry.filename],
                    'Qualification evidence changed')
            if entry.filename == f'oci-builder-{arch}/evidence/acceptance.json':
                documents['acceptance'] = oci.json_document(raw)
        proof = documents['acceptance']
        require(proof['source'] == lock['source'] and proof['architecture'] == arch and
                proof['qualified'] is True and proof['runtime_passed'] is True and
                proof['oci']['config'] == spec['config'] and
                proof['oci']['manifest'] == spec['manifest'], 'Qualification acceptance differs')
    return proof


def prepare(lock, arch, destination, check):
    validate_lock(lock)
    require(arch in lock['architectures'], 'Native builder architecture unsupported')
    validate_run(lock, api(f"actions/runs/{lock['qualification_run']}", check))
    destination.mkdir(mode=0o700, exist_ok=False)
    spec = lock['architectures'][arch]
    for kind in ('evidence', 'image'):
        download(lock, spec['artifacts'][kind], destination / (kind + '.zip'), check)
    verify_evidence(destination / 'evidence.zip', lock, arch)
    unpack_images(destination / 'image.zip', destination, spec, check)
    (destination / 'image.zip').unlink()
    proof = image.verify_oci(destination / OCI_ARCHIVE, arch, spec['oci']['files'], check)
    require(all(proof[k] == spec[k] for k in ('manifest', 'config')) and
            proof['diff_ids'] == spec['oci']['diff_ids'], 'Verified builder identity differs')
    image.verify_docker_export(destination / DOCKER_ARCHIVE, proof, spec['embedded_import_tag'])
    oci.materialize(destination / OCI_ARCHIVE, proof, destination / 'layout', check)
    (destination / OCI_ARCHIVE).unlink()
    return {'source': lock['source'], 'qualification_run': lock['qualification_run'],
            'attempt': lock['attempt'], 'architecture': arch,
            'lock_sha256': oci.digest_file(LOCK), 'config': spec['config'], 'manifest': spec['manifest'],
            'artifacts': spec['artifacts'], 'files': proof['files'], 'diff_ids': proof['diff_ids']}


def load(supervisor, destination, spec, record):
    image.begin_import(supervisor, spec['embedded_import_tag'], destination / DOCKER_ARCHIVE, record)
    actual = image.inspect_image(supervisor, spec['config'])
    image.validate_image(actual, record['architecture'], spec['config'])
    require(actual['RootFS']['Layers'] == spec['oci']['diff_ids'], 'Imported rootfs differs')
    record['import_verified'] = True


def cleanup(supervisor, spec, record):
    if record.get('import_started'):
        record['import_cleanup'] = image.remove_import(supervisor, spec['embedded_import_tag'], spec['config'],
            tag_absent_before_load=record.get('import_tag_absent_before_load'))


def check_receipt(record, lock, arch):
    spec = lock['architectures'][arch]
    require(all(record.get(k) == v for k, v in {
        'source': lock['source'], 'qualification_run': lock['qualification_run'], 'attempt': lock['attempt'],
        'architecture': arch, 'lock_sha256': oci.digest_file(LOCK), 'config': spec['config'],
        'manifest': spec['manifest'], 'artifacts': spec['artifacts'], 'files': spec['oci']['files'],
        'diff_ids': spec['oci']['diff_ids'], 'import_verified': True,
        'import_tag_absent_before_load': True, 'import_started': True}.items()),
        'Accepted builder consumer receipt differs')
    cleanup_result = record.get('import_cleanup', {})
    require(cleanup_result.get('absent') is True or cleanup_result == {
        'removed': True, 'image': spec['config'], 'tag': spec['embedded_import_tag']},
        'Accepted builder import cleanup missing')
