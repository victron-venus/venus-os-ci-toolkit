#!/usr/bin/env python3
"""Offline integrity checks for nondeployable hosted runner OCI artifacts.

No image code is executed. Raw archives and every compressed/uncompressed layer
are verified without expanding a root filesystem onto the worker disk.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile

INDEX = 'application/vnd.oci.image.index.v1+json'
MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
CONFIG = 'application/vnd.oci.image.config.v1+json'
EMPTY_CONFIG = 'application/vnd.oci.empty.v1+json'
EMPTY_CONFIG_DIGEST = 'sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'
ATTESTATION = 'application/vnd.docker.attestation.manifest.v1+json'
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
        seen, artifact_subjects = set(), []
        runtime_descriptor = None
        for desc in image_index['manifests']:
            require(desc['digest'] not in seen, 'Repeated image descriptor')
            seen.add(desc['digest'])
            manifest = archive.json_descriptor(desc, MANIFEST)
            require(manifest.get('schemaVersion') == 2, 'Invalid manifest')
            if desc.get('annotations', {}).get('vnd.docker.reference.type') == 'attestation-manifest':
                config_desc = manifest['config']
                if config_desc.get('mediaType') == EMPTY_CONFIG:
                    require(manifest.get('artifactType') == ATTESTATION, 'Unsupported attestation artifact type')
                    require(desc.get('platform') == {'architecture': 'unknown', 'os': 'unknown'},
                            'Attestation platform differs')
                    require(config_desc.get('digest') == EMPTY_CONFIG_DIGEST and
                            type(config_desc.get('size')) is int and config_desc['size'] == 2 and
                            ('data' not in config_desc or config_desc['data'] == 'e30='),
                            'Invalid empty attestation config')
                    require(archive.read(archive.descriptor(config_desc, EMPTY_CONFIG)) == b'{}',
                            'Invalid empty attestation config')
                    subject = manifest.get('subject')
                    require(isinstance(subject, dict), 'Attestation subject missing')
                    archive.descriptor(subject, MANIFEST)
                    require(desc['annotations'].get('vnd.docker.reference.digest') == subject['digest'],
                            'Attestation reference differs')
                    artifact_subjects.append({key: subject[key] for key in ('mediaType', 'digest', 'size')})
                else:
                    require('artifactType' not in manifest, 'Unsupported attestation artifact type')
                    config = archive.json_descriptor(config_desc, CONFIG)
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
            config = archive.json_descriptor(manifest['config'], CONFIG)
            runtime = config_contract(config, source, stage)
            runtime_descriptor = {key: desc[key] for key in ('mediaType', 'digest', 'size')}
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
        require(all(subject == runtime_descriptor for subject in artifact_subjects), 'Attestation subject differs')
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


def scan_index(runtime):
    """Trivy consumes an OCI directory; select the verified runtime explicitly."""
    return json.dumps({'schemaVersion': 2, 'mediaType': INDEX, 'manifests': [runtime]},
                      sort_keys=True, separators=(',', ':')).encode() + b'\n'


def check_scan_input(selection, proof):
    require(selection.get('schema') == 1 and
            selection.get('archive_sha256') == proof['archive_sha256'] and
            selection.get('image_index') == proof['image_index'], 'Scan archive binding differs')
    runtime, config = selection['runtime'], selection['config']
    require(set(runtime) == {'mediaType', 'digest', 'size', 'platform'} and
            runtime['mediaType'] == MANIFEST and runtime['digest'] == proof['platform']['manifest'] and
            runtime['platform'] == {'architecture': 'amd64', 'os': 'linux'}, 'Scan runtime differs')
    require(config['mediaType'] == CONFIG and config['digest'] == proof['platform']['config'] and
            selection['layers'] == proof['platform']['layers'], 'Scan configuration or layers differ')
    files = selection['files']
    expected = {'index.json', 'oci-layout'}
    for desc in [runtime, config, *selection['layers']]:
        require(re.fullmatch(r'sha256:[0-9a-f]{64}', desc['digest']) and
                type(desc['size']) is int and 0 < desc['size'] <= MAX_ARCHIVE, 'Invalid scan descriptor')
        name = 'blobs/sha256/' + desc['digest'][7:]
        expected.add(name)
        require(files.get(name) == {'sha256': desc['digest'][7:], 'bytes': desc['size']},
                'Scan blob binding differs')
    raw = scan_index(runtime)
    require(files.get('index.json') == {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)} and
            set(files) == expected, 'Scan inventory differs')


def verify_scan_input(destination, selection, proof, check):
    """Rehash the exact scanner directory before/after use; no image code runs."""
    check_scan_input(selection, proof)
    require(destination.is_dir() and not destination.is_symlink(), 'Unsafe scan directory')
    actual = set()
    for path in destination.rglob('*'):
        require(not path.is_symlink(), 'Scan input contains a symlink')
        if path.is_dir():
            require(path.relative_to(destination).as_posix() in {'blobs', 'blobs/sha256'},
                    'Unexpected scan directory')
            continue
        name = path.relative_to(destination).as_posix()
        require(name in selection['files'], 'Unexpected scan file')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(fd)
            require(stat.S_ISREG(before.st_mode) and before.st_size == selection['files'][name]['bytes'],
                    'Scan file type or size differs')
            digest = hashlib.sha256()
            while block := stream.read(1024**2):
                check()
                digest.update(block)
            require(identity(os.fstat(fd)) == identity(before) == identity(path.stat(follow_symlinks=False)) and
                    digest.hexdigest() == selection['files'][name]['sha256'], 'Scan bytes changed')
        actual.add(name)
    require(actual == set(selection['files']), 'Scan files missing')
    require(json_document((destination / 'oci-layout').read_bytes()) == {'imageLayoutVersion': '1.0.0'},
            'Scan OCI version differs')
    check()


def project_scan_input(path, proof, destination, check):
    """Copy unchanged runtime blobs from the accepted archive, omitting attestations."""
    destination.mkdir(mode=0o700, exist_ok=False)
    archive = Archive(path, proof['archive_sha256'], proof['archive_bytes'])
    try:
        outer = json_document(archive.read('index.json'))
        require(outer.get('schemaVersion') == 2 and len(outer.get('manifests', [])) == 1,
                'Ambiguous scan archive root')
        root = outer['manifests'][0]
        require(root['digest'] == proof['image_index'], 'Scan source index differs')
        index = archive.json_descriptor(root, INDEX)
        require(index.get('schemaVersion') == 2, 'Invalid scan source index')
        matches = [d for d in index['manifests'] if d['digest'] == proof['platform']['manifest']]
        require(len(matches) == 1, 'Scan runtime is missing or duplicated')
        runtime = matches[0]
        require(runtime.get('platform') == {'architecture': 'amd64', 'os': 'linux'} and
                runtime.get('annotations', {}).get('vnd.docker.reference.type') != 'attestation-manifest',
                'Scan target is not the verified runtime')
        manifest = archive.json_descriptor(runtime, MANIFEST)
        require(manifest.get('schemaVersion') == 2 and manifest['config']['digest'] == proof['platform']['config'] and
                manifest['layers'] == proof['platform']['layers'], 'Scan manifest differs')
        config = archive.json_descriptor(manifest['config'], CONFIG)
        require(config_contract(config, proof['source_commit'], proof['stage']) == proof['platform']['runtime_config'] and
                config['rootfs'] == {'type': 'layers', 'diff_ids': proof['platform']['diff_ids']},
                'Scan runtime configuration differs')
        runtime = {key: runtime[key] for key in ('mediaType', 'digest', 'size', 'platform')}
        selection = {'schema': 1, 'archive_sha256': proof['archive_sha256'], 'image_index': proof['image_index'],
                     'runtime': runtime, 'config': manifest['config'], 'layers': manifest['layers'], 'files': {}}
        (destination / 'blobs/sha256').mkdir(mode=0o700, parents=True)
        for desc in [runtime, manifest['config'], *manifest['layers']]:
            name = archive.descriptor(desc)
            if name in selection['files']:
                continue
            fd = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            digest, count = hashlib.sha256(), 0
            with os.fdopen(fd, 'wb') as output, archive.tar.extractfile(archive.files[name]) as source:
                while block := source.read(1024**2):
                    check()
                    count += len(block)
                    require(count <= desc['size'], 'Scan copy exceeds descriptor')
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(fd)
            require(count == desc['size'] and digest.hexdigest() == desc['digest'][7:], 'Scan copy differs')
            selection['files'][name] = {'sha256': digest.hexdigest(), 'bytes': count}
        for name, raw in [('index.json', scan_index(runtime)), ('oci-layout', archive.read('oci-layout'))]:
            fd = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(raw)
            selection['files'][name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
        archive.unchanged()
        verify_scan_input(destination, selection, proof, check)
        return selection
    finally:
        archive.close()


def effective_path(value):
    """Use one POSIX spelling for layer entries; never resolve host paths."""
    require(isinstance(value, str) and len(value) <= 4096 and not value.startswith('/') and '\\' not in value and
            '..' not in value.split('/'), 'Unsafe effective file path')
    result = '/'.join(part for part in value.split('/') if part not in {'', '.'})
    require(result, 'Empty effective file path')
    return result


def merge_effective_layer(previous, entries):
    """Apply whiteouts to lower layers, then overlay this layer's entries."""
    result = dict(previous)
    # Hardlink headers can change metadata of a shared inode. This reader does
    # not model those inode aliases: retain ordinary unrelated links, but never
    # use either endpoint as direct regular-file evidence, even after overwrite.
    ambiguous = {path for path, record in previous.items() if record.get('hardlink_ambiguous')}
    for records in (previous, entries):
        for path, record in records.items():
            if record['type'] == 'hardlink':
                ambiguous.update((path, record['target']))
    for path in entries:
        parent, _, name = path.rpartition('/')
        if name.startswith('.wh.'):
            for ancestor in PurePosixPath(path).parents:
                if str(ancestor) in result:
                    require(result[str(ancestor)]['type'] == 'directory',
                            'Whiteout has a non-directory ancestor')
        if name == '.wh..wh..opq':
            result = {p: r for p, r in result.items() if parent and not p.startswith(parent + '/')}
        elif name.startswith('.wh.'):
            target = (parent + '/' if parent else '') + name[4:]
            require(name != '.wh.', 'Empty whiteout target')
            result = {p: r for p, r in result.items() if p != target and not p.startswith(target + '/')}
    # Most additions are ordinary files. Avoid scanning the complete tree for
    # each file; only an existing ancestor can have descendants to remove.
    ancestors = {str(parent) for path in result for parent in PurePosixPath(path).parents}
    for path, record in entries.items():
        if PurePosixPath(path).name.startswith('.wh.'):
            continue
        # Validate at the moment of application, not only against the eventual
        # tree: a later directory entry cannot legitimize an earlier child.
        for candidate in (path, record['target']) if record['type'] == 'hardlink' else (path,):
            for parent in PurePosixPath(candidate).parents:
                if str(parent) in result:
                    require(result[str(parent)]['type'] == 'directory',
                            'Layer entry has a non-directory ancestor at application')
        if record['type'] != 'directory' and path in ancestors:
            result = {p: r for p, r in result.items() if not p.startswith(path + '/')}
        result[path] = record
        ancestors.update(str(parent) for parent in PurePosixPath(path).parents)
    for path in ambiguous & result.keys():
        result[path] = {**result[path], 'hardlink_ambiguous': True}
    return result


def effective_regular(tree, path):
    """Selected evidence must be direct regular bytes, never a link target."""
    require(path == effective_path(path), 'Noncanonical selected path')
    require(path in tree and tree[path]['type'] == 'regular', 'Selected file is not regular')
    require(not tree[path].get('hardlink_ambiguous'), 'Selected file has an unmodelled hardlink alias')
    for parent in PurePosixPath(path).parents:
        if str(parent) != '.' and str(parent) in tree:
            require(tree[str(parent)]['type'] == 'directory', 'Selected file has a non-directory ancestor')
    return tree[path]


def effective_text(tree, path):
    record = effective_regular(tree, path)
    require(isinstance(record.get('text'), str), 'Effective file text missing')
    raw = record['text'].encode('utf-8')
    require(len(raw) == record['bytes'] and hashlib.sha256(raw).hexdigest() == record['sha256'],
            'Effective text does not match recorded file bytes')
    return record['text']


def package_files(effective, package, architecture, version, allowed_symlinks=None):
    """Bind installed package metadata to its final regular files and directories.

    This does not determine vulnerability applicability. In particular, an
    installed package name or version alone is never evidence of a fixed file.
    """
    require(re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', package) and
            architecture in {'amd64', 'arm64'}, 'Invalid package evidence identity')
    tree = effective['files']
    allowed_symlinks = allowed_symlinks or {}
    status = effective_text(tree, 'var/lib/dpkg/status')
    stanzas = [text for text in status.split('\n\n') if text.startswith('Package: ' + package + '\n')]
    require(len(stanzas) == 1, 'Package status missing or ambiguous')
    fields = {}
    for line in stanzas[0].splitlines():
        if line.startswith((' ', '\t')):
            continue
        name, separator, value = line.partition(': ')
        require(separator and name not in fields, 'Invalid or duplicate package status field')
        fields[name] = value
    require(fields.get('Status') == 'install ok installed' and fields.get('Version') == version and
            fields.get('Architecture') == architecture, 'Installed package identity differs')
    prefix = 'var/lib/dpkg/info/' + package + ':' + architecture
    filelist = effective_regular(tree, prefix + '.list')
    md5sums = effective_regular(tree, prefix + '.md5sums')
    files_text = effective_text(tree, prefix + '.list')
    sums_text = effective_text(tree, prefix + '.md5sums')
    inventory = {}
    for line in files_text.splitlines():
        if line == '/.':
            continue
        require(line.startswith('/') and line == '/' + effective_path(line[1:]), 'Noncanonical package file path')
        path = line[1:]
        require(path not in inventory and path in tree, 'Duplicate or missing package file')
        record = tree[path]
        require(not record.get('hardlink_ambiguous'), 'Package file has an unmodelled hardlink alias')
        require(record['type'] in {'regular', 'directory'} or
                (record['type'] == 'symlink' and allowed_symlinks.get(path) == record.get('target')),
                'Package evidence contains an unapproved link or special file')
        for parent in PurePosixPath(path).parents:
            if str(parent) != '.' and str(parent) in tree:
                require(tree[str(parent)]['type'] == 'directory', 'Package file has a non-directory ancestor')
        inventory[path] = {k: v for k, v in record.items() if k not in {'text', 'layer'}}
    require(inventory, 'Package file inventory is empty')
    hashes = {}
    for line in sums_text.splitlines():
        parts = line.split(None, 1)
        require(len(parts) == 2 and re.fullmatch(r'[0-9a-f]{32}', parts[0]), 'Invalid package MD5 entry')
        digest, path = parts
        require(path == effective_path(path) and path not in hashes and path in inventory and
                inventory[path]['type'] == 'regular' and inventory[path].get('md5') == digest,
                'Package file checksum differs')
        hashes[path] = digest
    require(set(hashes) == {path for path, record in inventory.items() if record['type'] == 'regular'},
            'Package checksum inventory is incomplete')
    return {'package': package, 'architecture': architecture, 'version': version,
            'source_package': fields.get('Source'), 'status': stanzas[0],
            'filelist_sha256': filelist['sha256'], 'md5sums_sha256': md5sums['sha256'],
            'file_layers': sorted({tree[path]['layer'] for path in inventory}),
            'inventory_sha256': hashlib.sha256(json.dumps(inventory, sort_keys=True,
                separators=(',', ':')).encode()).hexdigest(), 'inventory': inventory}


class LayerDigestReader(io.RawIOBase):
    def __init__(self, stream, limit, check):
        self.stream, self.limit, self.check = stream, limit, check
        self.digest, self.count = hashlib.sha256(), 0

    def readable(self):
        return True

    def read(self, amount=-1):
        require(0 <= amount <= 1024**2, 'Unbounded layer read')
        self.check()
        block = self.stream.read(amount)
        self.count += len(block)
        require(self.count <= self.limit, 'Effective layer exceeds size bound')
        self.digest.update(block)
        return block


def evidence_text(path):
    return (path == 'var/lib/dpkg/status' or
            path.startswith(('var/lib/dpkg/info/linux-libc-dev', 'var/lib/dpkg/info/libbpf1')) or
            path in {'usr/local/share/victron-libbpf/build-provenance.json',
                     'usr/local/share/victron-libbpf/source-manifest.json',
                     'usr/local/share/victron-libbpf/package.buildinfo',
                     'usr/local/share/victron-libbpf/dynamic-symbols.txt',
                     'usr/local/share/victron-libbpf/debian-libbpf1.symbols'})


def read_effective_layer(raw, descriptor, remaining, check):
    """Hash an ordered layer in memory; retain only bounded metadata text."""
    require(descriptor['mediaType'] in {LAYER, LAYER + '+gzip'}, 'Unsupported effective layer compression')
    compressed = LayerDigestReader(raw, descriptor['size'], check)
    decoded = gzip.GzipFile(fileobj=compressed) if descriptor['mediaType'].endswith('+gzip') else compressed
    plain = LayerDigestReader(decoded, remaining, check)
    entries, text_bytes = {}, 0
    try:
        with tarfile.open(fileobj=plain, mode='r|') as archive:
            for member in archive:
                check()
                if member.name in {'.', './'} and member.isdir():
                    continue
                path = effective_path(member.name)
                require(path not in entries and len(entries) < 300000 and
                        not member.issparse() and 0 <= member.size <= 4 * 1024**3,
                        'Duplicate, sparse or excessive effective layer entry')
                record = {'type': 'other', 'bytes': member.size, 'mode': member.mode,
                          'layer': descriptor['digest']}
                if member.isfile():
                    # MD5 only matches dpkg's legacy inventory; SHA-256 binds
                    # the actual file bytes used for security acceptance.
                    sha, md5, count, magic = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0, b''
                    text = bytearray() if evidence_text(path) else None
                    with archive.extractfile(member) as source:
                        while block := source.read(1024**2):
                            check()
                            count += len(block)
                            sha.update(block)
                            md5.update(block)
                            if not magic:
                                magic = block[:4]
                            if text is not None:
                                text_bytes += len(block)
                                require(len(text) + len(block) <= 8 * 1024**2 and text_bytes <= 32 * 1024**2,
                                        'Effective metadata text exceeds bound')
                                text.extend(block)
                    require(count == member.size, 'Truncated effective file')
                    record.update(type='regular', sha256=sha.hexdigest(), md5=md5.hexdigest(), elf=magic == b'\x7fELF')
                    if text is not None:
                        record['text'] = text.decode('utf-8')
                elif member.isdir():
                    record['type'] = 'directory'
                elif member.issym():
                    record.update(type='symlink', target=member.linkname)
                elif member.islnk():
                    record.update(type='hardlink', target=effective_path(member.linkname))
                entries[path] = record
        while plain.read(1024**2):
            pass
        # Gzip may read ahead, but every compressed byte must still be bound.
        while compressed.read(1024**2):
            pass
        require(compressed.count == descriptor['size'] and
                'sha256:' + compressed.digest.hexdigest() == descriptor['digest'], 'Effective layer blob differs')
        return entries, plain.count, 'sha256:' + plain.digest.hexdigest()
    finally:
        if decoded is not compressed:
            decoded.close()


def effective_files(destination, selection, proof, check):
    """Read the verified scan layout once; this alone grants no scan exception."""
    verify_scan_input(destination, selection, proof, check)
    tree, unpacked, layers = {}, 0, []
    require(len(selection['layers']) == len(proof['platform']['diff_ids']), 'Effective layer count differs')
    for descriptor, expected_diff in zip(selection['layers'], proof['platform']['diff_ids'], strict=True):
        path = destination / 'blobs/sha256' / descriptor['digest'][7:]
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as raw:
            before = os.fstat(fd)
            require(stat.S_ISREG(before.st_mode) and before.st_size == descriptor['size'], 'Effective layer file differs')
            entries, size, diff = read_effective_layer(raw, descriptor, MAX_UNPACKED - unpacked, check)
            require(diff == expected_diff and identity(os.fstat(fd)) == identity(before) ==
                    identity(path.stat(follow_symlinks=False)), 'Effective layer changed or diff ID differs')
        unpacked += size
        tree = merge_effective_layer(tree, entries)
        require(len(tree) <= 300000, 'Effective file inventory exceeds bound')
        require(sum(len(record.get('text', '').encode('utf-8')) for record in tree.values()) <= 64 * 1024**2,
                'Effective metadata inventory exceeds bound')
        layers.append({'digest': descriptor['digest'], 'diff_id': diff, 'uncompressed_bytes': size})
    verify_scan_input(destination, selection, proof, check)
    return {'schema': 1, 'archive_sha256': proof['archive_sha256'], 'image_index': proof['image_index'],
            'archive_bytes': proof['archive_bytes'],
            'source_commit': proof['source_commit'], 'manifest': proof['platform']['manifest'],
            'config': proof['platform']['config'], 'platform': {'os': 'linux', 'architecture': 'amd64'},
            'layers': layers, 'files': tree}
