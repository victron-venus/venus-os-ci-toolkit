#!/usr/bin/env python3
"""Offline integrity checks for nondeployable hosted runner OCI artifacts.

No image code is executed. Raw archives and every compressed/uncompressed layer
are verified without expanding a root filesystem onto the worker disk.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile

INDEX = 'application/vnd.oci.image.index.v1+json'
MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
CONFIG = 'application/vnd.oci.image.config.v1+json'
LAYER = 'application/vnd.oci.image.layer.v1.tar'
ENTRY = ['/opt/victron-ci-reserve/start-runner.sh']
SOURCE_URL = 'https://github.com/victron-venus/venus-os-ci-toolkit'
MAX_ARCHIVE = 8 * 1024**3
MAX_UNPACKED = 20 * 1024**3
def require(value, message):
    if not value:
        raise ValueError(message)


def digest_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


class Archive:
    def __init__(self, path, expected, size):
        self.path = Path(path)
        self.stream = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb')
        try:
            info = os.fstat(self.stream.fileno())
            require(stat.S_ISREG(info.st_mode) and 0 < info.st_size == size <= MAX_ARCHIVE, 'Archive type/size changed')
            self.before = identity(info)
            self.expected = expected
            require(hashlib.file_digest(self.stream, 'sha256').hexdigest() == expected, 'Archive hash changed')
            self.stream.seek(0)
            self.tar = tarfile.open(fileobj=self.stream, mode='r:')
            self.files, names = {}, set()
            for member in self.tar:
                name = member.name.rstrip('/')
                require(str(PurePosixPath(name)) == name and not name.startswith('/') and
                        all(p not in ('', '.', '..') for p in name.split('/')) and '\\' not in name,
                        'Noncanonical OCI path')
                require(name not in names, 'Duplicate OCI member')
                names.add(name)
                if member.isdir():
                    require(name in {'blobs', 'blobs/sha256'}, 'Unexpected OCI directory')
                    continue
                require(member.isfile() and not member.issparse(), 'OCI links/special entries forbidden')
                require(name in {'index.json', 'oci-layout'} or re.fullmatch(r'blobs/sha256/[0-9a-f]{64}', name),
                        'Unexpected OCI file')
                require(0 <= member.size <= MAX_ARCHIVE and len(names) <= 5000, 'OCI inventory bound exceeded')
                self.files[name] = member
                if name.startswith('blobs/'):
                    with self.tar.extractfile(member) as stream:
                        require(hashlib.file_digest(stream, 'sha256').hexdigest() == name.rsplit('/', 1)[1], 'Blob hash mismatch')
            require({'index.json', 'oci-layout'} <= self.files.keys(), 'OCI metadata missing')
            self.used = set()
        except BaseException:
            self.stream.close()
            raise

    def read(self, name):
        member = self.files[name]
        require(member.size <= 32 * 1024**2, 'Oversized OCI JSON')
        with self.tar.extractfile(member) as stream:
            return stream.read()

    def descriptor(self, desc, expected_type=None):
        require(re.fullmatch(r'sha256:[0-9a-f]{64}', desc.get('digest', '')) and
                isinstance(desc.get('size'), int) and not desc.get('urls'), 'Invalid/remote descriptor')
        if expected_type:
            require(desc.get('mediaType') == expected_type, 'Unexpected descriptor type')
        name = 'blobs/sha256/' + desc['digest'][7:]
        require(name in self.files and self.files[name].size == desc['size'], 'Missing/wrong-size descriptor')
        self.used.add(name)
        return name

    def json_descriptor(self, desc, expected_type=None):
        return json_document(self.read(self.descriptor(desc, expected_type)))

    def unchanged(self):
        require(identity(os.fstat(self.stream.fileno())) == self.before and
                identity(self.path.stat(follow_symlinks=False)) == self.before, 'Archive identity changed')
        self.stream.seek(0)
        require(hashlib.file_digest(self.stream, 'sha256').hexdigest() == self.expected, 'Archive changed after verification')

    def close(self):
        self.tar.close()
        self.stream.close()


def config_contract(config, source, stage):
    require(config.get('os') == 'linux' and config.get('architecture') == 'amd64', 'Unexpected executable platform')
    runtime = config['config']
    require(runtime.get('User') == '1001:1001' and runtime.get('WorkingDir') == '/home/runner' and
            runtime.get('Entrypoint') == ENTRY, 'Runner identity/entrypoint changed')
    labels = runtime.get('Labels', {})
    require(labels.get('org.opencontainers.image.revision') == source and
            labels.get('org.opencontainers.image.source') == SOURCE_URL and
            labels.get('io.victron.runner.stage') == stage, 'Image source/stage labels differ')
    env = runtime.get('Env', [])
    require(len(env) == len({entry.split('=', 1)[0] for entry in env}), 'Duplicate environment key')
    for entry in ('HOME=/home/runner', 'RUNNER_TOOL_CACHE=/opt/hostedtoolcache',
                  'AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache', 'ImageOS=ubuntu24'):
        require(entry in env, 'Required runtime environment missing')
    require(not any(re.search(r'(?i)(TOKEN|PASSWORD|SECRET|PRIVATE_KEY|JITCONFIG|(^|_)PAT($|_))', entry.split('=', 1)[0]) for entry in env),
            'Credential-like runtime environment is forbidden')
    require(not runtime.get('Volumes') and not runtime.get('ExposedPorts'), 'Unexpected image runtime surface')
    return runtime



def json_document(raw):
    """Reject duplicate JSON keys instead of silently changing a descriptor."""
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def sentinel_layer(archive, descriptor, state, sentinel):
    """Track one final file through ordered layers, including ancestor whiteouts."""
    name = archive.descriptor(descriptor)
    target = 'opt/victron-ci-reserve/guard-ca.crt'
    ancestors = {'', 'opt/', 'opt/victron-ci-reserve/'}
    with archive.tar.extractfile(archive.files[name]) as raw:
        with tarfile.open(fileobj=raw, mode='r|*') as layer:
            for member in layer:
                original = member.name
                require(not original.startswith('/') and '..' not in original.split('/'),
                        'Unsafe layer path')
                # POSIX tar writers may spell the same path using repeated '/'
                # or './'. Normalize before every ancestor/whiteout comparison.
                path = '/'.join(part for part in original.split('/') if part not in {'', '.'})
                if any(path == parent + '.wh..wh..opq' for parent in ancestors):
                    state = None
                if path in {'opt/.wh.victron-ci-reserve', 'opt/victron-ci-reserve/.wh.guard-ca.crt', '.wh.opt'}:
                    state = None
                if path in {'opt', 'opt/victron-ci-reserve'}:
                    require(member.isdir(), 'Site CA parent is not a directory')
                if path == target:
                    require(member.isfile() and not member.issparse() and member.size == len(sentinel),
                            'Site CA must be the regular nondeployable sentinel')
                    state = layer.extractfile(member).read()
    return state


def verify(path, expected, size, source, stage, sentinel, base=None):
    """Bind a complete OCI image to its source, public base and invalid site CA."""
    require(stage in {'public', 'private'} and re.fullmatch(r'[0-9a-f]{40}', source), 'Invalid image identity')
    require(sentinel == b'UNCONFIGURED SITE CA\n', 'Unexpected trust sentinel')
    archive = Archive(path, expected, size)
    try:
        require(json_document(archive.read('oci-layout')) == {'imageLayoutVersion': '1.0.0'}, 'OCI layout differs')
        index = json_document(archive.read('index.json'))
        require(index.get('schemaVersion') == 2 and len(index['manifests']) == 1, 'Ambiguous OCI root')
        root = index['manifests'][0]
        image_index = archive.json_descriptor(root, INDEX)
        require(image_index.get('schemaVersion') == 2, 'Invalid image index')
        runtime, evidence, attestations, unpacked, ca = None, None, [], 0, None
        seen = set()
        for desc in image_index['manifests']:
            require(desc['digest'] not in seen, 'Repeated image descriptor')
            seen.add(desc['digest'])
            manifest = archive.json_descriptor(desc, MANIFEST)
            require(manifest.get('schemaVersion') == 2, 'Invalid manifest')
            config = archive.json_descriptor(manifest['config'], CONFIG)
            if desc.get('annotations', {}).get('vnd.docker.reference.type') == 'attestation-manifest':
                require(config.get('os') == config.get('architecture') == 'unknown', 'Attestation platform differs')
                for layer in manifest['layers']:
                    statement = archive.json_descriptor(layer, 'application/vnd.in-toto+json')
                    require(statement.get('_type') in {'https://in-toto.io/Statement/v0.1', 'https://in-toto.io/Statement/v1'},
                            'Invalid attestation')
                    attestations.append({'manifest': desc['digest'], 'subject': statement['subject'],
                                         'predicate_type': statement['predicateType']})
                continue
            require(runtime is None and desc.get('platform') == {'architecture': 'amd64', 'os': 'linux'},
                    'Expected exactly one Linux amd64 runtime')
            runtime = config_contract(config, source, stage)
            layers = manifest['layers']
            require(config['rootfs']['type'] == 'layers' and len(config['rootfs']['diff_ids']) == len(layers),
                    'Layer/rootfs mismatch')
            for layer, expected_diff in zip(layers, config['rootfs']['diff_ids']):
                name = archive.descriptor(layer)
                require(layer['mediaType'] in {LAYER, LAYER+'+gzip'}, 'Unsupported compression')
                with archive.tar.extractfile(archive.files[name]) as raw:
                    stream = gzip.GzipFile(fileobj=raw) if layer['mediaType'].endswith('+gzip') else raw
                    digest = hashlib.sha256()
                    while block := stream.read(1024**2):
                        unpacked += len(block)
                        require(unpacked <= MAX_UNPACKED, 'Uncompressed bound exceeded')
                        digest.update(block)
                    require(expected_diff == 'sha256:' + digest.hexdigest(), 'Uncompressed digest mismatch')
                ca = sentinel_layer(archive, layer, ca, sentinel)
            evidence = {'manifest': desc['digest'], 'config': manifest['config']['digest'],
                        'layers': layers, 'diff_ids': config['rootfs']['diff_ids'], 'runtime_config': runtime}
        require(runtime is not None and ca == sentinel, 'Unconfigured site CA sentinel is missing')
        subjects = {subject['digest'].get('sha256') for statement in attestations
                    if statement['predicate_type'].startswith('https://slsa.dev/provenance/')
                    for subject in statement['subject']}
        require(evidence['manifest'][7:] in subjects, 'Build provenance subject missing')
        require(archive.used == set(archive.files) - {'index.json', 'oci-layout'}, 'Unreferenced OCI blobs')
        if base:
            require(base['stage'] == 'public' and base['source_commit'] == source, 'Public base source differs')
            count = len(base['platform']['layers'])
            require(evidence['layers'][:count] == base['platform']['layers'] and
                    evidence['diff_ids'][:count] == base['platform']['diff_ids'], 'Accepted public layers changed')
            before, after = dict(base['platform']['runtime_config']), dict(runtime)
            for item in (before, after):
                item.pop('Labels', None)
            require(before == after, 'Private runtime configuration changed')
        archive.unchanged()
        return {'schema': 1, 'stage': stage, 'source_commit': source,
                'archive_sha256': expected, 'archive_bytes': size, 'image_index': root['digest'],
                'platform': evidence, 'attestations': attestations, 'verified_blobs': len(archive.used),
                'uncompressed_bytes': unpacked, 'site_configured': False, 'deployable': False,
                'sentinel_sha256': hashlib.sha256(sentinel).hexdigest(),
                'claim': 'OCI integrity and source labels; no signature, vulnerability or runtime qualification.'}
    finally:
        archive.close()


def materialize(path, proof, destination, check):
    """Copy only verified OCI blobs; never extract an image root filesystem."""
    destination.mkdir(mode=0o700, exist_ok=False)
    archive = Archive(path, proof['archive_sha256'], proof['archive_bytes'])
    try:
        for name, member in archive.files.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.tar.extractfile(member) as source, target.open('xb') as output:
                while block := source.read(1024**2):
                    check()
                    output.write(block)
        archive.unchanged()
    finally:
        archive.close()
