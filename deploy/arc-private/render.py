#!/usr/bin/env python3
"""Render repository-scoped ephemeral ARC pools. Rendering never deploys resources."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('reserve_pool', ROOT.parent / 'runner-reserve/render_pool.py')
RESERVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RESERVE)
PROFILES = ('ci', 'automation', 'release', 'lan', 'arm64')
PINNED = re.compile(r'^[a-z0-9./:_-]+@sha256:[0-9a-f]{64}$')
SCOPE = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
RUNNER_HOME = '/home/runner'
TOOL_CACHE = '/opt/hostedtoolcache'
PRIVATE_ADMISSION = 'private-ephemeral-workers'
HOSTNAME_LABEL = 'kubernetes.io/hostname'
SHARED_RUNTIME = 'victron-reserve'
EXCLUSIVE_RUNTIME = 'victron-private-exclusive'
SANDBOX_RUNTIMES = (SHARED_RUNTIME, EXCLUSIVE_RUNTIME)
NUC_RUNTIME = 'victron-private-nuc-kvm'
SCOPE_LABEL = 'private-runners.github.io/scope'
PROFILE_LABEL = 'private-runners.github.io/profile'
SERIAL_LABEL = 'private-runners.github.io/serial-domain'
DOCKER_DATA = '/var/lib/docker'
DOCKER_SCRATCH_SIZE = '16Gi'
# Embed the checked startup code; native workers have no ConfigMap dependency.
NATIVE_DOCKER_BOOT = (ROOT / 'native-docker.sh').read_text()
NATIVE_BOOT = '''set -euo pipefail
umask 077
test -n "${ACTIONS_RUNNER_INPUT_JITCONFIG:-}"
test ! -e /home/runner/.runner
cp -R /actions-runner/. /home/runner/
mkdir -p /home/runner/_work /opt/hostedtoolcache
cd /home/runner
if [ -n "${DOCKER_HOST:-}" ]; then
  for attempt in $(seq 1 90); do
    docker info >/dev/null 2>&1 && break
    test "$attempt" != 90 || exit 1
    sleep 1
  done
fi
# Jobs need normal checkout modes so Docker COPY works for non-root image users.
# The pod filesystem is disposable and never shared with another job.
umask 022
exec ./run.sh
'''


class NucKvmConfig(NamedTuple):
    """One explicitly selected private CI scope; placement/resources are fixed."""
    scope: str
    image: str
    node_ip: str


class NucKvmTarget(NamedTuple):
    """One explicitly selected repository/profile pair."""
    scope: str
    profile: str


class NucKvmPlan(NamedTuple):
    """Complete NUC target set sharing one reviewed image and guard address."""
    targets: tuple[NucKvmTarget, ...]
    image: str
    node_ip: str


def load_nuc_kvm_plan(path):
    """Read a strict plan, never an incremental patch or resource override."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate key in NUC KVM plan')
            result[key] = value
        return result

    data = json.loads(path.read_text(), object_pairs_hook=unique_object)
    if not isinstance(data, dict) or set(data) != {'targets', 'image', 'node_ip'}:
        raise ValueError('NUC KVM plan requires exactly targets, image and node_ip')
    if not isinstance(data['targets'], list) or not data['targets']:
        raise ValueError('NUC KVM plan requires an explicit nonempty target list')
    if any(not isinstance(target, dict) or set(target) != {'scope', 'profile'}
           for target in data['targets']):
        raise ValueError('Each NUC KVM target requires exactly scope and profile')
    return NucKvmPlan(tuple(NucKvmTarget(**target) for target in data['targets']),
                      data['image'], data['node_ip'])


def validate_nuc_kvm(rows, config, sandbox_runtime, node_ip):
    if config is None:
        return None
    legacy = isinstance(config, NucKvmConfig)
    if legacy:
        if not all(isinstance(x, str) for x in config):
            raise ValueError('NUC KVM requires a typed scope, image and node address')
        plan = NucKvmPlan((NucKvmTarget(config.scope, 'ci'),), config.image, config.node_ip)
    elif isinstance(config, NucKvmPlan):
        plan = config
    else:
        raise ValueError('NUC KVM requires a typed config or complete plan')
    if (not isinstance(plan.image, str) or not PINNED.fullmatch(plan.image)
            or not isinstance(plan.node_ip, str)):
        raise ValueError('NUC KVM requires a digest-pinned image and node address')
    if (not isinstance(plan.targets, tuple) or not plan.targets
            or any(not isinstance(target, NucKvmTarget)
                   or not isinstance(target.scope, str) or not SCOPE.fullmatch(target.scope)
                   or not isinstance(target.profile, str) or target.profile not in ('ci', 'automation', 'release')
                   for target in plan.targets)):
        raise ValueError('NUC KVM requires explicit typed CI, automation or release targets')
    if len(set(plan.targets)) != len(plan.targets):
        raise ValueError('Duplicate NUC KVM target')
    address = ipaddress.IPv4Address(plan.node_ip)
    if address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local:
        raise ValueError('NUC guard requires a unicast node address')
    if str(address) == node_ip or sandbox_runtime != SHARED_RUNTIME:
        raise ValueError('NUC must be separate from the existing shared node/runtime')
    for target in plan.targets:
        selected = [row for row in rows if row['name'] == target.scope]
        if len(selected) != 1 or selected[0].get('visibility') != 'private':
            raise ValueError('NUC KVM scope must exist exactly once as private inventory')
        row = selected[0]
        if target.profile not in row['profiles']:
            raise ValueError('NUC KVM target profile must be declared in its inventory row')
        if target.profile == 'ci':
            mode = (row.get('ci_resource_class', 'standard'), validate_ci_storage(row))
            if legacy and mode != ('standard', 'tmpfs-2gi'):
                raise ValueError('Legacy NUC KVM supports only explicit standard CI tmpfs-2gi')
            if mode not in (('standard', 'tmpfs-2gi'), ('heavy', 'tmpfs-6gi-heavy')):
                raise ValueError('NUC KVM CI requires standard tmpfs-2gi or heavy tmpfs-6gi-heavy')
        elif target.profile == 'automation' and validate_automation_storage(row) != 'tmpfs-3gi':
            raise ValueError('NUC KVM automation requires explicit tmpfs-3gi')
    return NucKvmPlan(tuple(sorted(plan.targets)), plan.image, plan.node_ip)


def name(scope, profile):
    return 'private-' + hashlib.sha256(scope.encode()).hexdigest()[:12] + '-' + profile


def validate_listener_node(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9](?:[-a-z0-9.]{0,61}[a-z0-9])?', value):
        raise ValueError('Listener node must be a valid hostname label')
    return value


def validate_profiles(row):
    if row.get('ci_resource_class', 'standard') not in ('standard', 'heavy'):
        raise ValueError('Unknown CI resource class')
    profiles = row['profiles']
    if not isinstance(profiles, list) or not profiles or any(p not in PROFILES for p in profiles):
        raise ValueError('Unsupported or empty worker profile')
    if len(set(profiles)) != len(profiles):
        raise ValueError('Duplicate worker profile')
    if {'lan', 'release'} <= set(profiles):
        raise ValueError('Choose one release route: sandbox release or LAN')
    validate_ci_storage(row)
    validate_automation_storage(row)


def validate_ci_storage(row):
    storage = row.get('ci_storage', 'disk')
    if storage not in ('disk', 'tmpfs-2gi', 'tmpfs-6gi-heavy'):
        raise ValueError('Unknown private CI storage mode')
    if storage == 'tmpfs-2gi' and (
            'ci' not in row['profiles'] or row.get('ci_resource_class', 'standard') != 'standard'):
        raise ValueError('The 2Gi tmpfs candidate supports only standard CI')
    if storage == 'tmpfs-6gi-heavy' and (
            row.get('visibility') != 'private' or 'ci' not in row['profiles']
            or row.get('ci_resource_class') != 'heavy'):
        raise ValueError('The 6Gi tmpfs candidate requires explicitly private heavy CI')
    return storage


def validate_automation_storage(row):
    storage = row.get('automation_storage', 'disk')
    if storage not in ('disk', 'tmpfs-3gi'):
        raise ValueError('Unknown private automation storage mode')
    if storage == 'tmpfs-3gi' and (
            row.get('visibility') != 'private' or 'automation' not in row['profiles']):
        raise ValueError('The 3Gi tmpfs candidate requires explicitly private automation')
    return storage


def validate_lan(row):
    for destination in row.get('lan_egress', []):
        network = ipaddress.ip_network(destination['cidr'])
        if network.version != 4 or network.prefixlen != 32:
            raise ValueError('LAN destinations must be exact IPv4 /32 hosts')
        if type(destination['port']) is not int or not 1 <= destination['port'] <= 65535:
            raise ValueError('Invalid LAN TCP port')
    if 'lan' in row['profiles'] and not row.get('lan_egress'):
        raise ValueError('LAN profile requires explicit destinations')


def validate_inventory(inventory):
    rows = inventory['repositories']
    seen = set()
    for row in rows:
        scope = row['name']
        if not SCOPE.fullmatch(scope) or scope in seen or row.get('visibility') != 'private':
            raise ValueError('Inventory requires unique explicitly private owner/repository scopes')
        seen.add(scope)
        validate_profiles(row)
        validate_lan(row)
        if 'listener_node' in row:
            validate_listener_node(row['listener_node'])
    return rows


def values(row, profile, sandbox_image, native_image, node_ip, arm_node='h7', listener_node='mp',
           sandbox_runtime=SHARED_RUNTIME):
    storage = validate_ci_storage(row)
    automation_storage = validate_automation_storage(row)
    if (storage != 'disk' or automation_storage != 'disk') and sandbox_runtime != SHARED_RUNTIME:
        raise ValueError('The tmpfs candidate requires the unchanged shared runtime')
    if sandbox_runtime not in SANDBOX_RUNTIMES:
        raise ValueError('Unknown private sandbox runtime')
    scope = row['name']
    if profile not in row['profiles'] or not all(PINNED.fullmatch(i) for i in (sandbox_image, native_image)):
        raise ValueError('Declared profile and digest-pinned images required')
    # Reuse the reviewed reserve bootstrap, runtime and worker isolation contract.
    fake_inventory = {'organizations': [], 'repositories': [{'name': scope}],
                      'profiles': {profile: name(scope, profile)}}
    result = RESERVE.pool_values(scope, profile, sandbox_image, 'mp', node_ip, fake_inventory)
    result.update({'runnerScaleSetName': name(scope, profile),
                   'scaleSetLabels': [name(scope, profile)],
                   'githubConfigSecret': 'private-runner-app-' + scope.split('/')[0].lower()})
    listener = result['listenerTemplate']['spec']['containers'][0]
    result['listenerTemplate']['spec']['securityContext'] = {
        'seccompProfile': {'type': 'RuntimeDefault'}}
    result['listenerTemplate']['spec']['nodeSelector'] = {
        HOSTNAME_LABEL: validate_listener_node(row.get('listener_node', listener_node))}
    listener['resources'] = {'requests': {'cpu': '5m', 'memory': '32Mi'},
                             'limits': {'cpu': '100m', 'memory': '192Mi'}}
    spec = result['template']['spec']
    result['template']['metadata']['labels']['private-runners.github.io/scope'] = name(scope, 'scope')
    result['template']['metadata']['labels']['private-runners.github.io/profile'] = profile
    spec['restartPolicy'] = 'Never'
    spec['serviceAccountName'] = 'reserve-worker' if profile in ('ci', 'automation', 'release') else 'private-worker'
    spec['containers'][0]['resources'] = {
        'requests': {'cpu': '1900m' if profile == 'ci' else '50m',
                     'memory': '1Gi' if profile in ('ci', 'automation') else '512Mi', 'ephemeral-storage': '1Gi'},
        'limits': {'cpu': '4' if profile == 'ci' else '1',
                   'memory': {'ci': '6Gi', 'automation': '4Gi'}.get(profile, '2Gi'),
                   'ephemeral-storage': '20Gi'}}
    if profile == 'ci' and row.get('ci_resource_class') == 'heavy':
        # Covers native build/test suites formerly split between a large runner
        # and Docker sidecar. Namespace limits admit only one heavy worker and
        # reject a concurrent standard worker while it is present.
        spec['containers'][0]['resources'] = {
            'requests': {'cpu': '1500m', 'memory': '13Gi', 'ephemeral-storage': '2Gi'},
            'limits': {'cpu': '3', 'memory': '16Gi', 'ephemeral-storage': '30Gi'}}
    if profile in ('ci', 'automation', 'release'):
        spec['runtimeClassName'] = sandbox_runtime
        spec['volumes'] = [{'name': 'docker-data', 'emptyDir': {'sizeLimit': DOCKER_SCRATCH_SIZE}}]
        spec['containers'][0]['volumeMounts'] = [{'name': 'docker-data', 'mountPath': DOCKER_DATA}]
        mode = None
        runner = spec['containers'][0]
        if profile == 'ci' and storage == 'tmpfs-2gi':
            mode = 'ci-tmpfs-2gi'
            runner['resources']['requests']['memory'] = '3Gi'
        elif profile == 'ci' and storage == 'tmpfs-6gi-heavy':
            mode = 'ci-heavy-tmpfs-6gi'
        elif profile == 'automation' and automation_storage == 'tmpfs-3gi':
            mode = 'automation-tmpfs-3gi'
            runner['resources']['requests']['memory'] = '5Gi'
            runner['resources']['limits']['memory'] = '6Gi'
        runner['env'].append({'name': 'RUNNER_DOCKER_STORAGE_DRIVER',
                              'value': 'overlayfs' if mode else 'overlay2'})
        if mode:
            runner['env'] += [
                {'name': 'RUNNER_PRIVATE_DOCKER_STORAGE', 'value': mode},
                {'name': 'RUNNER_PRIVATE_NAMESPACE', 'valueFrom': {'fieldRef': {'fieldPath': 'metadata.namespace'}}},
                {'name': 'RUNNER_PRIVATE_PROFILE', 'valueFrom': {'fieldRef': {
                    'fieldPath': "metadata.labels['private-runners.github.io/profile']"}}},
                *[{'name': 'RUNNER_PRIVATE_MEMORY_' + suffix, 'valueFrom': {'resourceFieldRef': {
                    'containerName': 'runner', 'resource': resource + '.memory', 'divisor': '1'}}}
                  for suffix, resource in [('LIMIT', 'limits'), ('REQUEST', 'requests')]],
            ]
        return result
    spec.pop('runtimeClassName')
    result['template']['metadata']['labels'].pop(RESERVE.LABEL)
    spec['nodeSelector'] = {HOSTNAME_LABEL: arm_node if profile == 'arm64' else 'mp',
                            'kubernetes.io/arch': 'arm64' if profile == 'arm64' else 'amd64'}
    spec['securityContext'] = {'runAsUser': 1001, 'runAsGroup': 121, 'fsGroup': 121,
                               'seccompProfile': {'type': 'RuntimeDefault'}}
    security = {'runAsNonRoot': True, 'allowPrivilegeEscalation': False,
                'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}
    # A fresh per-pod emptyDir mount, not a host temporary file.
    mounts = {'home': RUNNER_HOME, 'tools': TOOL_CACHE, 'tmp': '/tmp'}  # nosec B108
    runner = {'name': 'runner', 'image': native_image, 'imagePullPolicy': 'IfNotPresent',
              'command': ['/bin/bash', '-c'], 'args': [NATIVE_BOOT], 'securityContext': security,
              'env': [{'name': 'HOME', 'value': RUNNER_HOME},
                      {'name': 'RUNNER_TOOL_CACHE', 'value': TOOL_CACHE},
                      {'name': 'AGENT_TOOLSDIRECTORY', 'value': TOOL_CACHE},
                      {'name': 'ImageOS', 'value': 'ubuntu24'}],
              'resources': {'requests': {'cpu': '50m', 'memory': '256Mi', 'ephemeral-storage': '512Mi'},
                            'limits': {'cpu': '2', 'memory': '3Gi', 'ephemeral-storage': '12Gi'}},
              'volumeMounts': [{'name': k, 'mountPath': v} for k, v in mounts.items()]}
    spec['containers'] = [runner]
    spec['volumes'] = [{'name': k, 'emptyDir': {'sizeLimit': '12Gi' if k == 'home' else '2Gi'}} for k in mounts]
    if profile == 'arm64':
        # This trusted private architecture profile retains native Docker support;
        # no host socket, host path, persistent disk or Kubernetes identity is used.
        runner['env'].append({'name': 'DOCKER_HOST', 'value': 'unix:///docker-socket/docker.sock'})
        runner['volumeMounts'].append({'name': 'docker-socket', 'mountPath': '/docker-socket'})
        spec['initContainers'] = [{
            'name': 'docker', 'restartPolicy': 'Always', 'image': native_image,
            'command': ['/bin/bash', '-c'],
            'args': [NATIVE_DOCKER_BOOT],
            'securityContext': {'privileged': True, 'runAsUser': 0, 'runAsGroup': 0},
            'resources': {'requests': {'cpu': '50m', 'memory': '512Mi', 'ephemeral-storage': '512Mi'},
                          'limits': {'cpu': '1', 'memory': '2Gi', 'ephemeral-storage': '16Gi'}},
            'volumeMounts': [{'name': 'docker-socket', 'mountPath': '/docker-socket'},
                             {'name': 'docker-data', 'mountPath': '/docker-data'},
                             {'name': 'home', 'mountPath': RUNNER_HOME}]}]
        spec['volumes'] += [{'name': 'docker-socket', 'emptyDir': {'sizeLimit': '64Mi'}},
                            {'name': 'docker-data', 'emptyDir': {'sizeLimit': '16Gi'}}]
    return result


def sandbox_quota(profile, automation_tmpfs=False):
    # ARC keeps listeners in its controller namespace. This bounds aggregate
    # worker demand, including RuntimeClass overhead, across all repositories.
    budget = {'ci': ('8200m', '17Gi'), 'automation': ('2200m', '9Gi'), 'release': ('2200m', '5Gi')}[profile]
    return {'pods': '2', 'requests.cpu': '2000m' if profile == 'ci' else '1000m',
            'requests.memory': ('14Gi' if profile == 'ci' else
                                '6Gi' if profile == 'automation' and automation_tmpfs else '3Gi'),
            'limits.cpu': budget[0], 'limits.memory': budget[1],
            'requests.ephemeral-storage': '3Gi', 'limits.ephemeral-storage': '40Gi'}


def private_sandbox_foundation(source, sandbox_runtime=SHARED_RUNTIME, automation_tmpfs=False):
    item = copy.deepcopy(source)
    meta = item['metadata']
    if item['kind'] == 'ValidatingAdmissionPolicy':
        meta['name'] = PRIVATE_ADMISSION
        item['spec']['matchConstraints']['namespaceSelector']['matchLabels'] = {
            'runner-reserve.github.io/admission': 'private-worker'}
        item['spec']['validations'] = private_scratch_validations(item['spec']['validations'])
        if sandbox_runtime == EXCLUSIVE_RUNTIME:
            for rule in item['spec']['validations']:
                if rule['message'] == 'gVisor is mandatory':
                    rule['expression'] = (
                        "has(object.spec.runtimeClassName) && object.spec.runtimeClassName == "
                        + json.dumps(EXCLUSIVE_RUNTIME))
            item['spec']['validations'].extend([
                {'expression': "has(object.spec.restartPolicy) && object.spec.restartPolicy == 'Never'",
                 'message': 'Exclusive bind mounts require one container lifetime'},
                {'expression': "object.spec.containers.all(c, (!has(c.terminationMessagePath) || c.terminationMessagePath == '/dev/termination-log') && (!has(c.terminationMessagePolicy) || c.terminationMessagePolicy == 'File'))",
                 'message': 'Exclusive workers use the verified termination log path'},
            ])
    elif item['kind'] == 'ValidatingAdmissionPolicyBinding':
        meta['name'] = PRIVATE_ADMISSION
        item['spec']['policyName'] = PRIVATE_ADMISSION
    elif item['kind'] == 'Namespace':
        meta['name'] = meta['name'].replace('runner-reserve-', 'arc-private-')
        meta['labels']['app.kubernetes.io/part-of'] = 'private-ephemeral-runners'
        meta['labels']['runner-reserve.github.io/admission'] = 'private-worker'
    else:
        meta['namespace'] = meta['namespace'].replace('runner-reserve-', 'arc-private-')
        if item['kind'] == 'ResourceQuota':
            item['spec']['hard'] = sandbox_quota(meta['namespace'].removeprefix('arc-private-'), automation_tmpfs)
    return item


def exclusive_runtime_resources(private_policy):
    """Keep this class private even if a Pod bypasses the namespace label selector."""
    guard = copy.deepcopy(private_policy)
    guard['metadata']['name'] = 'private-exclusive-runtime-use'
    guard['spec']['matchConstraints'].pop('namespaceSelector')
    guard['spec']['matchConditions'] = [{
        'name': 'exclusive-runtime',
        'expression': "has(object.spec.runtimeClassName) && object.spec.runtimeClassName == "
                      + json.dumps(EXCLUSIVE_RUNTIME)}]
    guard['spec']['validations'].insert(0, {
        'expression': "request.namespace in ['arc-private-ci', 'arc-private-automation', 'arc-private-release']",
        'message': 'The exclusive runtime is restricted to private sandbox namespaces'})
    runtime = {
        'apiVersion': 'node.k8s.io/v1', 'kind': 'RuntimeClass',
        'metadata': {'name': EXCLUSIVE_RUNTIME}, 'handler': EXCLUSIVE_RUNTIME,
        'overhead': {'podFixed': {'cpu': '100m', 'memory': '128Mi'}},
        'scheduling': {'nodeSelector': {HOSTNAME_LABEL: 'mp',
                                        'kubernetes.io/arch': 'amd64'}}}
    binding = {
        'apiVersion': 'admissionregistration.k8s.io/v1',
        'kind': 'ValidatingAdmissionPolicyBinding',
        'metadata': {'name': 'private-exclusive-runtime-use'},
        'spec': {'policyName': 'private-exclusive-runtime-use', 'validationActions': ['Deny']}}
    return [runtime, guard, binding]


def private_tmpfs_validation():
    """Exact opt-in tuples; dynamic Quantity access also supports Kubernetes 1.31."""
    def memory(request, limit):
        return (f"dyn(c.resources).requests['memory'] == '{request}' && "
                f"dyn(c.resources).limits['memory'] == '{limit}'")

    def cpu(request, limit):
        return ("'cpu' in dyn(c.resources).requests && 'cpu' in dyn(c.resources).limits && "
                "type(dyn(c.resources).requests['cpu']) == string && "
                "type(dyn(c.resources).limits['cpu']) == string && "
                f"dyn(c.resources).requests['cpu'] == '{request}' && "
                f"dyn(c.resources).limits['cpu'] == '{limit}'")

    def context(mode, profile):
        return (f"e.value == '{mode}' && object.metadata.namespace == 'arc-private-{profile}' && "
                f"object.metadata.labels['private-runners.github.io/profile'] == '{profile}' && "
                "c.env.exists(b, b.name == 'RUNNER_DOCKER_STORAGE_DRIVER' && has(b.value) && "
                "!has(b.valueFrom) && b.value == 'overlayfs')")

    cases = [
        context('ci-tmpfs-2gi', 'ci') + ' && ' + memory('3Gi', '6Gi') + ' && ' + cpu('1900m', '4'),
        context('ci-heavy-tmpfs-6gi', 'ci') + ' && ' + memory('13Gi', '16Gi')
        + ' && ' + cpu('1500m', '3'),
        context('automation-tmpfs-3gi', 'automation') + ' && ' + memory('5Gi', '6Gi')
        + ' && ' + cpu('50m', '1'),
    ]
    return (
        "object.spec.containers.all(c, !has(c.env) || ("
        "size(c.env.filter(e, e.name == 'RUNNER_PRIVATE_DOCKER_STORAGE')) <= 1 && "
        "c.env.all(e, e.name != 'RUNNER_PRIVATE_DOCKER_STORAGE' || ("
        "has(e.value) && !has(e.valueFrom) && object.spec.runtimeClassName == 'victron-reserve' && "
        "has(object.metadata.labels) && 'private-runners.github.io/profile' in object.metadata.labels && "
        "has(c.resources) && has(dyn(c.resources).requests) && has(dyn(c.resources).limits) && "
        "type(dyn(c.resources).requests) == map && type(dyn(c.resources).limits) == map && "
        "'memory' in dyn(c.resources).requests && 'memory' in dyn(c.resources).limits && "
        "type(dyn(c.resources).requests['memory']) == string && "
        "type(dyn(c.resources).limits['memory']) == string && ("
        + ' || '.join('(' + case + ')' for case in cases) + ')))))')


def private_scratch_validations(validations):
    """Permit only one disposable disk mount, preserving the public no-volume rule."""
    for rule in validations:
        if rule['message'] == 'Host, persistent and credential volumes are forbidden':
            rule['expression'] = (
                "has(object.spec.volumes) && size(object.spec.volumes) == 1 && "
                "object.spec.volumes.all(v, v.name == 'docker-data' && has(v.emptyDir) && "
                "(!has(v.emptyDir.medium) || v.emptyDir.medium == '') && "
                "has(dyn(v.emptyDir).sizeLimit) && type(dyn(v.emptyDir).sizeLimit) == string && "
                "dyn(v.emptyDir).sizeLimit == '16Gi')")
            rule['message'] = 'Only the bounded private Docker emptyDir is allowed'
    # Kubernetes 1.31 omits Quantity maps from its advisory CEL schema.
    # Dynamic access retains explicit presence, map, string and exact-value guards.
    validations.extend([
        {'expression': private_tmpfs_validation(),
         'message': 'The tmpfs candidate requires its exact private profile and resource tuple'},
        {'expression': "object.spec.containers.all(c, has(c.volumeMounts) && size(c.volumeMounts) == 1 && c.volumeMounts.all(m, m.name == 'docker-data' && m.mountPath == '/var/lib/docker' && !has(m.subPath) && !has(m.subPathExpr) && !has(m.mountPropagation) && (!has(m.readOnly) || !m.readOnly)))",
         'message': 'Docker scratch cannot be redirected, shared or mounted elsewhere'},
        {'expression': "object.spec.containers.all(c, has(c.env) && size(c.env.filter(e, e.name == 'RUNNER_DOCKER_STORAGE_DRIVER')) == 1 && c.env.exists(e, e.name == 'RUNNER_DOCKER_STORAGE_DRIVER' && has(e.value) && e.value == (c.env.exists(m, m.name == 'RUNNER_PRIVATE_DOCKER_STORAGE') ? 'overlayfs' : 'overlay2') && !has(e.valueFrom)))",
         'message': 'Private storage modes require their exact Docker backend'},
        {'expression': "!has(object.metadata.annotations) || object.metadata.annotations.all(k, !k.startsWith('dev.gvisor.'))",
         'message': 'Worker annotations cannot override sandbox filesystem policy'},
    ])
    return validations


def native_foundations(profile, network_policy):
    ns = 'arc-private-' + profile
    resources = [
        {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': ns, 'labels': {
            'app.kubernetes.io/part-of': 'private-ephemeral-runners',
            'pod-security.kubernetes.io/enforce': 'privileged' if profile == 'arm64' else 'baseline'}}},
        {'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': {'name': 'private-worker', 'namespace': ns},
         'automountServiceAccountToken': False},
        {'apiVersion': 'v1', 'kind': 'ResourceQuota', 'metadata': {'name': 'private-budget', 'namespace': ns},
         'spec': {'hard': {'pods': '8', 'requests.cpu': '500m', 'requests.memory': '4Gi',
                           'limits.cpu': '12', 'limits.memory': '20Gi',
                           'requests.ephemeral-storage': '4Gi', 'limits.ephemeral-storage': '80Gi'}}}]
    base = copy.deepcopy(network_policy)
    base['metadata'] = {'name': 'private-worker-isolation', 'namespace': ns}
    base['spec']['egress'] = [r for r in base['spec']['egress'] if r['ports'][0]['port'] != 19999]
    resources.append(base)
    return resources


def lan_policy(row):
    return {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
            'metadata': {'name': name(row['name'], 'lan'), 'namespace': 'arc-private-lan'},
            'spec': {'podSelector': {'matchLabels': {'private-runners.github.io/scope': name(row['name'], 'scope')}},
                     'policyTypes': ['Egress'], 'egress': [
                         {'to': [{'ipBlock': {'cidr': d['cidr']}}],
                          'ports': [{'port': d['port'], 'protocol': 'TCP'}]} for d in row['lan_egress']]}}


def foundations(rows, sandbox_image, node_ip, sandbox_runtime=SHARED_RUNTIME):
    if sandbox_runtime not in SANDBOX_RUNTIMES:
        raise ValueError('Unknown private sandbox runtime')
    # Separate policy/image identity preserves the public reserve boundary.
    rendered = RESERVE.foundations(['ci', 'automation', 'release'], sandbox_image, node_ip)
    automation_tmpfs = any(validate_automation_storage(row) == 'tmpfs-3gi' for row in rows)
    resources = [private_sandbox_foundation(item, sandbox_runtime, automation_tmpfs)
                 for item in rendered['items']]
    if sandbox_runtime == EXCLUSIVE_RUNTIME:
        private_policy = next(item for item in resources if item['kind'] == 'ValidatingAdmissionPolicy')
        resources.extend(exclusive_runtime_resources(private_policy))
    network = next(r for r in resources if r['kind'] == 'NetworkPolicy')
    for profile in ('lan', 'arm64'):
        if any(profile in r['profiles'] for r in rows):
            resources.extend(native_foundations(profile, network))
    resources.extend(lan_policy(row) for row in rows if 'lan' in row['profiles'])
    return {'apiVersion': 'v1', 'kind': 'List', 'items': resources}


def nuc_serial_affinity():
    """All NUC private workers share one scheduler-enforced slot."""
    return {'podAntiAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': [{
        'labelSelector': {'matchLabels': {SERIAL_LABEL: 'nuc-kvm'}},
        'namespaceSelector': {}, 'topologyKey': HOSTNAME_LABEL}]}}


def nuc_serial_validation():
    """Require the complete affinity shape, including the all-namespace selector."""
    return (
        'has(object.metadata.labels) && '
        + json.dumps(SERIAL_LABEL) + ' in object.metadata.labels && '
        'object.metadata.labels[' + json.dumps(SERIAL_LABEL) + "] == 'nuc-kvm' && "
        'has(object.spec.affinity) && !has(object.spec.affinity.nodeAffinity) && '
        '!has(object.spec.affinity.podAffinity) && has(object.spec.affinity.podAntiAffinity) && '
        '(!has(object.spec.affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution) || '
        'size(object.spec.affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution) == 0) && '
        'has(object.spec.affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution) && '
        'size(object.spec.affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution) == 1 && '
        'object.spec.affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution.all(t, '
        't.topologyKey == ' + json.dumps(HOSTNAME_LABEL) + ' && '
        'has(t.labelSelector) && has(t.labelSelector.matchLabels) && '
        't.labelSelector.matchLabels == ' + json.dumps({SERIAL_LABEL: 'nuc-kvm'}) + ' && '
        '(!has(t.labelSelector.matchExpressions) || size(t.labelSelector.matchExpressions) == 0) && '
        'has(t.namespaceSelector) && '
        '(!has(t.namespaceSelector.matchLabels) || size(t.namespaceSelector.matchLabels) == 0) && '
        '(!has(t.namespaceSelector.matchExpressions) || size(t.namespaceSelector.matchExpressions) == 0) && '
        '(!has(t.namespaces) || size(t.namespaces) == 0) && '
        '(!has(t.matchLabelKeys) || size(t.matchLabelKeys) == 0) && '
        '(!has(t.mismatchLabelKeys) || size(t.mismatchLabelKeys) == 0))')


def nuc_kvm_value(value, config):
    """Change only a selected worker; keep route, listener and bootstrap."""
    spec = value['template']['spec']
    spec['runtimeClassName'] = NUC_RUNTIME
    spec['nodeSelector'] = {HOSTNAME_LABEL: 'nuc', 'kubernetes.io/arch': 'amd64'}
    spec['schedulerName'] = 'default-scheduler'
    spec['affinity'] = nuc_serial_affinity()
    labels = value['template']['metadata']['labels']
    labels[SERIAL_LABEL] = 'nuc-kvm'
    runner = spec['containers'][0]
    runner['image'] = config.image
    if labels[PROFILE_LABEL] == 'ci':
        runner['resources']['requests']['cpu'] = '600m'
        runner['resources']['limits']['cpu'] = '2'
    for env in runner['env']:
        if env['name'] == 'RUNNER_RESERVE_NODE_IP':
            env['value'] = config.node_ip


def nuc_environment_validation(expected):
    """Bind bootstrap inputs, ARC 0.15 metadata and the separately checked JIT ref."""
    clauses = []
    for env in expected:
        key = json.dumps(env['name'])
        if 'value' in env:
            check = 'has(e.value) && !has(e.valueFrom) && e.value == ' + json.dumps(env['value'])
        elif 'fieldRef' in env['valueFrom']:
            field = json.dumps(env['valueFrom']['fieldRef']['fieldPath'])
            check = ("!has(e.value) && has(e.valueFrom) && has(e.valueFrom.fieldRef) && "
                     f"e.valueFrom.fieldRef.fieldPath == {field} && "
                     "(!has(e.valueFrom.fieldRef.apiVersion) || e.valueFrom.fieldRef.apiVersion == 'v1')")
        else:
            resource = json.dumps(env['valueFrom']['resourceFieldRef']['resource'])
            check = ("!has(e.value) && has(e.valueFrom) && has(e.valueFrom.resourceFieldRef) && "
                     "e.valueFrom.resourceFieldRef.containerName == 'runner' && "
                     f"e.valueFrom.resourceFieldRef.resource == {resource} && "
                     "has(dyn(e.valueFrom.resourceFieldRef).divisor) && "
                     "dyn(e.valueFrom.resourceFieldRef).divisor == '1'")
        clauses.append(f"size(c.env.filter(e, e.name == {key})) == 1 && "
                       f"c.env.all(e, e.name != {key} || ({check}))")
    # ARC 0.15.0 resourcebuilder.go appends these literal metadata fields with
    # the JIT reference. They are optional for authored Pod dry-runs, but cannot
    # be duplicated, redirected through valueFrom or repurposed by a caller.
    metadata = {
        'GITHUB_ACTIONS_RUNNER_EXTRA_USER_AGENT': 'actions-runner-controller/0.15.0',
        'ACTIONS_RUNNER_RETURN_VERSION_DEPRECATED_EXIT_CODE': '1',
    }
    for name, value in metadata.items():
        key = json.dumps(name)
        clauses.append(f"size(c.env.filter(e, e.name == {key})) <= 1 && "
                       f"c.env.all(e, e.name != {key} || "
                       f"(has(e.value) && !has(e.valueFrom) && e.value == {json.dumps(value)}))")
    names = [e['name'] for e in expected] + ['ACTIONS_RUNNER_INPUT_JITCONFIG'] + list(metadata)
    return ('object.spec.containers.all(c, has(c.env) && c.env.all(e, e.name in '
            + json.dumps(names) + ') && ' + ' && '.join('(' + x + ')' for x in clauses) + ')')


def nuc_target_validation(target, value, scopes=None):
    """Keep identity, resources and bootstrap environment in one allowed branch."""
    runner = value['template']['spec']['containers'][0]
    namespace = json.dumps('arc-private-' + target.profile)
    scope_check = (' == ' + json.dumps(name(target.scope, 'scope')) if scopes is None
                   else ' in ' + json.dumps([name(scope, 'scope') for scope in scopes]))
    return (
        'request.namespace == ' + namespace + ' && object.metadata.namespace == ' + namespace + ' && '
        'has(object.metadata.labels) && '
        + json.dumps(SCOPE_LABEL) + ' in object.metadata.labels && '
        + json.dumps(PROFILE_LABEL) + ' in object.metadata.labels && '
        'object.metadata.labels[' + json.dumps(SCOPE_LABEL) + ']' + scope_check + ' && '
        'object.metadata.labels[' + json.dumps(PROFILE_LABEL) + '] == '
        + json.dumps(target.profile) + ' && '
        'object.spec.containers.all(c, has(c.resources) && '
        'has(dyn(c.resources).requests) && has(dyn(c.resources).limits) && '
        'dyn(c.resources).requests == ' + json.dumps(runner['resources']['requests']) + ' && '
        'dyn(c.resources).limits == ' + json.dumps(runner['resources']['limits']) + ') && '
        + nuc_environment_validation(runner['env']))


def nuc_targets_validation(values_by_target):
    """Share a branch only when every non-scope contract field is identical."""
    groups = {}
    for target in sorted(values_by_target):
        value = values_by_target[target]
        runner = value['template']['spec']['containers'][0]
        key = json.dumps([target.profile, runner['resources'], runner['env']], sort_keys=True)
        groups.setdefault(key, []).append(target)
    branches = []
    for targets in groups.values():
        first = targets[0]
        scopes = [target.scope for target in targets] if len(targets) > 1 else None
        branches.append('(' + nuc_target_validation(first, values_by_target[first], scopes) + ')')
    return ' || '.join(branches)


def nuc_kvm_foundations(document, config, values_by_target):
    """Delegate only this runtime to a global exact contract, never a name exception."""
    items = document['items']
    private = next(x for x in items if x['kind'] == 'ValidatingAdmissionPolicy'
                   and x['metadata']['name'] == PRIVATE_ADMISSION)
    guard = copy.deepcopy(private)
    guard['metadata']['name'] = NUC_RUNTIME + '-workers'
    guard['spec']['matchConstraints'].pop('namespaceSelector')
    runtime_match = "has(object.spec.runtimeClassName) && object.spec.runtimeClassName == " + json.dumps(NUC_RUNTIME)
    old_runtime_match = ("oldObject != null && has(oldObject.spec.runtimeClassName) && "
                         "oldObject.spec.runtimeClassName == " + json.dumps(NUC_RUNTIME))
    runtime_in_request = '(' + runtime_match + ') || (' + old_runtime_match + ')'
    scope_matches = []
    scopes_by_namespace = {}
    for target in config.targets:
        namespace = 'arc-private-' + target.profile
        selected_scope = name(target.scope, 'scope')
        scopes_by_namespace.setdefault(namespace, []).append(selected_scope)
        for obj in ('object', 'oldObject'):
            scope_matches.append(
                f'(request.namespace == {json.dumps(namespace)} && {obj} != null && '
                f'has({obj}.metadata.labels) && {json.dumps(SCOPE_LABEL)} in {obj}.metadata.labels && '
                f'{obj}.metadata.labels[{json.dumps(SCOPE_LABEL)}] == {json.dumps(selected_scope)})')
    selected_in_request = ' || '.join(scope_matches)
    guard['spec']['matchConditions'] = [{
        'name': 'nuc-runtime-or-selected-scope',
        'expression': '(' + runtime_in_request + ') || (' + selected_in_request + ')'}]
    # PodStatusStrategy resets spec/ownerReferences, but leaves labels mutable.
    # Run the complete contract on status writes, not only ordinary Pod updates.
    guard['spec']['matchConstraints']['resourceRules'].append({
        'apiGroups': [''], 'apiVersions': ['v1'], 'operations': ['UPDATE'], 'resources': ['pods/status']})
    # Existing mp workers retain every original rule. The new global guard also
    # protects namespaces that do not carry the private admission label.
    private['spec']['matchConditions'] = [{'name': 'not-nuc-runtime',
                                          'expression': '!(' + runtime_in_request + ')'}]
    for target in config.targets:
        # Do not condition this denial on the caller-controlled profile label.
        private['spec']['validations'].append({
            'expression': "!has(object.metadata.labels) || !('" + SCOPE_LABEL
                          + "' in object.metadata.labels) || object.metadata.labels['" + SCOPE_LABEL
                          + "'] != " + json.dumps(name(target.scope, 'scope'))
                          + ' || request.namespace != ' + json.dumps('arc-private-' + target.profile),
            'message': 'The selected NUC scope cannot fall back to an mp worker in this namespace'})
    for rule in guard['spec']['validations']:
        if rule['message'] == 'gVisor is mandatory':
            rule['expression'] = runtime_match
        elif rule['message'] == 'Only the reviewed pinned worker image is allowed':
            rule['expression'] = ("size(object.spec.containers) == 1 && object.spec.containers[0].name == 'runner' && "
                                  "object.spec.containers[0].image == " + json.dumps(config.image))
        elif rule['message'] == 'The tmpfs candidate requires its exact private profile and resource tuple':
            # Dynamic Quantity maps retain the Kubernetes 1.31 advisory workaround.
            rule['expression'] = nuc_targets_validation(values_by_target)
            rule['message'] = 'NUC workers require one exact scope, namespace, profile, resource and environment tuple'
    guard['spec']['validations'].extend([
        {'expression': "request.operation != 'UPDATE' || (oldObject != null && "
                       "has(oldObject.metadata.labels) && has(object.metadata.labels) && "
                       + ' && '.join(
                           f'{json.dumps(key)} in oldObject.metadata.labels && '
                           f'{json.dumps(key)} in object.metadata.labels && '
                           f'object.metadata.labels[{json.dumps(key)}] == oldObject.metadata.labels[{json.dumps(key)}]'
                           for key in (SCOPE_LABEL, PROFILE_LABEL, 'runner-reserve.github.io/role', SERIAL_LABEL))
                       + " && (!has(oldObject.spec.nodeName) || oldObject.spec.nodeName == '' || "
                       "(has(object.spec.nodeName) && object.spec.nodeName == oldObject.spec.nodeName)))",
         'message': 'NUC updates preserve scope, profile, worker identity and the bound node'},
        {'expression': "has(object.spec.nodeSelector) && object.spec.nodeSelector == "
                       + json.dumps({HOSTNAME_LABEL: 'nuc', 'kubernetes.io/arch': 'amd64'})
                       + " && has(object.spec.schedulerName) && object.spec.schedulerName == 'default-scheduler' && ("
                       "(request.operation == 'CREATE' && (!has(object.spec.nodeName) || object.spec.nodeName == '')) || "
                       "(request.operation == 'UPDATE' && oldObject != null && ("
                       "((!has(oldObject.spec.nodeName) || oldObject.spec.nodeName == '') && "
                       "(!has(object.spec.nodeName) || object.spec.nodeName == '')) || "
                       "(has(oldObject.spec.nodeName) && oldObject.spec.nodeName == 'nuc' && "
                       "has(object.spec.nodeName) && object.spec.nodeName == oldObject.spec.nodeName))))",
         'message': 'NUC KVM cannot select another node or scheduler'},
        {'expression': nuc_serial_validation(),
         'message': 'NUC workers require cross-namespace serialization'},
        {'expression': "has(object.spec.restartPolicy) && object.spec.restartPolicy == 'Never' && "
                       "has(object.spec.activeDeadlineSeconds) && object.spec.activeDeadlineSeconds == 14400 && "
                       "has(object.spec.terminationGracePeriodSeconds) && object.spec.terminationGracePeriodSeconds == 30 && "
                       "has(object.spec.enableServiceLinks) && !object.spec.enableServiceLinks",
         'message': 'NUC workers keep the bounded single-job lifecycle'},
        {'expression': "has(dyn(object.spec).overhead) && dyn(object.spec).overhead == {'cpu': '100m', 'memory': '128Mi'}",
         'message': 'NUC worker overhead must match its RuntimeClass'},
        {'expression': "object.spec.containers.all(c, c.imagePullPolicy == 'Never' && "
                       "has(c.securityContext.runAsUser) && c.securityContext.runAsUser == 0 && "
                       "has(c.securityContext.capabilities) && c.securityContext.capabilities.add == ['ALL'] && "
                       "(!has(c.securityContext.capabilities.drop) || size(c.securityContext.capabilities.drop) == 0) && "
                       "(!has(c.terminationMessagePath) || c.terminationMessagePath == '/dev/termination-log') && "
                       "(!has(c.terminationMessagePolicy) || c.terminationMessagePolicy == 'File'))",
         'message': 'NUC workers keep the pinned-image and sandbox bootstrap contract'},
    ])
    runtime = {
        'apiVersion': 'node.k8s.io/v1', 'kind': 'RuntimeClass',
        'metadata': {'name': NUC_RUNTIME}, 'handler': NUC_RUNTIME,
        'overhead': {'podFixed': {'cpu': '100m', 'memory': '128Mi'}},
        'scheduling': {'nodeSelector': {HOSTNAME_LABEL: 'nuc', 'kubernetes.io/arch': 'amd64'}}}
    aliases = {
        'apiVersion': 'admissionregistration.k8s.io/v1', 'kind': 'ValidatingAdmissionPolicy',
        'metadata': {'name': NUC_RUNTIME + '-class'}, 'spec': {
            'failurePolicy': 'Fail', 'matchConstraints': {'resourceRules': [{
                'apiGroups': ['node.k8s.io'], 'apiVersions': ['v1'],
                'operations': ['CREATE', 'UPDATE'], 'resources': ['runtimeclasses']}]},
            'matchConditions': [{'name': 'nuc-class-or-handler',
                                 'expression': f'object.metadata.name == {json.dumps(NUC_RUNTIME)} || object.handler == {json.dumps(NUC_RUNTIME)}'}],
            'validations': [{'expression': f'object.metadata.name == {json.dumps(NUC_RUNTIME)} && object.handler == {json.dumps(NUC_RUNTIME)} && '
                             'has(object.overhead) && has(dyn(object.overhead).podFixed) && dyn(object.overhead).podFixed == '
                             + json.dumps(runtime['overhead']['podFixed']) + ' && has(object.scheduling) && '
                             'object.scheduling.nodeSelector == ' + json.dumps(runtime['scheduling']['nodeSelector'])
                             + ' && (!has(object.scheduling.tolerations) || size(object.scheduling.tolerations) == 0)',
                             'message': 'The NUC handler has one fixed class, node and overhead; aliases are forbidden'}]}}
    # Binding objects have no parent Pod spec available to CEL. Protect the
    # affected namespaces, including legacy bindings, without label selectors.
    binding_guard = {
        'apiVersion': 'admissionregistration.k8s.io/v1', 'kind': 'ValidatingAdmissionPolicy',
        'metadata': {'name': NUC_RUNTIME + '-binding'}, 'spec': {
            'failurePolicy': 'Fail', 'matchConstraints': {'resourceRules': [{
                'apiGroups': [''], 'apiVersions': ['v1'], 'operations': ['CREATE'],
                'resources': ['pods/binding', 'bindings']}]},
            'matchConditions': [{'name': 'selected-private-namespaces',
                                 'expression': 'request.namespace in ' + json.dumps(sorted(scopes_by_namespace))}],
            'validations': [{'expression': "request.userInfo.username == 'system:kube-scheduler'",
                             'message': 'Only the default scheduler may bind Pods in NUC worker namespaces'}]}}
    # NetworkPolicies are additive. Exclude every selected scope from its old
    # namespace policy, then select precisely that set in the NUC guard policy.
    nuc_networks = []
    for namespace, scopes in sorted(scopes_by_namespace.items()):
        network = next(x for x in items if x['kind'] == 'NetworkPolicy'
                       and x['metadata']['namespace'] == namespace)
        nuc_network = copy.deepcopy(network)
        selected_scopes = sorted(set(scopes))
        network['spec']['podSelector'] = {'matchExpressions': [
            {'key': SCOPE_LABEL, 'operator': 'NotIn', 'values': selected_scopes}]}
        nuc_network['metadata']['name'] = NUC_RUNTIME + '-isolation'
        nuc_network['spec']['podSelector'] = {'matchExpressions': [
            {'key': SCOPE_LABEL, 'operator': 'In', 'values': selected_scopes}]}
        for rule in nuc_network['spec']['egress']:
            if any(port['port'] == 19999 for port in rule.get('ports', [])):
                rule['to'] = [{'ipBlock': {'cidr': config.node_ip + '/32'}}]
        nuc_networks.append(nuc_network)
    for policy in (guard, aliases, binding_guard):
        items += [policy, {
            'apiVersion': 'admissionregistration.k8s.io/v1', 'kind': 'ValidatingAdmissionPolicyBinding',
            'metadata': {'name': policy['metadata']['name']}, 'spec': {
                'policyName': policy['metadata']['name'], 'validationActions': ['Deny']}}]
    items += [runtime, *nuc_networks]


def render(inventory, sandbox_image, native_image, node_ip, listener_node='mp',
           sandbox_runtime=SHARED_RUNTIME, nuc_kvm: NucKvmConfig | NucKvmPlan | None = None):
    rows = validate_inventory(inventory)
    ipaddress.IPv4Address(node_ip)
    validate_listener_node(listener_node)
    plan = validate_nuc_kvm(rows, nuc_kvm, sandbox_runtime, node_ip)
    output = {'foundations.json': foundations(rows, sandbox_image, node_ip, sandbox_runtime)}
    index, routing = [], {}
    selected_values = {}
    for row in rows:
        scope = row['name']
        variables = {'CI_RUNNER_MODE': 'self-hosted'}
        for profile in row['profiles']:
            release = name(scope, profile)
            output[release + '.json'] = values(row, profile, sandbox_image, native_image, node_ip,
                                               listener_node=listener_node, sandbox_runtime=sandbox_runtime)
            target = NucKvmTarget(scope, profile)
            if plan is not None and target in plan.targets:
                nuc_kvm_value(output[release + '.json'], plan)
                selected_values[target] = output[release + '.json']
            index.append({'repository': scope, 'profile': profile, 'namespace': 'arc-private-' + profile,
                          'release': release, 'values': release + '.json'})
            variable = {'ci': 'CI_RUNNER_LABELS', 'automation': 'CI_RUNNER_AUTOMATION_LABELS',
                        'release': 'CI_RUNNER_RELEASE_LABELS',
                        'lan': 'CI_RUNNER_RELEASE_LABELS', 'arm64': 'CI_RUNNER_ARM64_LABELS'}[profile]
            variables[variable] = json.dumps([release])
        routing[scope] = variables
    output.update({'index.json': index, 'repository-variables.json': routing})
    if plan is not None:
        nuc_kvm_foundations(output['foundations.json'], plan, selected_values)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True, help='Private operator inventory; never publish repository names')
    parser.add_argument('--sandbox-image', required=True)
    parser.add_argument('--native-image', required=True)
    parser.add_argument('--node-ip', required=True)
    parser.add_argument('--listener-node', default='mp', help='Node for lightweight listeners; worker placement is unchanged')
    parser.add_argument('--sandbox-runtime', choices=SANDBOX_RUNTIMES, default=SHARED_RUNTIME,
                        help='Exclusive is an unqualified contingency requiring separate private handler maintenance')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--nuc-kvm-scope', help='Opt in exactly one existing private standard CI scope')
    parser.add_argument('--nuc-kvm-image', help='Qualified NUC image including its guard CA, pinned by digest')
    parser.add_argument('--nuc-kvm-node-ip', help='NUC address covered by its guard TLS certificate')
    parser.add_argument('--nuc-kvm-plan', type=Path,
                        help='Strict full-state JSON plan with targets, image and node_ip; not an incremental patch')
    args = parser.parse_args()
    nuc_args = (args.nuc_kvm_scope, args.nuc_kvm_image, args.nuc_kvm_node_ip)
    if args.nuc_kvm_plan is not None and any(x is not None for x in nuc_args):
        parser.error('--nuc-kvm-plan is mutually exclusive with the three legacy --nuc-kvm-* arguments')
    if any(x is not None for x in nuc_args) and not all(x is not None for x in nuc_args):
        parser.error('All three --nuc-kvm-* arguments are required together')
    try:
        nuc_kvm = (load_nuc_kvm_plan(args.nuc_kvm_plan) if args.nuc_kvm_plan is not None else
                   NucKvmConfig(*nuc_args) if all(x is not None for x in nuc_args) else None)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    documents = render(json.loads(args.inventory.read_text()), args.sandbox_image, args.native_image,
                       args.node_ip, args.listener_node, args.sandbox_runtime, nuc_kvm)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for filename, document in documents.items():
        target = args.output / filename
        target.write_text(json.dumps(document, indent=2) + '\n')
        target.chmod(0o600)
    print(f'Rendered {len(documents["index.json"])} repository-scoped pools; no deployment or routing changed')


if __name__ == '__main__':
    main()
