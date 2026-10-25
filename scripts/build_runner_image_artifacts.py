#!/usr/bin/env python3
"""Build fixed, nondeployable runner OCI artifacts on a disposable hosted worker.

No registry push, live credentials, site certificates or deployment operations.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import time
import uuid

if __package__:
    from . import runner_image_cgroup as cgroup
    from . import runner_image_oci as oci
else:
    import runner_image_cgroup as cgroup
    import runner_image_oci as oci

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / '.github/containers/runner-artifacts.json'
GIB = 1024**3
RESERVE = 2 * GIB
START_FREE = 10 * GIB
SENTINEL = b'UNCONFIGURED SITE CA\n'
OWNER = 'io.victron.runner-artifact-owner'
LIMITS = {'Memory': 4 * GIB, 'MemorySwap': 4 * GIB,
          'CpuQuota': 200000, 'CpuPeriod': 100000}
require = oci.require
save = oci.save


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def document(path, maximum=8 * 1024**2):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= maximum, 'Unsafe metadata')
        raw = stream.read(maximum + 1)
    require(len(raw) == info.st_size, 'Metadata changed')
    return oci.json_document(raw)


def validate_event(env, event):
    require(env.get('GITHUB_ACTIONS') == 'true' and env.get('RUNNER_ENVIRONMENT') == 'github-hosted',
            'Disposable GitHub-hosted worker required')
    require(env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and env.get('RUNNER_ARCH') == 'X64'
            and env.get('RUNNER_OS') == 'Linux', 'Manual native Linux dispatch required')
    repo = event.get('repository', {})
    require(repo.get('private') is False and repo.get('full_name') == env.get('GITHUB_REPOSITORY'),
            'Public repository identity differs')
    require('https://github.com/' + repo['full_name'] == oci.SOURCE_URL, 'Canonical toolkit repository required')
    require(repo.get('default_branch') and env.get('GITHUB_REF') == 'refs/heads/' + repo['default_branch'],
            'Default branch required')
    require(re.fullmatch(r'[0-9a-f]{40}', env.get('GITHUB_SHA', '')), 'Full source identity required')
    for key in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'):
        require(re.fullmatch(r'[1-9][0-9]*', env.get(key, '')), 'Invalid workflow identity')
    return {'source_commit': env['GITHUB_SHA'], 'run_id': env['GITHUB_RUN_ID'],
            'run_attempt': env['GITHUB_RUN_ATTEMPT'], 'repository': repo['full_name']}


def clean_env(home):
    # Neither Actions auth nor host Docker/Git credentials enter build processes.
    return {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
            'HOME': str(home), 'DOCKER_CONFIG': str(home / 'docker'),
            'DOCKER_HOST': 'unix:///var/run/docker.sock', 'LANG': 'C.UTF-8',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '/bin/false'}


def validate_manifest(data):
    require(data.get('schema') == 1 and data.get('platform') == 'linux/amd64', 'Unexpected build manifest')
    require(re.fullmatch(r'[0-9a-f]{40}', data.get('canonical_source', '')), 'Canonical source missing')
    require(data.get('sentinel') == {'path': 'deploy/runner-reserve/guard-ca.crt',
            'installed_path': '/opt/victron-ci-reserve/guard-ca.crt', 'text': SENTINEL.decode()},
            'Only invalid site sentinel allowed')
    require(set(data.get('contexts', {})) == {'public', 'private'}, 'Unexpected stages')
    for name, spec in data['contexts'].items():
        recipe = 'deploy/' + ('runner-reserve' if name == 'public' else 'arc-private') + '/Dockerfile'
        require(spec['recipe'] == recipe and recipe in spec['files'], 'Canonical recipe differs')
        for path, item in spec['files'].items():
            require(not Path(path).is_absolute() and '..' not in Path(path).parts and
                    not any(c in path for c in '*?[]\\') and item['mode'] in {'100644', '100755'} and
                    re.fullmatch(r'[0-9a-f]{64}', item['sha256']) and 0 < item['bytes'] < 2 * 1024**2,
                    'Unsafe context declaration')
    require(set(data['tools']) == {'buildkit', 'trivy'}, 'Unexpected tool inventory')
    for name, prefix in [('buildkit', 'docker.io/moby/buildkit'), ('trivy', 'ghcr.io/aquasecurity/trivy')]:
        require(re.fullmatch(re.escape(prefix) + r'@sha256:[0-9a-f]{64}', data['tools'][name]['image']),
                'Immutable official tool image required')


def prepare_context(root, destination, spec, sentinel, env):
    """Read only committed blobs and copy the exact reviewed sparse inventory."""
    destination.mkdir(mode=0o700)
    for name, expected in spec['files'].items():
        tree = subprocess.check_output(['git', '-C', str(root), 'ls-tree', 'HEAD', '--', name], env=env, timeout=20).decode()
        require(tree.startswith(expected['mode'] + ' blob ') and tree.endswith('\t' + name + '\n'),
                'Context file mode or identity differs')
        raw = subprocess.check_output(['git', '-C', str(root), 'show', 'HEAD:' + name], env=env, timeout=20)
        require(len(raw) == expected['bytes'] and hashlib.sha256(raw).hexdigest() == expected['sha256'],
                'Canonical context differs from reviewed source')
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        target.chmod(0o755 if expected['mode'] == '100755' else 0o644)
    if sentinel:
        target = destination / 'deploy/runner-reserve/guard-ca.crt'
        target.write_bytes(SENTINEL)
        target.chmod(0o644)
    lines = (destination / (spec['recipe'] + '.dockerignore')).read_text().splitlines()
    actual = {line[1:] for line in lines if line.startswith('!')}
    require(actual == set(spec['files']) | ({'deploy/runner-reserve/guard-ca.crt'} if sentinel else set()),
            'Dockerignore differs from sparse inventory')
    require([line for line in lines if line and not line.startswith('#')][0] == '**',
            'Context must deny all undeclared inputs')


class Supervisor:
    """Finite owned commands with explicit disk observations; never prunes host state."""
    def __init__(self, env, paths, log, disk_log):
        self.env, self.paths, self.log, self.disk_log = env, paths, log, disk_log
        self.minimum = None

    def check(self, floor=RESERVE):
        free = {str(path): shutil.disk_usage(path).free for path in self.paths}
        minimum = min(free.values())
        self.minimum = minimum if self.minimum is None else min(self.minimum, minimum)
        with self.disk_log.open('a') as out:
            out.write(json.dumps({'at': now(), 'free_bytes': free, 'required_bytes': floor}) + '\n')
        require(minimum >= floor, 'Disk reserve exhausted')
        return minimum

    def run(self, command, seconds=60, capture=False, inspect=None):
        self.check()
        if capture:
            result = subprocess.run(command, env=self.env, capture_output=True, timeout=seconds, check=True)
            self.check()
            require(len(result.stdout) <= 2 * 1024**2, 'Metadata response too large')
            return result.stdout.decode().strip()
        with self.log.open('ab') as output:
            process = subprocess.Popen(command, env=self.env, stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            deadline = time.monotonic() + seconds
            try:
                while process.poll() is None:
                    require(time.monotonic() < deadline, 'Owned command deadline exceeded')
                    self.check()
                    if inspect:
                        inspect()
                    time.sleep(5)
                require(process.returncode == 0, 'Owned command failed: ' + str(process.returncode))
                self.check()
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)


def builder_identity(item, name, owner, image):
    require(item.get('Name') == '/buildx_buildkit_' + name + '0' and
            item.get('Config', {}).get('Image') == image and
            'RUNNER_ARTIFACT_OWNER=' + owner in item.get('Config', {}).get('Env', []) and
            re.fullmatch(r'[0-9a-f]{64}', item.get('Id', '')), 'Foreign builder identity')
    require(all(item.get('HostConfig', {}).get(k) == v for k, v in LIMITS.items()), 'Builder resource limits differ')
    require(item.get('HostConfig', {}).get('RestartPolicy', {}).get('Name') == 'no', 'Builder restart policy differs')
    return item['Id']


def container_absent(result, name):
    expected = (('Error: No such object: ' + name).encode(),
                ('Error response from daemon: No such container: ' + name).encode())
    return result.returncode == 1 and result.stdout.strip() in {b'', b'[]'} and result.stderr.strip() in expected


def probe_builder(sup, name, owner, image):
    # This path deliberately bypasses ordinary admission: a full disk or OOM
    # must not prevent deletion of an independently identified owned process.
    result = subprocess.run(['docker', 'inspect', 'buildx_buildkit_' + name + '0'],
                            env=sup.env, capture_output=True, timeout=30)
    if container_absent(result, 'buildx_buildkit_' + name + '0'):
        return None
    require(result.returncode == 0, 'Unable to determine builder ownership')
    items = json.loads(result.stdout)
    require(len(items) == 1, 'Ambiguous builder')
    builder_identity(items[0], name, owner, image)
    return items[0]


def inspect_builder(sup, name, owner, image):
    item = probe_builder(sup, name, owner, image)
    require(item is not None, 'Owned builder is missing')
    require(not item['State'].get('OOMKilled'), 'Builder was OOM-killed')
    return item


def qualify_cgroup(sup, name, owner, image, observed, work, public_recipe, observe):
    require(observed['HostConfig'].get('CgroupnsMode') == 'private', 'Private builder cgroup namespace required')
    pid = observed['State']['Pid']
    identity = cgroup.process_identity(pid)
    # PID 1 is the same process as Docker State.Pid, unlike the short-lived
    # `docker exec cat` process. BuildKit may move the daemon into /init while
    # both it and RUN workers still inherit the Docker-owned root's limits.
    inside = sup.run(['docker', 'exec', observed['Id'], 'cat', '/proc/1/cgroup'], capture=True)
    after = cgroup.process_identity(pid)
    observe({'kind': 'binding', 'container_id': observed['Id'], 'host_before': identity,
             'host_after': after, 'private_cgroup': inside})
    require(after == identity, 'Builder process changed during namespace correlation')
    private_path = cgroup.group_path(inside)
    binding = {'container_id': observed['Id'], 'builder_process': identity,
               'private_process_path': private_path,
               'owned_root': cgroup.owned_root(observed['Id'], identity, private_path)}
    _, limits = cgroup.sample(pid, binding, observe=observe)
    bases = re.findall(r'^FROM (docker.io/library/ubuntu@sha256:[0-9a-f]{64})$', public_recipe, re.M)
    require(len(bases) == 1, 'Canonical preflight Ubuntu base missing')
    context = work / 'cgroup-preflight'
    context.mkdir()
    marker = 'runner-proof-' + uuid.uuid4().hex
    # The shell deliberately remains alive so a host-side read-only observer
    # can see the actual RUN process, not just the BuildKit daemon settings.
    (context / 'Dockerfile').write_text('FROM ' + bases[0] + '\nRUN ' +
        json.dumps(['/bin/sh', '-c', 'sleep 20; :', marker]) + '\n')
    seen = {}
    def check():
        item = inspect_builder(sup, name, owner, image)
        require(item['Id'] == observed['Id'], 'Builder identity changed')
        cgroup.sample(item['State']['Pid'], binding, observe=observe)
        for child in cgroup.run_processes(marker, binding):
            seen[(child['pid'], child['starttime_ticks'])] = child
    sup.run(['docker', 'buildx', 'build', '--builder', name, '--platform', 'linux/amd64',
             '--progress', 'plain', '--no-cache', str(context)], seconds=180, inspect=check)
    check()
    require(len(seen) == 1, 'Actual bounded RUN process was not observed')
    return {**binding, 'cgroup_version': 2, 'namespace': 'private',
            'effective_limits': limits, 'run_process': next(iter(seen.values())),
            'preflight_base': bases[0], 'run_inherits_builder_limits': True}


def state_volume(name):
    return 'buildx_buildkit_' + name + '0_state'


def probe_volume(sup, name):
    result = subprocess.run(['docker', 'volume', 'inspect', name], env=sup.env,
                            capture_output=True, timeout=30)
    missing = {('Error response from daemon: get ' + name + ': no such volume').encode(),
               ('Error: No such volume: ' + name).encode()}
    if result.returncode == 1 and result.stdout.strip() in {b'', b'[]'} and result.stderr.strip() in missing:
        return None
    require(result.returncode == 0, 'Unable to determine owned volume state')
    items = json.loads(result.stdout)
    require(len(items) == 1 and items[0].get('Name') == name and items[0].get('Driver') == 'local' and
            items[0].get('Scope') == 'local' and not items[0].get('Options'), 'Unexpected state volume')
    return items[0]


def remove_builder(sup, name, owner, image, *, volume_absent_before=False):
    item = probe_builder(sup, name, owner, image)
    volume = state_volume(name)
    associated = False
    if item is not None:
        mounts = [mount for mount in item.get('Mounts', []) if mount.get('Destination') == '/var/lib/buildkit']
        require(len(mounts) == 1 and mounts[0].get('Type') == 'volume' and mounts[0].get('Name') == volume and
                mounts[0].get('RW') is True, 'Owned BuildKit state volume association differs')
        associated = True
        # Remove by immutable container ID, including an unhealthy owned instance.
        subprocess.run(['docker', 'rm', '--force', item['Id']], env=sup.env, check=True, timeout=60)
    metadata = Path(sup.env['DOCKER_CONFIG']) / 'buildx/instances' / name
    if metadata.exists():
        require(metadata.is_file() and not metadata.is_symlink(), 'Unsafe owned builder metadata')
        # This private DOCKER_CONFIG was exclusively created by this invocation.
        # Remove only that instance's metadata. An inactive Buildx instance may
        # retain its volume, so cache removal is independently verified below.
        require(probe_builder(sup, name, owner, image) is None, 'Builder name reappeared during cleanup')
        subprocess.run(['docker', 'buildx', 'rm', '--force', name], env=sup.env, check=True, timeout=60)
    require(probe_builder(sup, name, owner, image) is None, 'Owned builder removal not confirmed')
    if probe_volume(sup, volume) is not None:
        require(associated or volume_absent_before, 'State volume ownership is not established')
        # No force: Docker refuses removal if another container references it.
        subprocess.run(['docker', 'volume', 'rm', volume], env=sup.env, check=True, timeout=60)
    require(probe_volume(sup, volume) is None, 'Owned state volume removal not confirmed')
    result = {key: item['State'].get(key) for key in ('OOMKilled', 'ExitCode', 'Error')} if item else {'absent': True}
    return {**result, 'state_volume': volume, 'state_volume_removed': True}


def build_command(stage, data, identity, builder, context, output, layout=None, base=None):
    command = ['docker', 'buildx', 'build', '--builder', builder, '--platform', 'linux/amd64',
               '--progress', 'plain', '--provenance=mode=max', '--network=default',
               '--label', 'org.opencontainers.image.revision=' + identity['source_commit'],
               '--label', 'org.opencontainers.image.source=https://github.com/' + identity['repository'],
               '--label', 'io.victron.runner.stage=' + stage,
               '--output', 'type=oci,dest=' + str(output), '--tag', 'localhost/runner-' + stage + ':artifact',
               '--file', str(context / data['contexts'][stage]['recipe'])]
    if stage == 'private':
        require(layout is not None and base is not None, 'Accepted public base required')
        command += ['--build-context', 'accepted-base=oci-layout://' + str(layout) + '@' + base['image_index'],
                    '--build-arg', 'BASE_IMAGE=accepted-base']
    return command + [str(context)]


def scan_summary(data, proof):
    require(data.get('SchemaVersion') == 2 and isinstance(data.get('Results'), list) and data['Results'],
            'Incomplete image vulnerability report')
    require(data.get('ArtifactType') == 'container_image', 'Scanner did not analyze an image')
    image_id = data.get('Metadata', {}).get('ImageID')
    require(image_id in {proof['platform']['config'], proof['platform']['config'][7:]}, 'Scan image identity differs')
    results = data['Results']
    require(any(r.get('Class') == 'os-pkgs' and r.get('Packages') for r in results), 'OS inventory absent')
    require(any(r.get('Class') == 'lang-pkgs' and r.get('Packages') for r in results), 'Language inventory absent')
    counts = {}
    for result in results:
        for finding in result.get('Vulnerabilities') or []:
            level = finding.get('Severity', 'UNKNOWN')
            counts[level] = counts.get(level, 0) + 1
    return {'schema': 1, 'counts': counts, 'blocking_findings': counts.get('HIGH', 0) + counts.get('CRITICAL', 0),
            'scan_gate_passed': not (counts.get('HIGH', 0) + counts.get('CRITICAL', 0)),
            'suppressed_findings': 0, 'packages': sum(len(r.get('Packages') or []) for r in results),
            'targets': len(results), 'deployable': False, 'site_configured': False}


def check_cgroup_receipt(build):
    proof = build.get('cgroup_proof', {})
    require(proof.get('cgroup_version') == 2 and proof.get('namespace') == 'private' and
            proof.get('run_inherits_builder_limits') is True and
            proof.get('effective_limits') == {'cpu.max': '200000 100000',
                'memory.max': str(4 * GIB), 'memory.swap.max': '0'}, 'Cgroup enforcement evidence missing')
    require(proof['container_id'] == build['builder_id'], 'Cgroup proof Docker identity differs')
    cgroup.require_run_inside(proof['run_process'], proof)


def validate_parent(parent, identity, manifest):
    build = document(parent / 'build-receipt.json')
    for key, value in identity.items():
        require(build.get(key) == value, 'Public artifact belongs to another source/run/attempt')
    require(build.get('stage') == 'public' and build.get('build_success') is True and
            build.get('oci_verified') is True and build.get('scan_completed') is True and
            build.get('builder_removed') is True and build.get('scanner_removed') is True,
            'Public artifact is incomplete')
    require(build.get('builder_cleanup_state', {}).get('state_volume_removed') is True,
            'Public builder cache removal is unconfirmed')
    check_cgroup_receipt(build)
    require(document(parent / 'cgroup-verification.json') == build['cgroup_proof'], 'Parent cgroup proof differs')
    require(build.get('manifest_sha256') == oci.digest_file(MANIFEST), 'Public manifest binding differs')
    require(build.get('canonical_context') == manifest['contexts']['public']['files'] and
            build.get('canonical_source') == manifest['canonical_source'], 'Parent canonical inputs differ')
    proof = oci.verify(parent / 'image.oci.tar', build['archive_sha256'], build['archive_bytes'],
                       identity['source_commit'], 'public', SENTINEL)
    require(proof == document(parent / 'oci-verification.json'), 'Public OCI evidence differs')
    raw_scan = document(parent / 'scan.json', 128 * 1024**2)
    require(oci.digest_file(parent / 'scan.json') == build['scan_sha256'], 'Public scan changed')
    require(scan_summary(raw_scan, proof) == document(parent / 'scan-summary.json'), 'Public scan summary differs')
    return proof


def execute(stage, parent=None):
    identity = validate_event(os.environ, document(Path(os.environ['GITHUB_EVENT_PATH'])))
    data = document(MANIFEST)
    validate_manifest(data)
    temporary = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    work = temporary / ('runner-image-work-' + stage + '-' + identity['run_id'] + '-' + identity['run_attempt'])
    output = temporary / ('runner-image-' + stage + '-' + identity['run_id'] + '-' + identity['run_attempt'])
    work.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    (work / 'home').mkdir(mode=0o700)
    env = clean_env(work / 'home')
    record = {**identity, 'schema': 1, 'stage': stage, 'started_at': now(), 'build_success': False,
              'oci_verified': False, 'scan_completed': False, 'builder_removed': False, 'scanner_removed': False,
              'deployable': False, 'site_configured': False, 'keys_transferred': False, 'automatic_retry': False,
              'canonical_source': data['canonical_source'], 'canonical_context': data['contexts'][stage]['files'],
              'manifest_sha256': oci.digest_file(MANIFEST), 'limits': LIMITS,
              'executor_sha256': oci.digest_file(Path(__file__)),
              'cgroup_observer_sha256': oci.digest_file(Path(cgroup.__file__)),
              'verifier_sha256': oci.digest_file(Path(oci.__file__)), 'sentinel_sha256': hashlib.sha256(SENTINEL).hexdigest()}
    # Docker and containerd may have separate filesystems. Measure each existing root.
    paths = [temporary, ROOT] + [Path(p) for p in ('/var/lib/docker', '/var/lib/containerd') if Path(p).exists()]
    sup = Supervisor(env, paths, output / 'build.log', output / 'disk-observations.jsonl')
    name = 'runner-artifact-' + stage + '-' + identity['run_id'] + '-' + identity['run_attempt']
    image = data['tools']['buildkit']['image']
    owner = name
    created = False
    volume_absent_before = False
    def observe_cgroup(observation):
        with (output / 'cgroup-observations.jsonl').open('a') as stream:
            stream.write(json.dumps({'at': now(), **observation}, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
    try:
        require(sup.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture=True) == identity['source_commit'],
                'Checkout differs from dispatched commit')
        record['initial_free_bytes'] = sup.check(START_FREE)
        record['docker_version'] = sup.run(['docker', '--version'], capture=True)
        record['buildx_version'] = sup.run(['docker', 'buildx', 'version'], capture=True)
        context = work / 'context'
        prepare_context(ROOT, context, data['contexts'][stage], stage == 'public', env)
        base, layout = None, None
        if stage == 'private':
            require(parent is not None and parent.resolve().is_relative_to(temporary), 'Unexpected parent artifact directory')
            base = validate_parent(parent, identity, data)
            layout = work / 'public-layout'
            sup.check(RESERVE + base['archive_bytes'])
            oci.materialize(parent / 'image.oci.tar', base, layout, sup.check)
            record['base_index'] = base['image_index']
            record['base_archive_sha256'] = base['archive_sha256']
            # Original remains durably in the prior job's artifact. Remove this owned
            # download only after complete validation/materialization to avoid 3 copies.
            (parent / 'image.oci.tar').unlink()
        config = work / 'buildkitd.toml'
        config.write_text('[worker.oci]\n  max-parallelism = 1\n')
        require(probe_volume(sup, state_volume(name)) is None, 'Builder state volume already exists')
        volume_absent_before = True
        record['state_volume_absent_before_creation'] = True
        command = ['docker', 'buildx', 'create', '--name', name, '--driver', 'docker-container',
                   '--buildkitd-config', str(config), '--driver-opt', 'image=' + image,
                   '--driver-opt', 'memory=4g', '--driver-opt', 'memory-swap=4g',
                   '--driver-opt', 'cpu-period=100000', '--driver-opt', 'cpu-quota=200000',
                   '--driver-opt', 'restart-policy=no', '--driver-opt', 'env.RUNNER_ARTIFACT_OWNER=' + owner]
        # A timeout may occur after Docker/Buildx accepted creation. Reconcile
        # this exact owned name in finally even if the create client fails.
        record['builder_creation_started'] = True
        created = True
        sup.run(command)
        record['builder_created'] = True
        sup.run(['docker', 'buildx', 'inspect', '--builder', name, '--bootstrap'], seconds=180)
        observed = inspect_builder(sup, name, owner, image)
        record['builder_id'] = observed['Id']
        record['builder_image'] = image
        public_recipe = subprocess.check_output(['git', '-C', str(ROOT), 'show',
            'HEAD:deploy/runner-reserve/Dockerfile'], env=env, timeout=20).decode()
        public_hash = hashlib.sha256(public_recipe.encode()).hexdigest()
        require(public_hash == data['contexts']['public']['files']['deploy/runner-reserve/Dockerfile']['sha256'],
                'Preflight source differs from canonical recipe')
        record['cgroup_proof'] = qualify_cgroup(sup, name, owner, image, observed, work, public_recipe, observe_cgroup)
        save(output / 'cgroup-verification.json', record['cgroup_proof'])
        def check_build():
            current = inspect_builder(sup, name, owner, image)
            require(current['Id'] == observed['Id'], 'Builder identity changed')
            cgroup.sample(current['State']['Pid'], record['cgroup_proof'], observe=observe_cgroup)
        command = build_command(stage, data, identity, name, context, output / 'image.oci.tar', layout, base)
        record['command'] = command
        sup.run(command, seconds=2700, inspect=check_build)
        check_build()
        final = inspect_builder(sup, name, owner, image)
        record['builder_state'] = {key: final['State'].get(key) for key in ('OOMKilled', 'ExitCode', 'Error')}
        record['build_success'] = True
        record['archive_bytes'] = (output / 'image.oci.tar').stat().st_size
        record['archive_sha256'] = oci.digest_file(output / 'image.oci.tar')
        # Releasing only our builder's state reclaims compiled Go caches before scan.
        record['builder_cleanup_state'] = remove_builder(sup, name, owner, image,
                                                        volume_absent_before=volume_absent_before)
        created = False
        record['builder_removed'] = True
        proof = oci.verify(output / 'image.oci.tar', record['archive_sha256'], record['archive_bytes'],
                           identity['source_commit'], stage, SENTINEL, base)
        save(output / 'oci-verification.json', proof)
        record['oci_verified'] = True
        run_scan(sup, data, work, output, owner, proof, record)
    except BaseException as exc:
        record['failure_type'] = type(exc).__name__
        # Do not include arbitrary command stderr/environment in structured metadata.
        raise
    finally:
        if created:
            try:
                record['builder_cleanup_state'] = remove_builder(sup, name, owner, image,
                                                                volume_absent_before=volume_absent_before)
                record['builder_removed'] = True
            except BaseException as exc:
                record['cleanup_failure_type'] = type(exc).__name__
        record['minimum_free_bytes'] = sup.minimum
        record['finished_at'] = now()
        save(output / 'build-receipt.json', record)


def probe_scanner(sup, name, owner, image):
    result = subprocess.run(['docker', 'inspect', name], env=sup.env, capture_output=True, timeout=30)
    if container_absent(result, name):
        return None
    require(result.returncode == 0, 'Unable to determine scanner ownership')
    items = json.loads(result.stdout)
    require(len(items) == 1, 'Ambiguous scanner container')
    item = items[0]
    require(item['Config'].get('Labels', {}).get(OWNER) == owner and item['Name'] == '/' + name and
            item['Config'].get('Image') == image and re.fullmatch(r'[0-9a-f]{64}', item.get('Id', '')),
            'Foreign scanner container')
    return item


def remove_scanner(sup, name, owner, image, record):
    # Unknown daemon/transport failures are not evidence of absence. Cleanup
    # deliberately bypasses disk admission, just like owned builder cleanup.
    item = probe_scanner(sup, name, owner, image)
    if item is not None:
        record['scanner_state'] = {key: item['State'].get(key) for key in ('OOMKilled', 'ExitCode', 'Error', 'Running')}
        subprocess.run(['docker', 'rm', '--force', item['Id']], env=sup.env, check=True, timeout=30)
    require(probe_scanner(sup, name, owner, image) is None, 'Owned scanner removal not confirmed')
    record['scanner_removed'] = True
    if item is not None:
        require(not item['State'].get('OOMKilled'), 'Scanner was OOM-killed')


def run_scan(sup, data, work, output, owner, proof, record):
    cache = work / 'scan-cache'
    cache.mkdir()
    (cache / 'tmp').mkdir()
    name = owner + '-scan'
    scan_output = work / 'scan-output'
    scan_output.mkdir()
    command = ['docker', 'run', '--name', name, '--label', OWNER + '=' + owner,
               '--user', str(os.getuid()) + ':' + str(os.getgid()), '--cpus', '2',
               '--memory', '4g', '--memory-swap', '4g', '--pids-limit', '256', '--read-only',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
               '--mount', 'type=bind,source=' + str(output / 'image.oci.tar') + ',target=/input/image.oci.tar,readonly',
               '--mount', 'type=bind,source=' + str(cache) + ',target=/cache',
               '--mount', 'type=bind,source=' + str(scan_output) + ',target=/output',
               '--env', 'TMPDIR=/cache/tmp', data['tools']['trivy']['image'],
               'image', '--input', '/input/image.oci.tar', '--cache-dir', '/cache',
               '--scanners', 'vuln', '--pkg-types', 'os,library', '--list-all-pkgs',
               '--format', 'json', '--output', '/output/scan.json', '--timeout', '15m',
               '--parallel', '2', '--ignorefile', '/dev/null', '--exit-code', '0']
    record['scan_command'] = command
    try:
        sup.run(command, seconds=1000)
    finally:
        # A killed docker client does not necessarily remove its container.
        remove_scanner(sup, name, owner, data['tools']['trivy']['image'], record)
    require(record.get('scanner_state', {}).get('ExitCode') == 0 and
            record['scanner_state'].get('Running') is False, 'Successful scanner exit was not observed')
    require(oci.digest_file(output / 'image.oci.tar') == record['archive_sha256'], 'Archive changed during scan')
    require(not (output / 'scan.json').exists(), 'Scan output already exists')
    (scan_output / 'scan.json').rename(output / 'scan.json')
    summary = scan_summary(document(output / 'scan.json', 128 * 1024**2), proof)
    save(output / 'scan-summary.json', summary)
    database = cache / 'db/metadata.json'
    save(output / 'scan-database.json', document(database))
    record['scan_sha256'] = oci.digest_file(output / 'scan.json')
    record['scan_completed'] = True
    record['scan_gate_passed'] = summary['scan_gate_passed']


def gate(directories):
    require(len(directories) == 2, 'Both stages required')
    builds = [document(p / 'build-receipt.json') for p in directories]
    require({b.get('stage') for b in builds} == {'public', 'private'}, 'Missing stage')
    for key in ('source_commit', 'run_id', 'run_attempt', 'manifest_sha256'):
        require(len({b.get(key) for b in builds}) == 1 and builds[0].get(key), 'Stage identity differs')
    for directory, build in zip(directories, builds):
        check_cgroup_receipt(build)
        require(document(directory / 'cgroup-verification.json') == build['cgroup_proof'], 'Cgroup proof differs')
        summary = document(directory / 'scan-summary.json')
        require(oci.digest_file(directory / 'scan.json') == build.get('scan_sha256'), 'Raw scan changed before gate')
        require(scan_summary(document(directory / 'scan.json', 128 * 1024**2),
                             document(directory / 'oci-verification.json')) == summary, 'Scan summary changed')
        require(build.get('build_success') is True and build.get('oci_verified') is True and
                build.get('scan_completed') is True and build.get('builder_removed') is True and
                build.get('scanner_removed') is True and
                build.get('builder_cleanup_state', {}).get('state_volume_removed') is True and
                not build.get('cleanup_failure_type'), 'Incomplete artifact production')
        require(summary.get('scan_gate_passed') is True and summary.get('blocking_findings') == 0 and
                build.get('scan_gate_passed') is True, 'Raw HIGH/CRITICAL findings require review; no automatic exclusions')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['public', 'private', 'gate'])
    parser.add_argument('--parent', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.operation == 'gate':
        require(args.parent and args.output, 'Both artifact directories required')
        gate([args.parent, args.output])
    else:
        execute(args.operation, args.parent)


if __name__ == '__main__':
    main()
