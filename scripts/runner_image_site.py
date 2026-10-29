#!/usr/bin/env python3
"""Derive a site OCI image by changing only its explicitly pinned public CA.

No image code, registry, runner registration or deployment is involved. The
output has unsigned conversion evidence, not a reused parent build attestation.
Full child scans and runtime qualification are still required.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile

try:
    from . import runner_image_oci as oci
except ImportError:
    import runner_image_oci as oci

TARGET = 'opt/victron-ci-reserve/guard-ca.crt'
BLOBS = 'blobs/sha256/'
PREDICATE = 'https://github.com/victron-venus/venus-os-ci-toolkit/runner-site-conversion/v1'
SENTINEL = b'UNCONFIGURED SITE CA\n'
MIB = 1024**2
GIB = 1024**3


def require(value, message):
    if not value:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def converter_identity():
    return {Path(path).name: sha(Path(path).read_bytes()) for path in (__file__, oci.__file__)}


def confined_path(workspace, relative, *, must_exist):
    """Only operator workspace-relative files; no traversal or symlink parents."""
    root = Path(workspace).resolve(strict=True)
    require(root.is_dir(), 'Workspace must be an existing directory')
    relative = Path(relative)
    require(not relative.is_absolute() and relative.parts and
            all(part not in {'.', '..'} for part in relative.parts), 'Workspace-relative file path required')
    path = root / relative
    for parent in [path, *path.parents]:
        if parent == root:
            break
        require(not parent.is_symlink(), 'Workspace paths cannot contain symlinks')
    require(path.resolve(strict=must_exist).is_relative_to(root), 'Path escapes workspace')
    require(path.parent.is_dir(), 'Output parent must already exist')
    return path


class ReserveReader:
    """Observe concurrent disk growth while copying a large inherited layer."""
    def __init__(self, stream, directory, reserve):
        self.stream, self.directory, self.reserve = stream, directory, reserve
        self.remaining = 0

    def read(self, count):
        if self.remaining <= 0:
            require(shutil.disk_usage(self.directory).free >= self.reserve + MIB,
                    'Disk reserve crossed while copying layer')
            self.remaining = MIB
        data = self.stream.read(count)
        self.remaining -= len(data)
        return data


def read_ca(path, expected):
    """A single currently valid public CA, pinned as exact PEM file bytes."""
    require(re.fullmatch(r'[0-9a-f]{64}', expected or ''), 'Expected CA file SHA256 required')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and 0 < info.st_size < 65536,
                'CA must be a bounded regular file')
        raw = stream.read(65536)
    require(sha(raw) == expected, 'CA file SHA256 differs')
    require(re.fullmatch(rb'-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\r?\n?', raw),
            'Exactly one public PEM certificate is required')
    # Validate the bytes already read, not a path that could change between calls.
    with tempfile.TemporaryDirectory(prefix='runner-public-ca-') as directory:
        certificate = Path(directory) / 'ca.pem'
        certificate.write_bytes(raw)
        for args in (['x509', '-in', str(certificate), '-noout', '-ext', 'basicConstraints'],
                     ['verify', '-CAfile', str(certificate), str(certificate)]):
            result = subprocess.run(['openssl', *args], capture_output=True, timeout=15, check=False)
            require(result.returncode == 0, 'CA certificate validation failed')
            if args[0] == 'x509':
                require(b'CA:TRUE' in result.stdout, 'Certificate is not a CA')
    return raw


def ca_layer(raw):
    plain = io.BytesIO()
    with tarfile.open(fileobj=plain, mode='w', format=tarfile.USTAR_FORMAT) as layer:
        info = tarfile.TarInfo(TARGET)
        info.size, info.mode, info.uid, info.gid, info.mtime = len(raw), 0o644, 0, 0, 0
        layer.addfile(info, io.BytesIO(raw))
    payload = plain.getvalue()
    compressed = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=compressed, mtime=0) as stream:
        stream.write(payload)
    return compressed.getvalue(), 'sha256:' + sha(payload)


def parent(path, expected, size, source, stage):
    proof = oci.verify(path, expected, size, source, stage, SENTINEL)
    archive = oci.Archive(path, expected, size)
    try:
        runtime = oci.json_document(archive.read(BLOBS + proof['platform']['manifest'][7:]))
        config = archive.json_descriptor(runtime['config'], oci.CONFIG)
        return archive, proof, runtime, config
    except BaseException:
        archive.close()
        raise


def graph(proof, runtime, config, ca):
    """Produce the entire deterministic child graph apart from parent layers."""
    blobs = {}

    def blob(value, media):
        raw = value if isinstance(value, bytes) else canonical(value)
        digest = sha(raw)
        blobs[digest] = raw
        return {'mediaType': media, 'digest': 'sha256:' + digest, 'size': len(raw)}

    layer, diff_id = ca_layer(ca)
    child_config = copy.deepcopy(config)
    child_config['rootfs']['diff_ids'].append(diff_id)
    child_config.setdefault('history', []).append({
        'created': '1970-01-01T00:00:00Z', 'created_by': 'runner_image_site.py: public CA only'})
    child_runtime = copy.deepcopy(runtime)
    child_runtime['config'] = blob(child_config, oci.CONFIG)
    child_runtime['layers'].append(blob(layer, oci.LAYER + '+gzip'))
    runtime_descriptor = blob(child_runtime, oci.MANIFEST)
    conversion = {
        '_type': 'https://in-toto.io/Statement/v1',
        'subject': [{'name': 'site-runner-image', 'digest': {'sha256': runtime_descriptor['digest'][7:]}}],
        'predicateType': PREDICATE,
        'predicate': {
            'signed': False, 'parent_source_commit': proof['source_commit'], 'stage': proof['stage'],
            'converter_source_hashes': converter_identity(),
            'parent': {k: proof[k] for k in ('archive_sha256', 'archive_bytes', 'image_index')}
                      | {'runtime_manifest': proof['platform']['manifest']},
            'change': {'path': TARGET, 'file_sha256': sha(ca), 'bytes': len(ca),
                       'mode': 0o644, 'uid': 0, 'gid': 0, 'mtime': 0, 'diff_id': diff_id},
            'runtime_qualified': False, 'scan_accepted': False,
        },
    }
    statement = blob(conversion, 'application/vnd.in-toto+json')
    attestation = blob({'schemaVersion': 2, 'artifactType': oci.ATTESTATION,
                       'config': blob(b'{}', oci.EMPTY_CONFIG), 'layers': [statement],
                       'subject': dict(runtime_descriptor)}, oci.MANIFEST)
    attestation['platform'] = {'os': 'unknown', 'architecture': 'unknown'}
    attestation['annotations'] = {'vnd.docker.reference.type': 'attestation-manifest',
                                 'vnd.docker.reference.digest': runtime_descriptor['digest']}
    runtime_descriptor['platform'] = {'os': 'linux', 'architecture': 'amd64'}
    root = blob({'schemaVersion': 2, 'manifests': [runtime_descriptor, attestation]}, oci.INDEX)
    files = {'index.json': canonical({'schemaVersion': 2, 'manifests': [root]}),
             'oci-layout': canonical({'imageLayoutVersion': '1.0.0'}),
             **{BLOBS + key: value for key, value in blobs.items()}}
    return files, root, runtime_descriptor, child_config, child_runtime, conversion


def verify_site(path, expected, size, parent_path, parent_sha, parent_size, source, stage, ca):
    """Recompute the exact graph; reject every undeclared child byte or file."""
    base, proof, runtime, config = parent(parent_path, parent_sha, parent_size, source, stage)
    child = None
    try:
        files, root, descriptor, child_config, child_runtime, conversion = graph(proof, runtime, config, ca)
        inherited = {BLOBS + d['digest'][7:]: d for d in runtime['layers']}
        child = oci.Archive(path, expected, size)
        require(set(child.files) == set(files) | set(inherited), 'Child graph contains unexpected or missing files')
        for name, raw in files.items():
            require(child.files[name].size == len(raw) and child.read(name) == raw, 'Child graph differs from CA-only derivation')
        for name, desc in inherited.items():
            require(child.files[name].size == desc['size'], 'Parent layer size changed')
        # Both Archive constructors have rehashed every blob. Inherited names
        # and sizes therefore bind exact bytes, not merely a declared prefix.
        base.unchanged()
        child.unchanged()
        return {'schema': 1, 'stage': stage, 'source_commit': source, 'archive_sha256': expected,
                'archive_bytes': size, 'image_index': root['digest'],
                'platform': {'manifest': descriptor['digest'], 'config': child_runtime['config']['digest'],
                             'layers': child_runtime['layers'], 'diff_ids': child_config['rootfs']['diff_ids'],
                             'runtime_config': child_config['config']},
                'parent': conversion['predicate']['parent'], 'ca_file_sha256': sha(ca),
                'parent_source_commit': source,
                'changed_paths': [TARGET], 'runtime_config_unchanged': True,
                'conversion_predicate': PREDICATE, 'signed': False,
                'converter_source_hashes': conversion['predicate']['converter_source_hashes'],
                'site_configured': True, 'deployable': False,
                'scan_accepted': False, 'runtime_qualified': False,
                'claim': 'Exact CA-only OCI derivation; full child scan and bootstrap/JIT acceptance still required.'}
    finally:
        base.close()
        if child is not None:
            child.close()


def derive(parent_path, parent_sha, parent_size, source, stage, ca_path, ca_sha, output,
           *, workspace, reserve_bytes=20 * GIB):
    parent_path = confined_path(workspace, parent_path, must_exist=True)
    ca_path = confined_path(workspace, ca_path, must_exist=True)
    output = confined_path(workspace, output, must_exist=False)
    require(not output.exists() and not output.is_symlink(), 'Output already exists')
    require(type(reserve_bytes) is int and reserve_bytes >= 20 * GIB, 'At least 20 GiB disk reserve required')
    ca = read_ca(ca_path, ca_sha)
    base, proof, runtime, config = parent(parent_path, parent_sha, parent_size, source, stage)
    temporary = None
    try:
        files, _, _, _, _, _ = graph(proof, runtime, config, ca)
        inherited = {BLOBS + d['digest'][7:]: d for d in runtime['layers']}
        # One output archive plus tar header/padding overhead; no expanded rootfs.
        projected = sum(len(v) for v in files.values()) + sum(d['size'] for d in inherited.values())
        projected += (len(files) + len(inherited)) * 1024 + 10240
        require(shutil.disk_usage(output.parent).free >= reserve_bytes + projected, 'Insufficient space for child and reserve')
        fd, name = tempfile.mkstemp(prefix='.runner-site-', suffix='.partial', dir=output.parent)
        temporary = Path(name)
        with os.fdopen(fd, 'wb') as stream:
            with tarfile.open(fileobj=stream, mode='w', format=tarfile.USTAR_FORMAT) as archive:
                for path in sorted(set(files) | set(inherited)):
                    require(shutil.disk_usage(output.parent).free >= reserve_bytes, 'Disk reserve crossed')
                    info = tarfile.TarInfo(path)
                    info.mode, info.uid, info.gid, info.mtime = 0o644, 0, 0, 0
                    if path in files:
                        info.size = len(files[path])
                        archive.addfile(info, io.BytesIO(files[path]))
                    else:
                        info.size = inherited[path]['size']
                        with base.tar.extractfile(base.files[path]) as blob:
                            archive.addfile(info, ReserveReader(blob, output.parent, reserve_bytes))
            stream.flush()
            os.fsync(stream.fileno())
        base.unchanged()
        require(shutil.disk_usage(output.parent).free >= reserve_bytes, 'Disk reserve crossed')
        expected, size = oci.digest_file(temporary), temporary.stat().st_size
        result = verify_site(temporary, expected, size, parent_path, parent_sha, parent_size, source, stage, ca)
        os.link(temporary, output)  # No overwrite, including a concurrently created destination.
        temporary.unlink()
        temporary = None
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return result
    finally:
        base.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True,
                        help='Existing private operator directory; all file arguments are relative to it')
    parser.add_argument('--parent', type=Path, required=True)
    parser.add_argument('--parent-sha256', required=True)
    parser.add_argument('--parent-bytes', type=int, required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--stage', choices=('public', 'private'), required=True)
    parser.add_argument('--ca', type=Path, required=True)
    parser.add_argument('--ca-file-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--verify', action='store_true', help='Verify an existing child instead of creating it')
    parser.add_argument('--child-sha256', help='Required expected child archive hash with --verify')
    parser.add_argument('--child-bytes', type=int, help='Required expected child archive size with --verify')
    args = parser.parse_args()
    args.receipt = confined_path(args.workspace, args.receipt, must_exist=False)
    require(not args.receipt.exists() and not args.receipt.is_symlink(), 'Receipt already exists')
    if args.verify:
        require(args.child_sha256 and args.child_bytes, 'Expected child hash and size required')
        ca = read_ca(confined_path(args.workspace, args.ca, must_exist=True), args.ca_file_sha256)
        result = verify_site(confined_path(args.workspace, args.output, must_exist=True),
                             args.child_sha256, args.child_bytes,
                             confined_path(args.workspace, args.parent, must_exist=True),
                             args.parent_sha256, args.parent_bytes, args.source, args.stage, ca)
    else:
        require(not args.child_sha256 and not args.child_bytes, 'Child identity is only an input with --verify')
        result = derive(args.parent, args.parent_sha256, args.parent_bytes, args.source,
                        args.stage, args.ca, args.ca_file_sha256, args.output, workspace=args.workspace)
    oci.save(args.receipt, result)
    print(json.dumps({'receipt': str(args.receipt), 'site_configured': True, 'deployable': False}))


if __name__ == '__main__':
    main()
