"""Consume immutable, qualified builder bytes on a disposable hosted worker."""

import datetime as dt
from dataclasses import dataclass
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
TRANSPORT = ROOT / '.github/oci-tools/accepted-builder-transport.json'
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


def stream_api(path, output, maximum, check, *, media=False):
    """Bound both time and bytes; never buffer an image or expose auth in argv."""
    allowed = (r'actions/(runs|artifacts)/\d+(?:/zip)?|releases/(?:assets/)?\d+|'
               r'git/ref/tags/v\d+\.\d+\.\d+')
    require(re.fullmatch(allowed, path, flags=re.ASCII), 'Unexpected API path')
    require(not media or re.fullmatch(r'releases/assets/\d+', path, flags=re.ASCII),
            'Binary media is only allowed for a pinned release asset')
    command = ['gh', 'api', f'repos/{REPOSITORY}/{path}']
    if media:
        command += ['--header', 'Accept: application/octet-stream']
    process = subprocess.Popen(command,
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


@dataclass(frozen=True)
class TransportSnapshot:
    """Immutable original bytes; each selection is parsed from that same input."""

    raw: bytes

    @property
    def selection(self):
        return oci.json_document(self.raw)

    def receipt(self):
        return {'lock_sha256': hashlib.sha256(self.raw).hexdigest(), 'selection': self.selection}


def read_transport():
    raw = TRANSPORT.read_bytes()
    require(len(raw) <= MAX_METADATA, 'Transport lock oversized')
    value = oci.json_document(raw)
    require(value.get('schema') == 1 and value.get('kind') in {'actions', 'release'},
            'Explicit builder transport required')
    return TransportSnapshot(raw)


def transport_receipt(snapshot=None):
    return (snapshot if snapshot is not None else read_transport()).receipt()


def require_transport_unchanged(snapshot):
    require(read_transport().raw == snapshot.raw, 'Builder transport changed during operation')


def release_asset_spec(value, name, size=None, digest=None):
    require(type(value.get('id')) is int and value['id'] > 0 and value.get('name') == name and
            type(value.get('size')) is int and 0 < value['size'] <= image.MAX_ROOTFS and
            re.fullmatch(SHA_PREFIX + SHA, value.get('digest', '')), 'Invalid pinned release asset')
    require(size is None or value['size'] == size, 'Release asset differs from original archive size')
    require(digest is None or value['digest'] == digest, 'Release asset differs from original archive hash')


def validate_release_transport(transport, lock):
    require(transport.get('repository') == REPOSITORY and type(transport.get('release_id')) is int and
            transport['release_id'] > 0 and re.fullmatch(r'v\d+\.\d+\.\d+', transport.get('tag', ''), re.ASCII) and
            re.fullmatch(r'[0-9a-f]{40}', transport.get('source', '')), 'Exact stable release identity required')
    require(set(transport.get('architectures', {})) == {'amd64', 'arm64'}, 'Both release architectures required')
    release_asset_spec(transport['provenance'], 'accepted-builder-provenance.json')
    require(transport['provenance']['size'] <= MAX_METADATA, 'Release provenance oversized')
    ids = [transport['provenance']['id']]
    for arch, files in transport['architectures'].items():
        require(set(files) == {'oci', 'docker', 'evidence'}, 'Original release asset inventory required')
        spec = lock['architectures'][arch]
        for kind, item in files.items():
            if kind == 'evidence':
                name = f'oci-builder-linux-{arch}.evidence.zip'
                size, digest = spec['artifacts']['evidence']['size_in_bytes'], spec['artifacts']['evidence']['digest']
            else:
                name = f'oci-builder-linux-{arch}.{kind}.tar'
                size, digest = spec[kind]['archive_bytes'], SHA_PREFIX + spec[kind]['archive_sha256']
            release_asset_spec(item, name, size, digest)
            ids.append(item['id'])
    require(len(ids) == len(set(ids)), 'Release asset IDs must be unique')


def release_metadata(transport, check):
    release = api(f"releases/{transport['release_id']}", check)
    require(release.get('id') == transport['release_id'] and release.get('tag_name') == transport['tag'] and
            release.get('draft') is False and release.get('prerelease') is False and release.get('published_at'),
            'Pinned published stable release differs')
    ref = api('git/ref/tags/' + transport['tag'], check)
    require(ref.get('object') == {'type': 'commit', 'sha': transport['source'],
            'url': f"https://api.github.com/repos/{REPOSITORY}/git/commits/{transport['source']}"},
            'Stable tag does not identify the pinned source commit')
    assets = release.get('assets', [])
    require(0 < len(assets) <= 100 and len({a['id'] for a in assets}) == len(assets),
            'Ambiguous release asset ownership')
    return {item['id']: item for item in assets}


def release_download(spec, assets, target, check):
    require(spec['id'] in assets, 'Asset is not owned by the pinned release')
    actual = api(f"releases/assets/{spec['id']}", check)
    for item in (assets[spec['id']], actual):
        require(item.get('state') == 'uploaded' and all(item.get(k) == v for k, v in spec.items()),
                'Pinned release asset metadata changed')
    with target.open('xb') as output:
        size, digest = stream_api(f"releases/assets/{spec['id']}", output, spec['size'], check, media=True)
    require(size == spec['size'] and SHA_PREFIX + digest == spec['digest'], 'Retained release bytes differ')


def acquire_release(transport, lock, arch, destination, check):
    validate_release_transport(transport, lock)
    assets = release_metadata(transport, check)
    provenance = destination / 'release-provenance.json'
    release_download(transport['provenance'], assets, provenance, check)
    proof = oci.json_document(provenance.read_bytes())
    require(proof.get('repository') == REPOSITORY and proof.get('lock_sha256') == oci.digest_file(LOCK) and
            proof.get('qualification') == {'source': lock['source'], 'run': lock['qualification_run'],
                                          'attempt': lock['attempt'], 'workflow': lock['workflow']} and
            proof.get('release', {}).get('source') == transport['source'], 'Retained provenance differs')
    for kind, filename in [('oci', OCI_ARCHIVE), ('docker', DOCKER_ARCHIVE), ('evidence', 'evidence.zip')]:
        spec = transport['architectures'][arch][kind]
        require(proof['architectures'][arch]['files'].get(spec['name']) ==
                {'sha256': spec['digest'][7:], 'bytes': spec['size']}, 'Retained provenance asset differs')
        release_download(spec, assets, destination / filename, check)


def acquire(lock, arch, destination, check, *, snapshot=None):
    """Verify original archives and evidence without importing or materializing them."""
    validate_lock(lock)
    require(arch in lock['architectures'], 'Native builder architecture unsupported')
    snapshot = snapshot if snapshot is not None else read_transport()
    require_transport_unchanged(snapshot)
    transport = snapshot.selection
    destination.mkdir(mode=0o700, exist_ok=False)
    spec = lock['architectures'][arch]
    if transport['kind'] == 'release':
        acquire_release(transport, lock, arch, destination, check)
    else:
        validate_run(lock, api(f"actions/runs/{lock['qualification_run']}", check))
        for kind in ('evidence', 'image'):
            download(lock, spec['artifacts'][kind], destination / (kind + '.zip'), check)
    verify_evidence(destination / 'evidence.zip', lock, arch)
    if transport['kind'] == 'actions':
        unpack_images(destination / 'image.zip', destination, spec, check)
        (destination / 'image.zip').unlink()
    proof = image.verify_oci(destination / OCI_ARCHIVE, arch, spec['oci']['files'], check)
    require(all(proof[k] == spec[k] for k in ('manifest', 'config')) and
            proof['diff_ids'] == spec['oci']['diff_ids'], 'Verified builder identity differs')
    image.verify_docker_export(destination / DOCKER_ARCHIVE, proof, spec['embedded_import_tag'])
    require_transport_unchanged(snapshot)
    return proof


def prepare(lock, arch, destination, check):
    snapshot = read_transport()
    proof = acquire(lock, arch, destination, check, snapshot=snapshot)
    spec = lock['architectures'][arch]
    oci.materialize(destination / OCI_ARCHIVE, proof, destination / 'layout', check)
    (destination / OCI_ARCHIVE).unlink()
    require_transport_unchanged(snapshot)
    return {'source': lock['source'], 'qualification_run': lock['qualification_run'],
            'attempt': lock['attempt'], 'architecture': arch,
            'lock_sha256': oci.digest_file(LOCK), 'config': spec['config'], 'manifest': spec['manifest'],
            'artifacts': spec['artifacts'], 'files': proof['files'], 'diff_ids': proof['diff_ids'],
            'transport': transport_receipt(snapshot)}


def load(supervisor, destination, spec, record):
    require(record.get('transport') == transport_receipt(), 'Builder transport changed before import')
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
        'transport': transport_receipt(),
        'diff_ids': spec['oci']['diff_ids'], 'import_verified': True,
        'import_tag_absent_before_load': True, 'import_started': True}.items()),
        'Accepted builder consumer receipt differs')
    cleanup_result = record.get('import_cleanup', {})
    require(cleanup_result.get('absent') is True or cleanup_result == {
        'removed': True, 'image': spec['config'], 'tag': spec['embedded_import_tag']},
        'Accepted builder import cleanup missing')
