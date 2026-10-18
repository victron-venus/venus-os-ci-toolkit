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


def name(scope, profile):
    return 'private-' + hashlib.sha256(scope.encode()).hexdigest()[:12] + '-' + profile


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
    return rows


def values(row, profile, sandbox_image, native_image, node_ip, arm_node='h7', listener_node='mp',
           sandbox_runtime=SHARED_RUNTIME):
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
    result['listenerTemplate']['spec']['nodeSelector'] = {HOSTNAME_LABEL: listener_node}
    listener['resources'] = {'requests': {'cpu': '5m', 'memory': '32Mi'},
                             'limits': {'cpu': '100m', 'memory': '192Mi'}}
    spec = result['template']['spec']
    result['template']['metadata']['labels']['private-runners.github.io/scope'] = name(scope, 'scope')
    result['template']['metadata']['labels']['private-runners.github.io/profile'] = profile
    spec['restartPolicy'] = 'Never'
    spec['serviceAccountName'] = 'reserve-worker' if profile in ('ci', 'automation', 'release') else 'private-worker'
    spec['containers'][0]['resources'] = {
        'requests': {'cpu': '900m' if profile == 'ci' else '50m',
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
        spec['containers'][0]['env'].append({'name': 'RUNNER_DOCKER_STORAGE_DRIVER', 'value': 'overlay2'})
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


def sandbox_quota(profile):
    # ARC keeps listeners in its controller namespace. This bounds aggregate
    # worker demand, including RuntimeClass overhead, across all repositories.
    budget = {'ci': ('8200m', '17Gi'), 'automation': ('2200m', '9Gi'), 'release': ('2200m', '5Gi')}[profile]
    return {'pods': '2', 'requests.cpu': '2000m' if profile == 'ci' else '1000m',
            'requests.memory': '14Gi' if profile == 'ci' else '3Gi',
            'limits.cpu': budget[0], 'limits.memory': budget[1],
            'requests.ephemeral-storage': '3Gi', 'limits.ephemeral-storage': '40Gi'}


def private_sandbox_foundation(source, sandbox_runtime=SHARED_RUNTIME):
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
            item['spec']['hard'] = sandbox_quota(meta['namespace'].removeprefix('arc-private-'))
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
    validations.extend([
        {'expression': "object.spec.containers.all(c, has(c.volumeMounts) && size(c.volumeMounts) == 1 && c.volumeMounts.all(m, m.name == 'docker-data' && m.mountPath == '/var/lib/docker' && !has(m.subPath) && !has(m.subPathExpr) && !has(m.mountPropagation) && (!has(m.readOnly) || !m.readOnly)))",
         'message': 'Docker scratch cannot be redirected, shared or mounted elsewhere'},
        {'expression': "object.spec.containers.all(c, has(c.env) && size(c.env.filter(e, e.name == 'RUNNER_DOCKER_STORAGE_DRIVER')) == 1 && c.env.exists(e, e.name == 'RUNNER_DOCKER_STORAGE_DRIVER' && has(e.value) && e.value == 'overlay2' && !has(e.valueFrom)))",
         'message': 'Private workers require the qualified overlay driver'},
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
    resources = [private_sandbox_foundation(item, sandbox_runtime) for item in rendered['items']]
    if sandbox_runtime == EXCLUSIVE_RUNTIME:
        private_policy = next(item for item in resources if item['kind'] == 'ValidatingAdmissionPolicy')
        resources.extend(exclusive_runtime_resources(private_policy))
    network = next(r for r in resources if r['kind'] == 'NetworkPolicy')
    for profile in ('lan', 'arm64'):
        if any(profile in r['profiles'] for r in rows):
            resources.extend(native_foundations(profile, network))
    resources.extend(lan_policy(row) for row in rows if 'lan' in row['profiles'])
    return {'apiVersion': 'v1', 'kind': 'List', 'items': resources}


def render(inventory, sandbox_image, native_image, node_ip, listener_node='mp',
           sandbox_runtime=SHARED_RUNTIME):
    rows = validate_inventory(inventory)
    ipaddress.IPv4Address(node_ip)
    if not isinstance(listener_node, str) or not re.fullmatch(r'[a-z0-9](?:[-a-z0-9.]{0,61}[a-z0-9])?', listener_node):
        raise ValueError('Listener node must be a valid hostname label')
    output = {'foundations.json': foundations(rows, sandbox_image, node_ip, sandbox_runtime)}
    index, routing = [], {}
    for row in rows:
        scope = row['name']
        variables = {'CI_RUNNER_MODE': 'self-hosted'}
        for profile in row['profiles']:
            release = name(scope, profile)
            output[release + '.json'] = values(row, profile, sandbox_image, native_image, node_ip,
                                               listener_node=listener_node, sandbox_runtime=sandbox_runtime)
            index.append({'repository': scope, 'profile': profile, 'namespace': 'arc-private-' + profile,
                          'release': release, 'values': release + '.json'})
            variable = {'ci': 'CI_RUNNER_LABELS', 'automation': 'CI_RUNNER_AUTOMATION_LABELS',
                        'release': 'CI_RUNNER_RELEASE_LABELS',
                        'lan': 'CI_RUNNER_RELEASE_LABELS', 'arm64': 'CI_RUNNER_ARM64_LABELS'}[profile]
            variables[variable] = json.dumps([release])
        routing[scope] = variables
    output.update({'index.json': index, 'repository-variables.json': routing})
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
    args = parser.parse_args()
    documents = render(json.loads(args.inventory.read_text()), args.sandbox_image, args.native_image,
                       args.node_ip, args.listener_node, args.sandbox_runtime)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for filename, document in documents.items():
        target = args.output / filename
        target.write_text(json.dumps(document, indent=2) + '\n')
        target.chmod(0o600)
    print(f'Rendered {len(documents["index.json"])} repository-scoped pools; no deployment or routing changed')


if __name__ == '__main__':
    main()
