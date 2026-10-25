"""Artifact-only runner builds: real Git/tar fixtures, no Docker or network."""
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from scripts import build_runner_image_artifacts as build
from scripts import runner_image_cgroup as cgroup
from scripts import runner_image_oci as oci

SOURCE = 'a' * 40


def layer_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as tar:
        for path, content in entries:
            item = tarfile.TarInfo(path)
            item.size = len(content)
            tar.addfile(item, io.BytesIO(content))
    return output.getvalue()


def image(path, *, stage='public', content=build.SENTINEL, extra=False,
          provenance=True, wrong_diff=False, edit=None, additional=(),
          artifact_attestation=False, attestation_first=False, edit_attestation=None,
          edit_attestation_descriptor=None, edit_runtime_manifest=None, edit_blobs=None):
    blobs = {}
    def blob(value, media):
        raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode()
        sha = hashlib.sha256(raw).hexdigest()
        blobs[sha] = raw
        return {'mediaType': media, 'digest': 'sha256:' + sha, 'size': len(raw)}
    entries = [('opt/victron-ci-reserve/guard-ca.crt', content)]
    plain = [layer_bytes(entries)] + [layer_bytes(e) for e in additional]
    layers = [blob(gzip.compress(raw, mtime=0), oci.LAYER + '+gzip') for raw in plain]
    config = {'os': 'linux', 'architecture': 'amd64', 'config': {
        'User': '1001:1001', 'WorkingDir': '/home/runner', 'Entrypoint': oci.ENTRY,
        'Env': ['HOME=/home/runner', 'RUNNER_TOOL_CACHE=/opt/hostedtoolcache',
                'AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache', 'ImageOS=ubuntu24'],
        'Labels': {'org.opencontainers.image.revision': SOURCE,
                   'org.opencontainers.image.source': oci.SOURCE_URL, 'io.victron.runner.stage': stage}},
        'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + hashlib.sha256(p).hexdigest() for p in plain]}}
    if wrong_diff:
        config['rootfs']['diff_ids'][0] = 'sha256:' + '0' * 64
    if edit:
        edit(config)
    conf = blob(config, oci.CONFIG)
    runtime_manifest = {'schemaVersion': 2, 'config': conf, 'layers': layers}
    if edit_runtime_manifest:
        edit_runtime_manifest(runtime_manifest, blob)
    manifest = blob(runtime_manifest, oci.MANIFEST)
    manifest['platform'] = {'architecture': 'amd64', 'os': 'linux'}
    manifests = [manifest]
    if provenance:
        statement = blob({'_type': 'https://in-toto.io/Statement/v0.1',
            'subject': [{'name': 'image', 'digest': {'sha256': manifest['digest'][7:]}}],
            'predicateType': 'https://slsa.dev/provenance/v0.2', 'predicate': {}}, 'application/vnd.in-toto+json')
        if artifact_attestation:
            config_desc = blob(b'{}', 'application/vnd.oci.empty.v1+json')
            config_desc['data'] = 'e30='
            attestation = {'schemaVersion': 2, 'config': config_desc, 'layers': [statement],
                'artifactType': 'application/vnd.docker.attestation.manifest.v1+json',
                'subject': {key: manifest[key] for key in ('mediaType', 'digest', 'size')}}
        else:
            attestation = {'schemaVersion': 2,
                'config': blob({'os': 'unknown', 'architecture': 'unknown'}, oci.CONFIG),
                'layers': [statement]}
        if edit_attestation:
            edit_attestation(attestation, blob)
        att = blob(attestation, oci.MANIFEST)
        att['annotations'] = {'vnd.docker.reference.type': 'attestation-manifest'}
        if artifact_attestation:
            att['platform'] = {'architecture': 'unknown', 'os': 'unknown'}
            att['annotations']['vnd.docker.reference.digest'] = manifest['digest']
        if edit_attestation_descriptor:
            edit_attestation_descriptor(att)
        manifests.append(att)
    if attestation_first:
        manifests.reverse()
    root = blob({'schemaVersion': 2, 'manifests': manifests}, oci.INDEX)
    if extra:
        blob(b'orphan', oci.LAYER)
    if edit_blobs:
        edit_blobs(blobs)
    files = {'index.json': json.dumps({'schemaVersion': 2, 'manifests': [root]}).encode(),
             'oci-layout': b'{"imageLayoutVersion":"1.0.0"}',
             **{'blobs/sha256/' + k: v for k, v in blobs.items()}}
    with tarfile.open(path, 'w') as tar:
        for name, raw in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(raw)
            tar.addfile(item, io.BytesIO(raw))
    return oci.digest_file(path), path.stat().st_size


class RunnerArtifactsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def verify(self, **kwargs):
        path = self.root / 'image.tar'
        sha, size = image(path, **kwargs)
        return oci.verify(path, sha, size, SOURCE, kwargs.get('stage', 'public'), build.SENTINEL)

    def event(self):
        env = {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_OS': 'Linux',
               'RUNNER_ARCH': 'X64', 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REPOSITORY': 'victron-venus/venus-os-ci-toolkit',
               'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': SOURCE, 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}
        return env, {'repository': {'private': False, 'full_name': 'victron-venus/venus-os-ci-toolkit', 'default_branch': 'main'}}

    def test_only_public_manual_default_branch_native_hosted_is_allowed(self):
        env, event = self.event()
        build.validate_event(env, event)
        for key, value in [('GITHUB_EVENT_NAME', 'pull_request_target'), ('RUNNER_ENVIRONMENT', 'self-hosted'),
                           ('RUNNER_ARCH', 'ARM64'), ('GITHUB_REF', 'refs/heads/other'), ('GITHUB_SHA', 'main'),
                           ('GITHUB_RUN_ATTEMPT', '../bad')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                build.validate_event({**env, key: value}, event)
        event['repository']['private'] = True
        with self.assertRaises(ValueError):
            build.validate_event(env, event)
        event['repository'].update(private=False, full_name='example/fork')
        env['GITHUB_REPOSITORY'] = 'example/fork'
        with self.assertRaisesRegex(ValueError, 'Canonical toolkit'):
            build.validate_event(env, event)

    def test_context_reads_committed_blobs_not_worktree_or_ignored_secrets(self):
        repo = self.root / 'repo'
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL)
        git('init', '-q')
        recipe = 'Dockerfile'
        (repo / recipe).write_bytes(b'FROM scratch\n')
        (repo / 'Dockerfile.dockerignore').write_bytes(b'**\n!Dockerfile\n!Dockerfile.dockerignore\n')
        files = {n: {'sha256': hashlib.sha256((repo / n).read_bytes()).hexdigest(), 'bytes': (repo / n).stat().st_size,
                     'mode': '100644'} for n in (recipe, recipe + '.dockerignore')}
        git('add', '.')
        git('-c', 'commit.gpgsign=false', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '-qm', 'fixture')
        (repo / recipe).write_bytes(b'uncommitted malicious build\n')
        (repo / 'private.key').write_text('synthetic private fixture')
        out = self.root / 'context'
        build.prepare_context(repo, out, {'recipe': recipe, 'files': files}, False, os.environ.copy())
        self.assertEqual((out / recipe).read_bytes(), b'FROM scratch\n')
        self.assertFalse((out / 'private.key').exists())
        files[recipe]['sha256'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'Canonical context'):
            build.prepare_context(repo, self.root / 'wrong', {'recipe': recipe, 'files': files}, False, os.environ.copy())

    def test_canonical_manifest_and_dockerfiles_are_complete(self):
        manifest = build.document(build.MANIFEST)
        build.validate_manifest(manifest)
        for stage, spec in manifest['contexts'].items():
            for name, record in spec['files'].items():
                self.assertEqual(oci.digest_file(build.ROOT / name), record['sha256'])
                self.assertEqual((build.ROOT / name).stat().st_size, record['bytes'])
                tracked = subprocess.check_output(['git', '-C', str(build.ROOT), 'ls-files', '-s', '--', name],
                                                  text=True).splitlines()
                self.assertEqual(len(tracked), 1)
                self.assertEqual(tracked[0].split()[0], record['mode'])
            declared = {line[1:] for line in (build.ROOT / (spec['recipe'] + '.dockerignore')).read_text().splitlines()
                        if line.startswith('!')}
            self.assertEqual(declared, set(spec['files']) | ({manifest['sentinel']['path']} if stage == 'public' else set()))

    def test_clean_environment_does_not_forward_credentials(self):
        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'synthetic', 'DOCKER_AUTH_CONFIG': 'synthetic'}):
            env = build.clean_env(self.root)
        self.assertNotIn('GITHUB_TOKEN', env)
        self.assertNotIn('DOCKER_AUTH_CONFIG', env)
        self.assertEqual(env['DOCKER_CONFIG'], str(self.root / 'docker'))

    def test_complete_oci_is_non_deployable_and_provenance_bound(self):
        proof = self.verify()
        self.assertFalse(proof['deployable'])
        self.assertFalse(proof['site_configured'])
        self.assertEqual(proof['sentinel_sha256'], hashlib.sha256(build.SENTINEL).hexdigest())
        self.assertEqual(len(proof['attestations']), 1)

    def test_effective_files_select_final_bytes_after_whiteouts(self):
        proof = self.verify(additional=(
            [('usr/include/original.h', b'old'), ('usr/include/remove.h', b'remove'),
             ('var/lib/dpkg/status', b'Package: example\nVersion: 1\n')],
            [('usr/include/.wh.original.h', b''), ('usr/include/original.h', b'new'),
             ('usr/include/.wh.remove.h', b''), ('var/lib/dpkg/status', b'Package: example\nVersion: 2\n')]))
        layout = self.root / 'layout'
        selection = oci.project_scan_input(self.root / 'image.tar', proof, layout, lambda: None)
        result = oci.effective_files(layout, selection, proof, lambda: None)
        self.assertEqual(result['archive_sha256'], proof['archive_sha256'])
        self.assertEqual(result['manifest'], proof['platform']['manifest'])
        self.assertEqual(result['files']['usr/include/original.h']['sha256'], hashlib.sha256(b'new').hexdigest())
        self.assertNotIn('usr/include/remove.h', result['files'])
        self.assertEqual(result['files']['var/lib/dpkg/status']['text'], 'Package: example\nVersion: 2\n')
        self.assertEqual([row['diff_id'] for row in result['layers']], proof['platform']['diff_ids'])

    def test_effective_layer_opaque_whiteouts_preserve_same_layer_additions(self):
        regular = {'type': 'regular'}
        previous = {'a/old': regular, 'elsewhere': regular}
        entries = {'a/new': regular, 'a/.wh..wh..opq': regular}
        result = oci.merge_effective_layer(previous, entries)
        self.assertEqual(set(result), {'a/new', 'elsewhere'})
        result = oci.merge_effective_layer(previous, {'new': regular, '.wh..wh..opq': regular})
        self.assertEqual(set(result), {'new'})

    def test_effective_layer_parent_replacement_removes_stale_descendants(self):
        previous = {'a': {'type': 'directory'}, 'a/header': {'type': 'regular'}}
        result = oci.merge_effective_layer(previous, {'a': {'type': 'symlink', 'target': 'elsewhere'}})
        self.assertEqual(result, {'a': {'type': 'symlink', 'target': 'elsewhere'}})
        result = oci.merge_effective_layer(result, {'a': {'type': 'directory'}, 'a/new': {'type': 'regular'}})
        self.assertEqual(set(result), {'a', 'a/new'})

    def test_effective_layer_requires_directory_before_child_application(self):
        previous = {'docs': {'type': 'symlink', 'target': 'archive'}}
        with self.assertRaisesRegex(ValueError, 'ancestor at application'):
            oci.merge_effective_layer(previous, {'docs/note': {'type': 'regular'},
                                                 'docs': {'type': 'directory'}})
        result = oci.merge_effective_layer(previous, {'docs': {'type': 'directory'},
                                                      'docs/note': {'type': 'regular'}})
        self.assertEqual(result['docs/note']['type'], 'regular')
        with self.assertRaisesRegex(ValueError, 'Whiteout'):
            oci.merge_effective_layer(previous, {'docs/.wh.note': {'type': 'regular'}})

    def test_hardlink_metadata_never_grants_direct_regular_file_evidence(self):
        original = {'manual': {'type': 'regular', 'mode': 0o644}}
        result = oci.merge_effective_layer(original,
            {'manual-copy': {'type': 'hardlink', 'target': 'manual', 'mode': 0o755}})
        self.assertEqual(original['manual'], {'type': 'regular', 'mode': 0o644})
        with self.assertRaisesRegex(ValueError, 'hardlink alias'):
            oci.effective_regular(result, 'manual')
        # A simple path model cannot prove inode separation after overwrite.
        result = oci.merge_effective_layer(result, {'manual': {'type': 'regular', 'mode': 0o644}})
        with self.assertRaisesRegex(ValueError, 'hardlink alias'):
            oci.effective_regular(result, 'manual')
        ordinary = oci.merge_effective_layer(result, {'unrelated': {'type': 'regular'}})
        self.assertEqual(oci.effective_regular(ordinary, 'unrelated'), {'type': 'regular'})
        package = self.package_fixture()
        package['files'] = oci.merge_effective_layer(package['files'],
            {'header-copy': {'type': 'hardlink', 'target': 'usr/include/a.h', 'mode': 0o755}})
        with self.assertRaisesRegex(ValueError, 'hardlink alias'):
            oci.package_files(package, 'headers', 'amd64', '1')

    def test_selected_effective_files_refuse_links_and_non_directory_ancestors(self):
        regular = {'type': 'regular', 'sha256': 'a' * 64}
        self.assertEqual(oci.effective_regular({'a/f': regular}, 'a/f'), regular)
        for kind in ('symlink', 'hardlink', 'regular', 'other'):
            with self.subTest(ancestor=kind), self.assertRaisesRegex(ValueError, 'ancestor'):
                oci.effective_regular({'a': {'type': kind}, 'a/f': regular}, 'a/f')
        for kind in ('symlink', 'hardlink', 'directory'):
            with self.subTest(selected=kind), self.assertRaisesRegex(ValueError, 'not regular'):
                oci.effective_regular({'a/f': {'type': kind}}, 'a/f')

    def test_effective_layer_validates_blob_and_diff_id_and_bounded_reads(self):
        plain = layer_bytes([('usr/include/a.h', b'header')])
        for compressed in (False, True):
            raw = gzip.compress(plain, mtime=0) if compressed else plain
            desc = {'mediaType': oci.LAYER + ('+gzip' if compressed else ''),
                    'size': len(raw), 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}
            with self.subTest(compressed=compressed):
                records, size, diff = oci.read_effective_layer(io.BytesIO(raw), desc, len(plain), lambda: None)
                self.assertEqual(records['usr/include/a.h']['sha256'], hashlib.sha256(b'header').hexdigest())
                self.assertEqual((size, diff), (len(plain), 'sha256:' + hashlib.sha256(plain).hexdigest()))
                with self.assertRaisesRegex(ValueError, 'size bound'):
                    oci.read_effective_layer(io.BytesIO(raw), desc, len(plain) - 1, lambda: None)
                with self.assertRaisesRegex(ValueError, 'blob differs'):
                    oci.read_effective_layer(io.BytesIO(raw), {**desc, 'digest': 'sha256:' + '0' * 64}, len(plain), lambda: None)

    def test_effective_layer_normalizes_paths_and_refuses_duplicate_members(self):
        raw = layer_bytes([('usr//include/./a.h', b'a')])
        desc = {'mediaType': oci.LAYER, 'size': len(raw), 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}
        records, _, _ = oci.read_effective_layer(io.BytesIO(raw), desc, len(raw), lambda: None)
        self.assertEqual(set(records), {'usr/include/a.h'})
        raw = layer_bytes([('usr/include/a.h', b'a'), ('usr//include/a.h', b'b')])
        desc.update(size=len(raw), digest='sha256:' + hashlib.sha256(raw).hexdigest())
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            oci.read_effective_layer(io.BytesIO(raw), desc, len(raw), lambda: None)

    def test_effective_files_reject_changed_layout_before_using_metadata(self):
        proof = self.verify(additional=([('var/lib/dpkg/status', b'Package: example\n')],))
        layout = self.root / 'layout'
        selection = oci.project_scan_input(self.root / 'image.tar', proof, layout, lambda: None)
        blob = layout / 'blobs/sha256' / selection['layers'][-1]['digest'][7:]
        blob.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'size differs'):
            oci.effective_files(layout, selection, proof, lambda: None)

    def package_fixture(self):
        def regular(data, text=False):
            result = {'type': 'regular', 'bytes': len(data), 'mode': 0o644, 'layer': 'sha256:' + '1' * 64,
                      'sha256': hashlib.sha256(data).hexdigest(), 'md5': hashlib.md5(data).hexdigest(), 'elf': False}
            if text:
                result['text'] = data.decode()
            return result
        content = b'ordinary header\n'
        return {'files': {
            'usr': {'type': 'directory', 'bytes': 0, 'mode': 0o755, 'layer': 'sha256:' + '1' * 64},
            'usr/include/a.h': regular(content),
            'var/lib/dpkg/status': regular(b'Package: headers\nStatus: install ok installed\n'
                b'Architecture: amd64\nVersion: 1\nSource: fixture\n', True),
            'var/lib/dpkg/info/headers:amd64.list': regular(b'/.\n/usr\n/usr/include/a.h\n', True),
            'var/lib/dpkg/info/headers:amd64.md5sums': regular(
                (hashlib.md5(content).hexdigest() + '  usr/include/a.h\n').encode(), True)}}

    def test_package_evidence_binds_exact_installed_files(self):
        evidence = self.package_fixture()
        result = oci.package_files(evidence, 'headers', 'amd64', '1')
        self.assertEqual(result['source_package'], 'fixture')
        self.assertEqual(set(result['inventory']), {'usr', 'usr/include/a.h'})
        moved = copy.deepcopy(evidence)
        moved['files']['usr/include/a.h']['layer'] = 'sha256:' + '2' * 64
        self.assertEqual(oci.package_files(moved, 'headers', 'amd64', '1')['inventory_sha256'],
                         result['inventory_sha256'])
        moved['files']['usr/include/a.h']['mode'] = 0o755
        self.assertNotEqual(oci.package_files(moved, 'headers', 'amd64', '1')['inventory_sha256'],
                            result['inventory_sha256'])

    def test_package_evidence_rejects_stale_status_files_or_checksums(self):
        edits = [
            lambda tree: tree['var/lib/dpkg/status'].update(text='Package: headers\nStatus: install ok installed\nArchitecture: amd64\nVersion: 2\n'),
            lambda tree: tree.pop('usr/include/a.h'),
            lambda tree: tree['usr/include/a.h'].update(md5='0' * 32),
            lambda tree: tree['var/lib/dpkg/info/headers:amd64.md5sums'].update(text=''),
            lambda tree: tree['var/lib/dpkg/info/headers:amd64.list'].update(text='/usr\n/usr/include/a.h\n/usr/include/a.h\n'),
            lambda tree: tree['usr'].update(type='symlink', target='elsewhere'),
        ]
        for edit in edits:
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                evidence = self.package_fixture()
                edit(evidence['files'])
                oci.package_files(evidence, 'headers', 'amd64', '1')

    def test_artifact_attestation_accepts_either_order_and_optional_inline_data(self):
        for first in (False, True):
            for inline in (False, True):
                with self.subTest(attestation_first=first, inline=inline):
                    proof = self.verify(artifact_attestation=True, attestation_first=first,
                        edit_attestation=None if inline else lambda att, blob: att['config'].pop('data'))
                    self.assertFalse(proof['deployable'])
                    self.assertFalse(proof['site_configured'])
                    self.assertEqual(len(proof['attestations']), 1)
                    self.assertEqual(proof['attestations'][0]['subject'][0]['digest']['sha256'],
                                     proof['platform']['manifest'][7:])

    def test_artifact_attestation_requires_known_type_and_non_executable_platform(self):
        manifest_changes = [
            lambda att, blob: att.pop('artifactType'),
            lambda att, blob: att.update(artifactType='application/example'),
            lambda att, blob: att.update(config=blob({'os': 'unknown', 'architecture': 'unknown'}, oci.CONFIG)),
        ]
        for change in manifest_changes:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'artifact type'):
                self.verify(artifact_attestation=True, edit_attestation=change)
        descriptor_changes = [
            lambda desc: desc.pop('platform'),
            lambda desc: desc.update(platform={'architecture': 'amd64', 'os': 'linux'}),
            lambda desc: desc.update(platform={'architecture': 'unknown', 'os': 'linux'}),
        ]
        for change in descriptor_changes:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'Attestation platform'):
                self.verify(artifact_attestation=True, edit_attestation_descriptor=change)

    def test_empty_attestation_config_requires_canonical_descriptor_and_local_blob(self):
        descriptor_changes = [
            ('size', 1, 'Invalid empty'),
            ('size', True, 'Invalid empty'),
            ('size', 2.0, 'Invalid empty'),
            ('digest', 'sha256:' + 'f' * 64, 'Invalid empty'),
            ('data', None, 'Invalid empty'),
            ('data', 'W10=', 'Invalid empty'),
            ('data', 'e30', 'Invalid empty'),
            ('mediaType', 'application/example', 'artifact type'),
            ('urls', ['https://example.invalid/config'], 'Invalid/remote descriptor'),
        ]
        for key, value, error in descriptor_changes:
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, error):
                self.verify(artifact_attestation=True,
                    edit_attestation=lambda att, blob: att['config'].update({key: value}))
        for raw in (b'[]', b'{ }', b'{"extra": true}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, 'Invalid empty'):
                self.verify(artifact_attestation=True,
                    edit_attestation=lambda att, blob: att.update(config=blob(raw, 'application/vnd.oci.empty.v1+json')))
        empty_sha = hashlib.sha256(b'{}').hexdigest()
        with self.assertRaisesRegex(ValueError, 'Missing/wrong-size descriptor'):
            self.verify(artifact_attestation=True, edit_blobs=lambda blobs: blobs.pop(empty_sha))
        with self.assertRaisesRegex(ValueError, 'Blob hash mismatch'):
            self.verify(artifact_attestation=True, edit_blobs=lambda blobs: blobs.update({empty_sha: b'[]'}))

    def test_artifact_subject_requires_local_manifest_descriptor_and_reference(self):
        with self.assertRaisesRegex(ValueError, 'Attestation subject missing'):
            self.verify(artifact_attestation=True, edit_attestation=lambda att, blob: att.pop('subject'))
        subject_changes = [
            ('mediaType', oci.INDEX, 'Unexpected descriptor type'),
            ('digest', 'sha256:' + 'f' * 64, 'Missing/wrong-size descriptor'),
            ('size', 1, 'Missing/wrong-size descriptor'),
            ('urls', ['https://example.invalid/subject'], 'Invalid/remote descriptor'),
        ]
        for key, value, error in subject_changes:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, error):
                self.verify(artifact_attestation=True,
                    edit_attestation=lambda att, blob: att['subject'].update({key: value}))
        for reference in (None, 'sha256:' + 'f' * 64):
            def edit_reference(desc):
                if reference is None:
                    desc['annotations'].pop('vnd.docker.reference.digest')
                else:
                    desc['annotations']['vnd.docker.reference.digest'] = reference
            with self.subTest(reference=reference), self.assertRaisesRegex(ValueError, 'Attestation reference differs'):
                self.verify(artifact_attestation=True, edit_attestation_descriptor=edit_reference)

    def test_artifact_subject_and_matching_reference_cannot_target_another_local_manifest(self):
        target = {}
        def retarget(att, blob):
            other = {**att, 'annotations': {'test-purpose': 'not-the-runtime'}}
            att['subject'] = blob(other, oci.MANIFEST)
            target['digest'] = att['subject']['digest']
        def retarget_reference(desc):
            desc['annotations']['vnd.docker.reference.digest'] = target['digest']
        for first in (False, True):
            with self.subTest(attestation_first=first), self.assertRaisesRegex(ValueError, 'Attestation subject differs'):
                self.verify(artifact_attestation=True, attestation_first=first,
                    edit_attestation=retarget, edit_attestation_descriptor=retarget_reference)

    def test_runtime_rejects_empty_and_unknown_config_types(self):
        for media in ('application/vnd.oci.empty.v1+json', 'application/example'):
            with self.subTest(media=media), self.assertRaisesRegex(ValueError, 'Unexpected descriptor type'):
                self.verify(edit_runtime_manifest=lambda manifest, blob: manifest.update(config=blob(b'{}', media)))

    def test_attestation_layers_still_require_known_media_type_and_provenance_subject(self):
        for artifact in (False, True):
            with self.subTest(artifact=artifact), self.assertRaisesRegex(ValueError, 'Unexpected descriptor type'):
                self.verify(artifact_attestation=artifact,
                    edit_attestation=lambda att, blob: att['layers'][0].update(mediaType='application/example'))
            with self.subTest(artifact=artifact), self.assertRaisesRegex(ValueError, 'Build provenance subject missing'):
                self.verify(artifact_attestation=artifact,
                    edit_attestation=lambda att, blob: att.update(layers=[]))

    def test_different_ca_whiteout_missing_provenance_and_bad_diff_fail(self):
        changes = [{'content': b'not our sentinel'}, {'provenance': False}, {'wrong_diff': True}, {'extra': True},
                   {'additional': [[('opt/victron-ci-reserve/.wh.guard-ca.crt', b'')]]},
                   {'additional': [[('opt/victron-ci-reserve/.wh..wh..opq', b'')]]},
                   {'additional': [[('.wh..wh..opq', b'')]]}]
        for artifact in (False, True):
            for change in changes:
                with self.subTest(artifact=artifact, change=change), self.assertRaises(ValueError):
                    self.verify(artifact_attestation=artifact, **change)

    def test_source_root_user_and_credentials_fail(self):
        for edit in [lambda c: c['config'].update(User='0:0'),
                     lambda c: c['config']['Labels'].update({'org.opencontainers.image.revision': 'b' * 40}),
                     lambda c: c['config']['Env'].append('BOT_PAT=synthetic')]:
            with self.assertRaises(ValueError):
                self.verify(edit=edit)

    def test_private_requires_exact_base_layers_and_configuration(self):
        for artifact in (False, True):
            with self.subTest(artifact=artifact):
                base = self.verify(artifact_attestation=artifact)
                path = self.root / 'private.tar'
                sha, size = image(path, stage='private', artifact_attestation=artifact,
                                  additional=[[('extra-file', b'private tooling')]])
                proof = oci.verify(path, sha, size, SOURCE, 'private', build.SENTINEL, base)
                self.assertEqual(proof['platform']['layers'][:1], base['platform']['layers'])
                wrong = copy.deepcopy(base)
                wrong['platform']['diff_ids'] = ['sha256:' + 'f' * 64]
                with self.assertRaisesRegex(ValueError, 'public layers'):
                    oci.verify(path, sha, size, SOURCE, 'private', build.SENTINEL, wrong)
                sha, size = image(path, stage='private', artifact_attestation=artifact,
                                  edit=lambda c: c['config']['Env'].append('NEW_POLICY=1'))
                with self.assertRaisesRegex(ValueError, 'configuration'):
                    oci.verify(path, sha, size, SOURCE, 'private', build.SENTINEL, base)

    def test_archive_path_replacement_and_duplicate_json_fail(self):
        path = self.root / 'original.tar'
        sha, size = image(path)
        archive = oci.Archive(path, sha, size)
        try:
            replacement = self.root / 'replacement'
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
            with self.assertRaisesRegex(ValueError, 'identity'):
                archive.unchanged()
        finally:
            archive.close()
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            oci.json_document(b'{"source":1,"source":2}')

    def test_materialization_retains_only_oci_files(self):
        proof = self.verify()
        out = self.root / 'layout'
        check = mock.Mock()
        oci.materialize(self.root / 'image.tar', proof, out, check)
        self.assertTrue((out / 'index.json').is_file())
        self.assertFalse((out / 'opt').exists())
        self.assertGreater(check.call_count, 0)

    def test_scan_projection_selects_exact_runtime_regardless_of_attestation_order(self):
        for stage in ('public', 'private'):
            for first in (False, True):
                with self.subTest(stage=stage, attestation_first=first):
                    path = self.root / (stage + str(first) + '.tar')
                    sha, size = image(path, stage=stage, artifact_attestation=True, attestation_first=first)
                    proof = oci.verify(path, sha, size, SOURCE, stage, build.SENTINEL)
                    layout = self.root / (stage + str(first))
                    selection = oci.project_scan_input(path, proof, layout, mock.Mock())
                    self.assertEqual(oci.digest_file(path), sha)
                    index = json.loads((layout / 'index.json').read_bytes())
                    self.assertEqual(index['manifests'], [selection['runtime']])
                    self.assertEqual(selection['runtime']['digest'], proof['platform']['manifest'])
                    with tarfile.open(path) as archive:
                        for name in selection['files']:
                            if name != 'index.json':
                                self.assertEqual((layout / name).read_bytes(), archive.extractfile(name).read())
                    self.assertEqual(len(selection['files']), 2 + 2 + len(proof['platform']['layers']))
                    self.assertFalse((layout / 'opt').exists())
                    oci.verify_scan_input(layout, selection, proof, mock.Mock())
                    with self.assertRaises(FileExistsError):
                        oci.project_scan_input(path, proof, layout, mock.Mock())

    def test_scan_projection_rejects_wrong_proof_and_changed_bytes(self):
        proof = self.verify(artifact_attestation=True, attestation_first=True)
        path = self.root / 'image.tar'
        for name, change in [
                ('root', lambda p: p.update(image_index='sha256:' + '1' * 64)),
                ('runtime', lambda p: p['platform'].update(manifest='sha256:' + '2' * 64)),
                ('config', lambda p: p['platform'].update(config='sha256:' + '3' * 64)),
                ('layers', lambda p: p['platform'].update(layers=[])),
                ('diffids', lambda p: p['platform'].update(diff_ids=[]))]:
            wrong = copy.deepcopy(proof)
            change(wrong)
            with self.subTest(name=name), self.assertRaises(ValueError):
                oci.project_scan_input(path, wrong, self.root / name, mock.Mock())
        layout = self.root / 'correct'
        selection = oci.project_scan_input(path, proof, layout, mock.Mock())
        member = layout / ('blobs/sha256/' + proof['platform']['config'][7:])
        original = member.read_bytes()
        member.write_bytes(b'X' + original[1:])
        with self.assertRaisesRegex(ValueError, 'bytes changed'):
            oci.verify_scan_input(layout, selection, proof, mock.Mock())
        member.write_bytes(original)
        (layout / 'extra').write_text('unexpected')
        with self.assertRaisesRegex(ValueError, 'Unexpected scan file'):
            oci.verify_scan_input(layout, selection, proof, mock.Mock())
        (layout / 'extra').unlink()
        member.unlink()
        member.symlink_to(path)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            oci.verify_scan_input(layout, selection, proof, mock.Mock())

    def test_scan_projection_detects_source_changed_during_copy(self):
        proof = self.verify()
        path = self.root / 'image.tar'
        replacement = self.root / 'replacement.tar'
        replacement.write_bytes(path.read_bytes())
        changed = False
        def change():
            nonlocal changed
            if not changed:
                replacement.replace(path)
                changed = True
        with self.assertRaisesRegex(ValueError, 'Archive identity changed'):
            oci.project_scan_input(path, proof, self.root / 'layout', change)

    def builder(self):
        return {'Id': 'a' * 64, 'Name': '/buildx_buildkit_test0',
                'Config': {'Image': 'pinned', 'Env': ['RUNNER_ARTIFACT_OWNER=nonce']},
                'Mounts': [{'Type': 'volume', 'Name': 'buildx_buildkit_test0_state',
                            'Destination': '/var/lib/buildkit', 'RW': True}],
                'HostConfig': {**build.LIMITS, 'RestartPolicy': {'Name': 'no'}}, 'State': {'OOMKilled': False}}

    def test_foreign_or_unbounded_builder_is_rejected(self):
        good = self.builder()
        self.assertEqual(build.builder_identity(good, 'test', 'nonce', 'pinned'), 'a' * 64)
        for field in ('Memory', 'MemorySwap', 'CpuQuota', 'CpuPeriod'):
            bad = copy.deepcopy(good)
            bad['HostConfig'][field] = 0
            with self.assertRaises(ValueError):
                build.builder_identity(bad, 'test', 'nonce', 'pinned')
        with self.assertRaises(ValueError):
            build.builder_identity(good, 'test', 'different', 'pinned')

    def test_owned_cleanup_does_not_depend_on_disk_admission_or_health(self):
        for oom in (False, True):
            sup = mock.Mock(env={'DOCKER_CONFIG': str(self.root / 'docker')})
            sup.check.side_effect = ValueError('disk full')
            item = self.builder()
            item['State']['OOMKilled'] = oom
            results = [mock.Mock(returncode=0, stdout=json.dumps([item]).encode()), mock.Mock(),
                       mock.Mock(returncode=1, stdout=b'[]', stderr=b'Error: No such object: buildx_buildkit_test0')]
            with mock.patch.object(build.subprocess, 'run', side_effect=results) as run, \
                 mock.patch.object(build, 'probe_volume', return_value=None):
                state = build.remove_builder(sup, 'test', 'nonce', 'pinned')
            self.assertEqual(state['OOMKilled'], oom)
            sup.check.assert_not_called()
            self.assertEqual(run.call_args_list[1].args[0], ['docker', 'rm', '--force', 'a' * 64])

    def test_foreign_builder_is_never_removed(self):
        sup = mock.Mock(env={'DOCKER_CONFIG': str(self.root / 'docker')})
        with mock.patch.object(build.subprocess, 'run', return_value=mock.Mock(
                returncode=0, stdout=json.dumps([self.builder()]).encode())) as run:
            with self.assertRaises(ValueError):
                build.remove_builder(sup, 'test', 'foreign', 'pinned')
        self.assertEqual(run.call_count, 1)

    def test_last_write_is_subject_to_disk_floor_for_both_command_modes(self):
        for capture in (False, True):
            sup = build.Supervisor({}, [self.root], self.root / 'log', self.root / 'disk')
            with mock.patch.object(sup, 'check', side_effect=[3 * build.GIB, ValueError('Disk reserve exhausted')]), \
                 mock.patch.object(build.subprocess, 'run', return_value=mock.Mock(stdout=b'complete')), \
                 mock.patch.object(build.subprocess, 'Popen', return_value=mock.Mock(returncode=0, poll=lambda: 0)):
                with self.subTest(capture=capture), self.assertRaisesRegex(ValueError, 'Disk reserve'):
                    sup.run(['fixture'], capture=capture)

    def test_scanner_unknown_absence_or_unconfirmed_removal_is_not_success(self):
        sup = mock.Mock(env={})
        absent = mock.Mock(returncode=1, stdout=b'[]', stderr=b'Error: No such object: fixture-scan')
        unknown = mock.Mock(returncode=2, stdout=b'', stderr=b'Cannot connect to the Docker daemon')
        item = {'Id': 'f' * 64, 'Name': '/fixture-scan', 'Config': {'Image': 'pinned',
                'Labels': {build.OWNER: 'fixture'}}, 'State': {'OOMKilled': False}}
        present = mock.Mock(returncode=0, stdout=json.dumps([item]).encode())
        for sequence in ([unknown], [present, mock.Mock(), unknown], [present, mock.Mock(), present]):
            record = {}
            with mock.patch.object(build.subprocess, 'run', side_effect=sequence), self.assertRaises(ValueError):
                build.remove_scanner(sup, 'fixture-scan', 'fixture', 'pinned', record)
            self.assertNotIn('scanner_removed', record)
        record = {}
        with mock.patch.object(build.subprocess, 'run', side_effect=[present, mock.Mock(), absent]) as run:
            build.remove_scanner(sup, 'fixture-scan', 'fixture', 'pinned', record)
        self.assertTrue(record['scanner_removed'])
        self.assertEqual(run.call_args_list[1].args[0], ['docker', 'rm', '--force', 'f' * 64])
        self.assertFalse(build.container_absent(absent, 'another-container'))

    def test_posix_equivalent_layer_names_have_identical_ca_meaning(self):
        # Ordinary tar-writer spellings must not change which file is inspected.
        for spelling in ('./opt//victron-ci-reserve/./guard-ca.crt', 'opt/./victron-ci-reserve/guard-ca.crt'):
            with self.subTest(spelling=spelling):
                self.verify(additional=[[(spelling, build.SENTINEL)]])
        for spelling in ('./opt//victron-ci-reserve/./.wh.guard-ca.crt',
                         './opt/./victron-ci-reserve//.wh..wh..opq'):
            with self.subTest(spelling=spelling), self.assertRaises(ValueError):
                self.verify(additional=[[(spelling, b'')]])

    def cgroup_fixture(self, pid, group, marker=None):
        proc = self.root / 'proc'
        path = proc / str(pid)
        path.mkdir(parents=True)
        (path / 'cgroup').write_text('0::' + group + '\n')
        (path / 'stat').write_text(str(pid) + ' (fixture name) ' + ' '.join(['S'] + ['0'] * 18 + [str(pid * 100)]))
        (path / 'cmdline').write_bytes(b'sh\0-c\0sleep 20; :\0' + (marker or '').encode() + b'\0')
        return proc

    def cgroup_binding(self, relocated=True):
        container_id = 'a' * 64
        root = '/system.slice/docker-' + container_id + '.scope'
        path = root + '/init' if relocated else root
        private = '/init' if relocated else '/'
        proc = self.cgroup_fixture(100, path)
        parent = cgroup.process_identity(100, proc)
        return proc, {'container_id': container_id, 'builder_process': parent,
                      'private_process_path': private, 'owned_root': root}

    def test_actual_cgroup_limits_and_run_inheritance_are_required(self):
        marker = 'runner-proof-' + 'a' * 32
        proc, binding = self.cgroup_binding()
        root = binding['owned_root']
        self.cgroup_fixture(101, root + '/buildkit/child', marker)
        groups = self.root / 'cgroup'
        directory = groups / root.lstrip('/')
        leaf = directory / 'init'
        leaf.mkdir(parents=True)
        (groups / 'cgroup.controllers').write_text('cpu memory')
        expected = {'cpu.max': '200000 100000', 'memory.max': str(4 * build.GIB), 'memory.swap.max': '0'}
        for key, value in expected.items():
            (directory / key).write_text(value)
            (leaf / key).write_text('max')
        observations = []
        parent, limits = cgroup.sample(100, binding, proc, groups, observations.append)
        self.assertEqual(limits, expected)
        self.assertEqual(observations[-1]['leaf_limits']['memory.max'], 'max')
        self.assertEqual(observations[-1]['root_limits'], expected)
        # RUN is a sibling of the relocated daemon, under the same owned root.
        self.assertEqual(cgroup.run_processes(marker, binding, proc)[0]['pid'], 101)
        (proc / '101/cgroup').write_text('0::/docker/unbounded\n')
        with self.assertRaisesRegex(ValueError, 'outside'):
            cgroup.run_processes(marker, binding, proc)
        for key in expected:
            (directory / key).write_text('max')
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'limits differ'):
                cgroup.sample(100, binding, proc, groups, observations.append)
            self.assertEqual(observations[-1]['root_limits'][key], 'max')
            self.assertNotIn('accepted', observations[-1])
            (directory / key).write_text(expected[key])
        (directory / 'memory.swap.max').write_text(str(4 * build.GIB))
        with self.assertRaisesRegex(ValueError, 'limits differ'):
            cgroup.sample(100, binding, proc, groups)
        (proc / '100/cgroup').write_text('0::/docker/changed-root\n')
        with self.assertRaisesRegex(ValueError, 'process changed'):
            cgroup.sample(100, binding, proc, groups)
        (proc / '100/cgroup').write_text('0::' + parent['cgroup'] + '\n')
        (proc / '100/stat').write_text('100 (new process) ' + ' '.join(['S'] + ['0'] * 18 + ['999']))
        with self.assertRaisesRegex(ValueError, 'process changed'):
            cgroup.sample(100, binding, proc, groups)
        with self.assertRaisesRegex(ValueError, 'cgroup v2'):
            cgroup.group_path('2:memory:/docker/bounded')

    def test_owned_root_requires_exact_docker_identity_and_private_suffix(self):
        container_id = 'a' * 64
        for root in ('/docker/' + container_id, '/system.slice/docker-' + container_id + '.scope'):
            for private in ('/', '/init', '/internal/init'):
                with self.subTest(root=root, private=private):
                    host = root if private == '/' else root + private
                    self.assertEqual(cgroup.owned_root(container_id, {'cgroup': host}, private), root)
        for host, private in (('/docker/' + 'b' * 64 + '/init', '/init'),
                              ('/docker/' + container_id + '/init', '/other'),
                              ('/docker/' + container_id + '/init', '/'),
                              ('/docker/' + container_id + '/init', '/docker/' + container_id + '/init'),
                              ('/docker/' + container_id + '/init', '/in/../init'),
                              ('/docker/' + container_id + '/./init', '/init'),
                              ('/', '/')):
            with self.subTest(host=host, private=private), self.assertRaises(ValueError):
                cgroup.owned_root(container_id, {'cgroup': host}, private)

    def test_cgroup_receipt_binds_root_and_run_to_docker_identity(self):
        _, binding = self.cgroup_binding()
        proof = {**binding, 'cgroup_version': 2, 'namespace': 'private',
                 'run_inherits_builder_limits': True,
                 'effective_limits': {'cpu.max': '200000 100000', 'memory.max': str(4 * build.GIB),
                                      'memory.swap.max': '0'},
                 'run_process': {'pid': 101, 'starttime_ticks': '23',
                                 'cgroup': binding['owned_root'] + '/buildkit/run'}}
        record = {'builder_id': binding['container_id'], 'cgroup_proof': proof}
        build.check_cgroup_receipt(record)
        for target, key, value in (('root', 'owned_root', '/system.slice'),
                                   ('root', 'container_id', 'b' * 64),
                                   ('root', 'private_process_path', '/'),
                                   ('child', 'cgroup', binding['owned_root'] + '-other/run'),
                                   ('child', 'pid', 100)):
            modified = copy.deepcopy(record)
            dest = modified['cgroup_proof'] if target == 'root' else modified['cgroup_proof']['run_process']
            dest[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                build.check_cgroup_receipt(modified)

    def test_cgroup_preflight_requires_a_real_marked_run(self):
        _, binding = self.cgroup_binding()
        parent = binding['builder_process']
        child = {'pid': 101, 'starttime_ticks': '13', 'cgroup': binding['owned_root'] + '/buildkit/run'}
        observed = self.builder()
        observed['HostConfig']['CgroupnsMode'] = 'private'
        observed['State']['Pid'] = 100
        recipe = 'FROM docker.io/library/ubuntu@sha256:' + 'a' * 64 + '\n'
        sup = mock.Mock()
        def run(command, **kwargs):
            if command[:2] == ['docker', 'exec']:
                self.assertEqual(command, ['docker', 'exec', observed['Id'], 'cat', '/proc/1/cgroup'])
                return '0::/init'
            kwargs['inspect']()
        sup.run.side_effect = run
        for matches in ([], [child]):
            work = self.root / ('missing' if not matches else 'seen')
            work.mkdir()
            journal = []
            with mock.patch.object(build, 'inspect_builder', return_value=observed), \
                 mock.patch.object(cgroup, 'process_identity', return_value=parent), \
                 mock.patch.object(cgroup, 'sample', return_value=(parent, {})), \
                 mock.patch.object(cgroup, 'run_processes', return_value=matches):
                if matches:
                    proof = build.qualify_cgroup(sup, 'test', 'nonce', 'pinned', observed, work, recipe, journal.append)
                    self.assertEqual(proof['run_process'], child)
                    self.assertEqual(proof['owned_root'], binding['owned_root'])
                    self.assertTrue(proof['run_inherits_builder_limits'])
                else:
                    with self.assertRaisesRegex(ValueError, 'not observed'):
                        build.qualify_cgroup(sup, 'test', 'nonce', 'pinned', observed, work, recipe, journal.append)
        observed['HostConfig']['CgroupnsMode'] = 'host'
        with self.assertRaisesRegex(ValueError, 'Private builder'):
            build.qualify_cgroup(sup, 'test', 'nonce', 'pinned', observed, self.root, recipe, journal.append)

    def test_namespace_correlation_rejects_process_change_and_retains_observation(self):
        _, binding = self.cgroup_binding()
        parent = binding['builder_process']
        observed = self.builder()
        observed['HostConfig']['CgroupnsMode'] = 'private'
        observed['State']['Pid'] = 100
        sup = mock.Mock()
        sup.run.return_value = '0::/init'
        journal = []
        after = {**parent, 'starttime_ticks': '999'}
        with mock.patch.object(cgroup, 'process_identity', side_effect=[parent, after]), \
             self.assertRaisesRegex(ValueError, 'during namespace correlation'):
            build.qualify_cgroup(sup, 'test', 'nonce', 'pinned', observed, self.root, '', journal.append)
        self.assertEqual(journal, [{'kind': 'binding', 'container_id': observed['Id'],
                                   'host_before': parent, 'host_after': after, 'private_cgroup': '0::/init'}])

    def test_create_timeout_retains_intent_and_still_reconciles_owned_builder(self):
        env, event = self.event()
        env['RUNNER_TEMP'] = str(self.root)
        event_path = self.root / 'event.json'
        event_path.write_text(json.dumps(event))
        env['GITHUB_EVENT_PATH'] = str(event_path)
        fake = mock.Mock(minimum=12 * build.GIB)
        fake.check.return_value = 12 * build.GIB
        def run(command, **kwargs):
            if command[0] == 'git':
                return SOURCE
            if 'create' in command:
                raise subprocess.TimeoutExpired(command, 60)
            return 'version'
        fake.run.side_effect = run
        with mock.patch.dict(os.environ, env), mock.patch.object(build, 'Supervisor', return_value=fake), \
             mock.patch.object(build, 'prepare_context'), mock.patch.object(build, 'remove_builder', return_value={'absent': True}) as cleanup:
            with mock.patch.object(build, 'probe_volume', return_value=None), self.assertRaises(subprocess.TimeoutExpired):
                build.execute('public')
        cleanup.assert_called_once()
        record = json.loads((self.root / 'runner-image-public-123-1/build-receipt.json').read_text())
        self.assertTrue(record['builder_creation_started'])
        self.assertTrue(record['builder_removed'])
        self.assertFalse(record['build_success'])
        self.assertEqual(record['failure_type'], 'TimeoutExpired')

    def test_owned_cache_is_removed_even_after_container_is_inactive(self):
        for item in (self.builder(), None):
            sup = mock.Mock(env={'DOCKER_CONFIG': str(self.root / 'docker')})
            with mock.patch.object(build, 'probe_builder', side_effect=[item, None]), \
                 mock.patch.object(build, 'probe_volume', side_effect=[{'Name': 'state'}, None]), \
                 mock.patch.object(build.subprocess, 'run') as run:
                result = build.remove_builder(sup, 'test', 'nonce', 'pinned', volume_absent_before=True)
            self.assertTrue(result['state_volume_removed'])
            self.assertEqual(run.call_args_list[-1].args[0],
                             ['docker', 'volume', 'rm', 'buildx_buildkit_test0_state'])
            self.assertNotIn('--force', run.call_args_list[-1].args[0])
        with mock.patch.object(build, 'probe_builder', return_value=None), \
             mock.patch.object(build, 'probe_volume', return_value={'Name': 'state'}), \
             mock.patch.object(build.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'ownership'):
                build.remove_builder(sup, 'test', 'nonce', 'pinned')
        run.assert_not_called()

    def test_cache_removal_unknown_outcome_or_wrong_association_is_rejected(self):
        sup = mock.Mock(env={'DOCKER_CONFIG': str(self.root / 'docker')})
        with mock.patch.object(build, 'probe_builder', side_effect=[self.builder(), None]), \
             mock.patch.object(build, 'probe_volume', return_value={'Name': 'state'}), \
             mock.patch.object(build.subprocess, 'run'):
            with self.assertRaisesRegex(ValueError, 'volume removal not confirmed'):
                build.remove_builder(sup, 'test', 'nonce', 'pinned')
        bad = self.builder()
        bad['Mounts'][0]['Name'] = 'unrelated'
        with mock.patch.object(build, 'probe_builder', return_value=bad), \
             mock.patch.object(build.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'association'):
                build.remove_builder(sup, 'test', 'nonce', 'pinned')
        run.assert_not_called()

    def test_build_command_is_artifact_only_with_exact_private_index(self):
        data = build.document(build.MANIFEST)
        identity = {'source_commit': SOURCE, 'repository': 'victron-venus/venus-os-ci-toolkit'}
        command = build.build_command('private', data, identity, 'builder', self.root, self.root / 'out',
                                      self.root / 'layout', {'image_index': 'sha256:' + 'a' * 64})
        self.assertNotIn('--push', command)
        self.assertNotIn('--load', command)
        self.assertIn('BASE_IMAGE=accepted-base', command)
        self.assertTrue(any(arg.endswith('@sha256:' + 'a' * 64) for arg in command))
        with self.assertRaises(ValueError):
            build.build_command('private', data, identity, 'builder', self.root, self.root / 'out')

    def test_libbpf_preflight_binds_same_context_builder_and_target(self):
        data = build.document(build.MANIFEST)
        identity = {'source_commit': SOURCE, 'repository': 'victron-venus/venus-os-ci-toolkit'}
        name = 'runner-artifact-public-123-1'
        command = build.build_command('public', data, identity, name, self.root, self.root / 'out')
        record = {'command': command, 'run_id': '123', 'run_attempt': '1', 'libbpf_preflight_completed': True,
                  'libbpf_preflight_command': build.libbpf_preflight_command(data, name, self.root)}
        build.check_libbpf_preflight(record, data)
        self.assertIn('patched-libbpf', record['libbpf_preflight_command'])
        self.assertIn('type=cacheonly', record['libbpf_preflight_command'])
        record['libbpf_preflight_command'][-1] += '/other'
        with self.assertRaisesRegex(ValueError, 'preflight'):
            build.check_libbpf_preflight(record, data)

    def report(self, proof):
        return {'SchemaVersion': 2, 'ArtifactType': 'container_image',
                'Metadata': {'ImageID': proof['platform']['config']}, 'Results': [
                    {'Class': 'os-pkgs', 'Packages': [{'Name': 'fixture'}]},
                    {'Class': 'lang-pkgs', 'Packages': [{'Name': 'fixture'}], 'Vulnerabilities': [
                        {'VulnerabilityID': 'fixture', 'Severity': 'HIGH'}]}]}

    def test_scan_keeps_raw_findings_and_rejects_wrong_or_empty_inventory(self):
        proof = self.verify()
        report = self.report(proof)
        result = build.scan_summary(report, proof)
        self.assertEqual(result['blocking_findings'], 1)
        self.assertFalse(result['scan_gate_passed'])
        self.assertEqual(result['suppressed_findings'], 0)
        for edit in [lambda d: d.update(Results=[]), lambda d: d['Metadata'].update(ImageID='wrong'),
                     lambda d: d['Results'].pop(0)]:
            bad = copy.deepcopy(report)
            edit(bad)
            with self.assertRaises(ValueError):
                build.scan_summary(bad, proof)

    def assessment_report(self, proof):
        report = self.report(proof)
        report['ArtifactName'] = '/verified/input'
        report['Metadata']['OS'] = {'Family': 'ubuntu', 'Name': '24.04'}
        for result, kind, target, package, version, purl in [
            (report['Results'][0], 'ubuntu', '/verified/input (ubuntu 24.04)',
             'fixture-headers', '1.0', 'pkg:deb/ubuntu/fixture-headers@1.0'),
            (report['Results'][1], 'gobinary', 'usr/local/bin/fixture',
             'example.invalid/module', 'v1.0', 'pkg:golang/example.invalid/module@v1.0'),
        ]:
            identifier = package + '@' + version
            result.update(Type=kind, Target=target, Packages=[{
                'ID': identifier, 'Name': package, 'Version': version,
                'Identifier': {'PURL': purl}}], Vulnerabilities=[{
                    'VulnerabilityID': 'CVE-2099-12345', 'Severity': 'HIGH', 'PkgID': identifier,
                    'Layer': {'Digest': proof['platform']['layers'][0]['digest'],
                              'DiffID': proof['platform']['diff_ids'][0]},
                    'PkgName': package, 'InstalledVersion': version, 'PkgIdentifier': {'PURL': purl}}])
        return report

    def test_assessment_identity_preserves_all_raw_blockers(self):
        proof = self.verify()
        report = self.assessment_report(proof)
        before = copy.deepcopy(report)
        rows = build.assessment_findings(report, proof, '/verified/input')
        self.assertEqual(len(rows), 2)
        self.assertEqual(report, before)
        self.assertEqual(build.scan_summary(report, proof)['blocking_findings'], 2)
        self.assertFalse(build.scan_summary(report, proof)['scan_gate_passed'])
        self.assertEqual({row['target'] for row in rows},
                         {'/verified/input (ubuntu 24.04)', 'usr/local/bin/fixture'})
        self.assertFalse(any('assessed_not_affected' in row for row in rows))

    def test_advisory_hash_changes_for_new_details_not_image_location(self):
        proof = self.verify()
        report = self.assessment_report(proof)
        before = build.assessment_findings(report, proof, '/verified/input')[0]['advisory_sha256']
        finding = report['Results'][0]['Vulnerabilities'][0]
        finding['Fingerprint'] = 'different image fingerprint'
        self.assertEqual(build.assessment_findings(report, proof, '/verified/input')[0]['advisory_sha256'], before)
        finding['Description'] = 'New affected behavior requires another applicability review.'
        self.assertNotEqual(build.assessment_findings(report, proof, '/verified/input')[0]['advisory_sha256'], before)
        finding['Layer']['Digest'] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(ValueError, 'Finding layer'):
            build.assessment_findings(report, proof, '/verified/input')

    def test_assessment_requires_exact_scan_and_package_identity(self):
        proof = self.verify()
        report = self.assessment_report(proof)
        edits = [lambda d: d.update(ArtifactName='/other'),
                 lambda d: d['Results'][0].update(Target='/other (ubuntu 24.04)'),
                 lambda d: d['Results'][0]['Vulnerabilities'][0].pop('PkgIdentifier'),
                 lambda d: d['Results'][0]['Vulnerabilities'][0].update(InstalledVersion='2.0'),
                 lambda d: d['Results'][0]['Packages'][0].update(Identifier={'PURL': 'pkg:other'}),
                 lambda d: d['Results'][0]['Packages'].append(copy.deepcopy(d['Results'][0]['Packages'][0])),
                 lambda d: d['Results'][0]['Vulnerabilities'].append(copy.deepcopy(d['Results'][0]['Vulnerabilities'][0]))]
        for edit in edits:
            value = copy.deepcopy(report)
            edit(value)
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                build.assessment_findings(value, proof, '/verified/input')

    def test_assessment_retains_new_vulnerability_and_changed_version(self):
        proof = self.verify()
        report = self.assessment_report(proof)
        result = report['Results'][0]
        result['Packages'][0].update(Version='2', Release='7', ID='fixture-headers@2-7')
        result['Vulnerabilities'][0].update(InstalledVersion='2-7', PkgID='fixture-headers@2-7',
                                           VulnerabilityID='CVE-2099-98765')
        rows = build.assessment_findings(report, proof, '/verified/input')
        self.assertEqual(rows[0]['version'], '2-7')
        self.assertEqual(rows[0]['vulnerability'], 'CVE-2099-98765')
        self.assertEqual(len(rows), 2)

    def acceptance_fixture(self):
        """A small distinct policy exercises the full pure validator, not a bypass."""
        proof = self.verify()
        report = self.assessment_report(proof)
        report['Results'][1]['Vulnerabilities'][0]['VulnerabilityID'] = 'CVE-2099-12346'
        policy = copy.deepcopy(build.SCAN_POLICY)
        def file(content):
            return {'type': 'regular', 'bytes': len(content), 'mode': 420,
                    'sha256': hashlib.sha256(content).hexdigest(), 'md5': hashlib.md5(content).hexdigest(),
                    'elf': content.startswith(b'\x7fELF'), 'layer': proof['platform']['layers'][0]['digest']}
        header = file(b'synthetic userspace header\n')
        inventory = {'usr/include/fixture.h': {k: v for k, v in header.items() if k != 'layer'}}
        policy['headers']['inventory_sha256'] = build.stable_digest(inventory)
        policy['headers']['selected_header'] = {k: v for k, v in header.items() if k != 'layer'}
        headers = {**policy['headers'], 'inventory': inventory, 'file_layers': [header['layer']]}
        symbols, debian_symbols = '00000000 T fixture@@LIBBPF_1.0\n', ' fixture@LIBBPF_1.0 1.0\n'
        spec = policy['libbpf']
        spec['source_manifest']['abi']['debian_symbols_sha256'] = hashlib.sha256(debian_symbols.encode()).hexdigest()
        spec['fixed_files']['debian-libbpf1.symbols'] = spec['source_manifest']['abi']['debian_symbols_sha256']
        manifest = json.dumps(spec['source_manifest']).encode()
        spec['source_manifest_sha256'] = hashlib.sha256(manifest).hexdigest()
        library = file(b'\x7fELF synthetic fixed library')
        package_inventory = {'usr/lib/x86_64-linux-gnu/libbpf.so.1.3.0':
                             {k: v for k, v in library.items() if k != 'layer'}}
        package = {'package': 'libbpf1', 'architecture': 'amd64',
                   'version': spec['source_manifest']['downstream_binary_version'],
                   'inventory': package_inventory, 'inventory_sha256': build.stable_digest(package_inventory)}
        files = {name: {**file(b'fixture'), 'sha256': sha} for name, sha in spec['fixed_files'].items()}
        buildinfo = ('Format: 1.0\nChecksums-Sha256:\n ' + '1' * 64 + ' 123 libbpf1_' +
                     spec['source_manifest']['downstream_source_version'] + '_amd64.deb\n')
        files.update({'source-manifest.json': file(manifest), 'package.buildinfo': file(buildinfo.encode()),
                      'dynamic-symbols.txt': file(symbols.encode()),
                      'debian-libbpf1.symbols': file(debian_symbols.encode())})
        provenance = {'source': spec['source_manifest'], 'architecture': 'amd64', 'uid': 10001,
                      'soname': 'libbpf.so.1', 'ordinary_userspace_smoke_passed': True,
                      'kernel_bpf_operations': False, 'build_command': spec['build_command'],
                      'package_fields': ['Package: libbpf1', 'Version: ' + package['version'], 'Architecture: amd64'],
                      'package_sha256': '1' * 64, 'library_sha256': library['sha256'],
                      'buildinfo_sha256': files['package.buildinfo']['sha256'],
                      'symbol_proof_sha256': files['dynamic-symbols.txt']['sha256'], 'abi_symbol_count': 1,
                      'original_source_files': {'src/relo_core.c': [spec['source_manifest']['patch']['before_sha256'], 420]}}
        components = {'headers': headers, 'runc': {**policy['runc'], 'layer': header['layer']},
                      'libbpf': {'package': package, 'library': library, 'files': files, 'provenance': provenance,
                                'source_manifest': spec['source_manifest'], 'symbols': symbols, 'debian_symbols': debian_symbols}}
        components['libbpf']['buildinfo'] = buildinfo
        context = {'source_commit': SOURCE, 'run_id': '123', 'run_attempt': '1', 'stage': 'public',
                   'artifact_name': report['ArtifactName'], 'raw_scan_sha256': '1' * 64, 'scan_input_sha256': '2' * 64,
                   'scanner': {'kind': 'native', 'version': '0.74.0', 'binary_sha256': '3' * 64},
                   'databases': {'db/trivy.db': '4' * 64, 'db/metadata.json': '5' * 64}}
        binding = build.validate_scan_context(report, proof, context)
        evidence = {'schema': 1, 'binding': {k: binding[k] for k in (
            'source_commit', 'archive_sha256', 'archive_bytes', 'image_index', 'runtime_manifest', 'config_digest', 'platform')},
            'components': components, 'errors': [], 'kernel_paths': {}, 'perf_paths': {}}
        # Use the same report contract and exact identity matching as production.
        report['Results'][1]['Target'] = 'usr/local/bin/runc'
        policy['advisories'] = {}
        for row, component, rule in zip(build.assessment_findings(report, proof, context['artifact_name']),
                                       ('headers', 'runc'), ('headers_with_separately_repaired_libbpf', 'runc_affected_packages_not_linked')):
            policy['identities'][component] = {k: v for k, v in row.items() if k not in {
                'target', 'vulnerability', 'severity', 'layer', 'advisory_sha256', 'status', 'fixed_version'}}
            policy['advisories'][row['vulnerability']] = [component, row['advisory_sha256'], rule]
        return report, proof, evidence, context, policy

    def test_acceptance_preserves_raw_counts_and_binds_full_evidence(self):
        raw, proof, evidence, context, policy = self.acceptance_fixture()
        before = copy.deepcopy(raw)
        with mock.patch.object(build, 'SCAN_POLICY', policy):
            result = build.scan_acceptance(raw, proof, evidence, context)
        self.assertTrue(result['acceptance_passed'])
        self.assertEqual(result['raw_blocking_findings'], 2)
        self.assertEqual(len(result['assessed_not_affected']), 2)
        self.assertEqual(result['binding']['raw_scan_sha256'], context['raw_scan_sha256'])
        self.assertEqual(result['binding']['file_evidence_canonical_sha256'], build.stable_digest(evidence))
        self.assertEqual(raw, before)
        self.assertFalse(build.scan_summary(raw, proof)['scan_gate_passed'])

    def test_new_or_changed_findings_remain_unresolved(self):
        raw, proof, evidence, context, policy = self.acceptance_fixture()
        for edit in [lambda d: d['Results'][0]['Vulnerabilities'][0].update(Description='New affected behavior'),
                     lambda d: d['Results'][0]['Vulnerabilities'][0].update(VulnerabilityID='CVE-2099-98765'),
                     lambda d: d['Results'][1].update(Target='usr/local/bin/other')]:
            changed = copy.deepcopy(raw)
            edit(changed)
            with self.subTest(edit=edit), mock.patch.object(build, 'SCAN_POLICY', policy):
                result = build.scan_acceptance(changed, proof, evidence, context)
            self.assertFalse(result['acceptance_passed'])
            self.assertEqual(result['unresolved_count'], 1)
            self.assertEqual(result['raw_blocking_findings'], 2)

    def test_actual_component_changes_cannot_use_a_version_only_disposition(self):
        raw, proof, evidence, context, policy = self.acceptance_fixture()
        edits = [lambda e: e['components']['headers']['inventory'].clear(),
                 lambda e: e['components']['runc'].update(sha256='0' * 64),
                 lambda e: e['perf_paths'].update({'usr/bin/perf': {}}),
                 lambda e: e['components']['libbpf']['library'].update(sha256=policy['libbpf']['unpatched_library_sha256']),
                 lambda e: e['components']['libbpf']['provenance'].update(ordinary_userspace_smoke_passed=False),
                 lambda e: e['components']['libbpf']['provenance'].update(build_command=['make']),
                 lambda e: e['components']['libbpf'].update(symbols='different ABI\n')]
        for edit in edits:
            changed = copy.deepcopy(evidence)
            edit(changed)
            with self.subTest(edit=edit), mock.patch.object(build, 'SCAN_POLICY', policy):
                result = build.scan_acceptance(raw, proof, changed, context)
            self.assertFalse(result['acceptance_passed'])

    def test_zero_raw_findings_still_require_actual_libbpf_backport(self):
        raw, proof, evidence, context, policy = self.acceptance_fixture()
        for result in raw['Results']:
            result['Vulnerabilities'] = []
        evidence['components'].pop('libbpf')
        with mock.patch.object(build, 'SCAN_POLICY', policy):
            result = build.scan_acceptance(raw, proof, evidence, context)
        self.assertEqual(result['unresolved_count'], 0)
        self.assertEqual(result['failed_prerequisites'], ['installed_libbpf_backport_not_verified'])
        self.assertFalse(result['acceptance_passed'])

    def test_kernel_file_selection_preserves_name_and_extension_boundaries(self):
        _, proof, _, _, _ = self.acceptance_fixture()
        selected = ['boot/config', 'lib/modules/example', 'usr/lib/modules/example',
                    'vmlinux', 'usr/share/vmlinuz-6.8', 'usr/share/vmlinux.debug',
                    'module.ko', 'usr/share/module.ko.gz', 'module.ko.xz', 'module.ko.zst']
        unselected = ['usr/include/linux/a.h', 'not-vmlinux', 'usr/share/vmlinuzbackup',
                      'module.ko.txt', 'module.ko.gz.extra', 'module.kong', 'bootleg/file']
        tree = {name: {'type': 'regular'} for name in selected + unselected}
        tree['boot/directory'] = {'type': 'directory'}
        with mock.patch.object(oci, 'effective_files', return_value={'files': tree}):
            evidence = build.scan_file_evidence(None, None, proof, lambda: None)
        self.assertEqual(set(evidence['kernel_paths']), set(selected))

    def test_effective_layer_marks_dpkg_md5_as_nonsecurity(self):
        raw = layer_bytes([('usr/share/example', b'ordinary file')])
        descriptor = {'mediaType': oci.LAYER, 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest(),
                      'size': len(raw)}
        with mock.patch.object(oci.hashlib, 'md5', wraps=hashlib.md5) as md5:
            oci.read_effective_layer(io.BytesIO(raw), descriptor, len(raw), lambda: None)
        md5.assert_called_once_with(usedforsecurity=False)

    def test_acceptance_context_rejects_mixed_image_stage_or_database_identity(self):
        raw, proof, evidence, context, policy = self.acceptance_fixture()
        for key, value in [('source_commit', 'b' * 40), ('stage', 'private'), ('run_attempt', 1),
                           ('databases', {'db/trivy.db': '4' * 64}), ('raw_scan_sha256', 'not-a-hash')]:
            with self.subTest(key=key), mock.patch.object(build, 'SCAN_POLICY', policy), self.assertRaises(ValueError):
                build.scan_acceptance(raw, proof, evidence, {**context, key: value})
        evidence['binding']['config_digest'] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(ValueError, 'binding differs'):
            build.scan_acceptance(raw, proof, evidence, context)

    def test_policy_is_bound_to_public_rationales_and_backport_source(self):
        path = build.ROOT / 'docs/RUNNER_SCAN_APPLICABILITY.json'
        self.assertEqual(oci.digest_file(path), build.SCAN_POLICY['source_evidence']['public_rationale_document_sha256'])
        review = json.loads(path.read_text())
        rows = {row['id']: row for row in review['records']}
        self.assertEqual(set(rows), set(build.SCAN_POLICY['advisories']))
        for identifier, (_, digest, rule) in build.SCAN_POLICY['advisories'].items():
            self.assertEqual(rows[identifier]['advisory_content_sha256'], digest)
            self.assertEqual(rows[identifier]['rule_id'], rule)
            self.assertTrue(rows[identifier]['rationale'])
            self.assertTrue(rows[identifier]['source_document']['url'].startswith('https://'))
        self.assertNotIn('/Users/', path.read_text())
        self.assertEqual(oci.digest_file(build.ROOT / '.github/containers/runner-libbpf.json'),
                         build.SCAN_POLICY['libbpf']['source_manifest_sha256'])
        self.assertEqual(build.document(build.ROOT / '.github/containers/runner-libbpf.json'),
                         build.SCAN_POLICY['libbpf']['source_manifest'])

    def test_database_absence_does_not_disable_unreviewed_analyzers(self):
        raw, proof, _, context, _ = self.acceptance_fixture()
        raw['Results'].append({'Type': 'jar', 'Class': 'lang-pkgs', 'Packages': [{'Name': 'fixture'}]})
        with self.assertRaisesRegex(ValueError, 'Java database absence'):
            build.validate_scan_context(raw, proof, context)
        context['databases'].update({'java-db/trivy-java.db': '6' * 64, 'java-db/metadata.json': '7' * 64})
        build.validate_scan_context(raw, proof, context)

    def test_database_custody_preserves_original_bytes_and_rejects_partial_java(self):
        cache, output = self.root / 'cache', self.root / 'output'
        (cache / 'db').mkdir(parents=True)
        output.mkdir()
        (cache / 'db/trivy.db').write_bytes(b'synthetic db')
        metadata = b'{ "Version": 2 }\n'
        (cache / 'db/metadata.json').write_bytes(metadata)
        result = build.retain_scan_databases(cache, output, lambda: None)
        self.assertEqual((output / 'scan-database.json').read_bytes(), metadata)
        self.assertEqual(result['db/metadata.json'], hashlib.sha256(metadata).hexdigest())
        (cache / 'java-db').mkdir()
        (cache / 'java-db/trivy-java.db').write_bytes(b'synthetic java db')
        (output / 'scan-database.json').unlink()
        with self.assertRaises(FileNotFoundError):
            build.retain_scan_databases(cache, output, lambda: None)

    def test_scanner_uses_readonly_runtime_directory_and_preserves_full_findings(self):
        for stage in ('public', 'private'):
            with self.subTest(stage=stage):
                work, output = self.root / (stage + '-work'), self.root / stage
                work.mkdir()
                output.mkdir()
                path = output / 'image.oci.tar'
                sha, size = image(path, stage=stage, artifact_attestation=True, attestation_first=True)
                proof = oci.verify(path, sha, size, SOURCE, stage, build.SENTINEL)
                record = {'archive_sha256': sha, 'source_commit': SOURCE,
                          'run_id': '123', 'run_attempt': '1', 'stage': stage}
                sup = mock.Mock()
                def scan(command, **kwargs):
                    self.assertEqual(command[command.index('--input') + 1], '/input/image')
                    self.assertIn('type=bind,source=' + str(work / 'scan-layout') + ',target=/input/image,readonly', command)
                    for flag, value in [('--cpus', '2'), ('--memory', '4g'), ('--memory-swap', '4g'),
                                        ('--scanners', 'vuln'), ('--pkg-types', 'os,library'),
                                        ('--ignorefile', '/dev/null'), ('--exit-code', '0')]:
                        self.assertEqual(command[command.index(flag) + 1], value)
                    self.assertIn('--list-all-pkgs', command)
                    report = self.assessment_report(proof)
                    report['ArtifactName'] = '/input/image'
                    report['Results'][0]['Target'] = '/input/image (ubuntu 24.04)'
                    (work / 'scan-output/scan.json').write_text(json.dumps(report))
                    (work / 'scan-cache/db').mkdir()
                    (work / 'scan-cache/db/metadata.json').write_text('{"Version":2}')
                    (work / 'scan-cache/db/trivy.db').write_bytes(b'synthetic database')
                sup.run.side_effect = scan
                def cleanup(*args):
                    record.update(scanner_state={'ExitCode': 0, 'Running': False}, scanner_removed=True)
                with mock.patch.object(build, 'remove_scanner', side_effect=cleanup):
                    build.run_scan(sup, build.document(build.MANIFEST), work, output, 'owned', proof, record)
                self.assertTrue(record['scan_completed'])
                self.assertFalse(record['scan_gate_passed'])
                self.assertFalse(record['scan_acceptance_passed'])
                self.assertEqual((output / 'scan-database.json').read_bytes(), b'{"Version":2}')
                accepted = build.check_scan_acceptance(output, record, proof, build.document(build.MANIFEST))
                self.assertEqual(accepted['unresolved_count'], 2)
                self.assertIn('installed_libbpf_backport_not_verified', accepted['failed_prerequisites'])
                forged = copy.deepcopy(accepted)
                forged['acceptance_passed'] = True
                (output / 'scan-acceptance.json').write_text(json.dumps(forged))
                record['scan_acceptance_sha256'] = oci.digest_file(output / 'scan-acceptance.json')
                record['scan_acceptance_passed'] = True
                with self.assertRaisesRegex(ValueError, 'acceptance changed'):
                    build.check_scan_acceptance(output, record, proof, build.document(build.MANIFEST))
                self.assertEqual(oci.digest_file(path), sha)
                self.assertFalse((work / 'scan-layout').exists())
                build.check_scan_receipt(output, record, proof)
                altered = json.loads((output / 'scan-input.json').read_text())
                altered['config']['digest'] = 'sha256:' + '4' * 64
                (output / 'scan-input.json').write_text(json.dumps(altered))
                record['scan_input_sha256'] = oci.digest_file(output / 'scan-input.json')
                with self.assertRaisesRegex(ValueError, 'configuration or layers'):
                    build.check_scan_receipt(output, record, proof)

    def test_parent_from_different_attempt_is_rejected_before_image_use(self):
        parent = self.root / 'parent'
        parent.mkdir()
        identity = {'source_commit': SOURCE, 'run_id': '1', 'run_attempt': '2', 'repository': 'example/public'}
        (parent / 'build-receipt.json').write_text(json.dumps({**identity, 'run_attempt': '1'}))
        with self.assertRaisesRegex(ValueError, 'source/run/attempt'):
            build.validate_parent(parent, identity, build.document(build.MANIFEST))

    def test_optimized_python_keeps_validation(self):
        result = subprocess.run([sys.executable, '-O', '-c',
            'from scripts.runner_image_oci import config_contract; config_contract({},"a"*40,"public")'],
            cwd=build.ROOT, capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_workflow_has_only_manual_sequential_artifact_jobs(self):
        text = (build.ROOT / '.github/workflows/runner-image-artifacts.yml').read_text()
        self.assertIn('workflow_dispatch: {}', text)
        self.assertIn('needs: public', text)
        self.assertNotIn('pull_request:', text)
        self.assertNotIn('secrets.', text)
        self.assertNotIn('packages: write', text)
        self.assertNotIn('registry', text)
        self.assertLess(text.index('name: Preserve complete artifact or failure evidence', text.index('  private:')),
                        text.index('name: Require both full scans'))
