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
          provenance=True, wrong_diff=False, edit=None, additional=()):
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
    manifest = blob({'schemaVersion': 2, 'config': conf, 'layers': layers}, oci.MANIFEST)
    manifest['platform'] = {'architecture': 'amd64', 'os': 'linux'}
    manifests = [manifest]
    if provenance:
        statement = blob({'_type': 'https://in-toto.io/Statement/v0.1',
            'subject': [{'name': 'image', 'digest': {'sha256': manifest['digest'][7:]}}],
            'predicateType': 'https://slsa.dev/provenance/v0.2', 'predicate': {}}, 'application/vnd.in-toto+json')
        att = blob({'schemaVersion': 2, 'config': blob({'os': 'unknown', 'architecture': 'unknown'}, oci.CONFIG),
                    'layers': [statement]}, oci.MANIFEST)
        att['annotations'] = {'vnd.docker.reference.type': 'attestation-manifest'}
        manifests.append(att)
    root = blob({'schemaVersion': 2, 'manifests': manifests}, oci.INDEX)
    if extra:
        blob(b'orphan', oci.LAYER)
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

    def test_different_ca_whiteout_missing_provenance_and_bad_diff_fail(self):
        changes = [{'content': b'not our sentinel'}, {'provenance': False}, {'wrong_diff': True}, {'extra': True},
                   {'additional': [[('opt/victron-ci-reserve/.wh.guard-ca.crt', b'')]]},
                   {'additional': [[('opt/victron-ci-reserve/.wh..wh..opq', b'')]]},
                   {'additional': [[('.wh..wh..opq', b'')]]}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify(**change)

    def test_source_root_user_and_credentials_fail(self):
        for edit in [lambda c: c['config'].update(User='0:0'),
                     lambda c: c['config']['Labels'].update({'org.opencontainers.image.revision': 'b' * 40}),
                     lambda c: c['config']['Env'].append('BOT_PAT=synthetic')]:
            with self.assertRaises(ValueError):
                self.verify(edit=edit)

    def test_private_requires_exact_base_layers_and_configuration(self):
        base = self.verify()
        path = self.root / 'private.tar'
        sha, size = image(path, stage='private', additional=[[('extra-file', b'private tooling')]])
        proof = oci.verify(path, sha, size, SOURCE, 'private', build.SENTINEL, base)
        self.assertEqual(proof['platform']['layers'][:1], base['platform']['layers'])
        wrong = copy.deepcopy(base)
        wrong['platform']['diff_ids'] = ['sha256:' + 'f' * 64]
        with self.assertRaisesRegex(ValueError, 'public layers'):
            oci.verify(path, sha, size, SOURCE, 'private', build.SENTINEL, wrong)
        sha, size = image(path, stage='private', edit=lambda c: c['config']['Env'].append('NEW_POLICY=1'))
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

    def builder(self):
        return {'Id': 'a' * 64, 'Name': '/buildx_buildkit_test0',
                'Config': {'Image': 'pinned', 'Env': ['RUNNER_ARTIFACT_OWNER=nonce']},
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
            with mock.patch.object(build.subprocess, 'run', side_effect=results) as run:
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

    def test_actual_cgroup_limits_and_run_inheritance_are_required(self):
        marker = 'runner-proof-' + 'a' * 32
        proc = self.cgroup_fixture(100, '/docker/bounded')
        self.cgroup_fixture(101, '/docker/bounded/buildkit/child', marker)
        groups = self.root / 'cgroup'
        directory = groups / 'docker/bounded'
        directory.mkdir(parents=True)
        (groups / 'cgroup.controllers').write_text('cpu memory')
        expected = {'cpu.max': '200000 100000', 'memory.max': str(4 * build.GIB), 'memory.swap.max': '0'}
        for key, value in expected.items():
            (directory / key).write_text(value)
        parent, limits = cgroup.sample(100, proc=proc, cgroups=groups)
        self.assertEqual(limits, expected)
        self.assertEqual(cgroup.run_processes(marker, parent, proc)[0]['pid'], 101)
        (proc / '101/cgroup').write_text('0::/docker/unbounded\n')
        with self.assertRaisesRegex(ValueError, 'outside'):
            cgroup.run_processes(marker, parent, proc)
        for key in expected:
            (directory / key).write_text('max')
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'limits differ'):
                cgroup.sample(100, parent, proc, groups)
            (directory / key).write_text(expected[key])
        (proc / '100/stat').write_text('100 (new process) ' + ' '.join(['S'] + ['0'] * 18 + ['999']))
        with self.assertRaisesRegex(ValueError, 'process changed'):
            cgroup.sample(100, parent, proc, groups)
        with self.assertRaisesRegex(ValueError, 'cgroup v2'):
            cgroup.group_path('2:memory:/docker/bounded')

    def test_cgroup_preflight_requires_a_real_marked_run(self):
        parent = {'pid': 100, 'starttime_ticks': '12', 'cgroup': '/docker/owned'}
        child = {'pid': 101, 'starttime_ticks': '13', 'cgroup': '/docker/owned/buildkit/run'}
        observed = self.builder()
        observed['HostConfig']['CgroupnsMode'] = 'private'
        observed['State']['Pid'] = 100
        recipe = 'FROM docker.io/library/ubuntu@sha256:' + 'a' * 64 + '\n'
        sup = mock.Mock()
        def run(command, **kwargs):
            if command[:2] == ['docker', 'exec']:
                return '0::/'
            kwargs['inspect']()
        sup.run.side_effect = run
        for matches in ([], [child]):
            work = self.root / ('missing' if not matches else 'seen')
            work.mkdir()
            with mock.patch.object(build, 'inspect_builder', return_value=observed), \
                 mock.patch.object(cgroup, 'sample', return_value=(parent, {})), \
                 mock.patch.object(cgroup, 'run_processes', return_value=matches):
                if matches:
                    proof = build.qualify_cgroup(sup, 'test', 'nonce', 'pinned', observed, work, recipe)
                    self.assertEqual(proof['run_process'], child)
                    self.assertTrue(proof['run_inherits_builder_limits'])
                else:
                    with self.assertRaisesRegex(ValueError, 'not observed'):
                        build.qualify_cgroup(sup, 'test', 'nonce', 'pinned', observed, work, recipe)
        observed['HostConfig']['CgroupnsMode'] = 'host'
        with self.assertRaisesRegex(ValueError, 'Private builder'):
            build.qualify_cgroup(sup, 'test', 'nonce', 'pinned', observed, self.root, recipe)

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
            with self.assertRaises(subprocess.TimeoutExpired):
                build.execute('public')
        cleanup.assert_called_once()
        record = json.loads((self.root / 'runner-image-public-123-1/build-receipt.json').read_text())
        self.assertTrue(record['builder_creation_started'])
        self.assertTrue(record['builder_removed'])
        self.assertFalse(record['build_success'])
        self.assertEqual(record['failure_type'], 'TimeoutExpired')

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
