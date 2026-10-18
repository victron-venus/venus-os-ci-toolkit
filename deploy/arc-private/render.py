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
exec ./run.sh
'''


def name(scope, profile):
    return 'private-' + hashlib.sha256(scope.encode()).hexdigest()[:12] + '-' + profile


def validate_inventory(inventory):
    rows = inventory['repositories']
    seen = set()
    for row in rows:
        scope = row['name']
        if not SCOPE.fullmatch(scope) or scope in seen or row.get('visibility') != 'private':
            raise ValueError('Inventory requires unique explicitly private owner/repository scopes')
        seen.add(scope)
        if not row['profiles'] or set(row['profiles']) - set(PROFILES):
            raise ValueError('Unsupported or empty worker profile')
        for destination in row.get('lan_egress', []):
            network = ipaddress.ip_network(destination['cidr'])
            if network.version != 4 or network.prefixlen != 32:
                raise ValueError('LAN destinations must be exact IPv4 /32 hosts')
            if type(destination['port']) is not int or not 1 <= destination['port'] <= 65535:
                raise ValueError('Invalid LAN TCP port')
        if 'lan' in row['profiles'] and not row.get('lan_egress'):
            raise ValueError('LAN profile requires explicit destinations')
    return rows


def values(row, profile, sandbox_image, native_image, node_ip, arm_node='h7'):
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
    listener['resources'] = {'requests': {'cpu': '5m', 'memory': '32Mi'},
                             'limits': {'cpu': '100m', 'memory': '192Mi'}}
    spec = result['template']['spec']
    result['template']['metadata']['labels']['private-runners.github.io/scope'] = name(scope, 'scope')
    result['template']['metadata']['labels']['private-runners.github.io/profile'] = profile
    spec['restartPolicy'] = 'Never'
    spec['serviceAccountName'] = 'reserve-worker' if profile in ('ci', 'automation', 'release') else 'private-worker'
    spec['containers'][0]['resources'] = {
        'requests': {'cpu': '250m' if profile == 'ci' else '50m',
                     'memory': '1Gi' if profile == 'ci' else '512Mi', 'ephemeral-storage': '1Gi'},
        'limits': {'cpu': '4' if profile == 'ci' else '1',
                   'memory': '6Gi' if profile == 'ci' else '2Gi', 'ephemeral-storage': '20Gi'}}
    if profile in ('ci', 'automation', 'release'):
        return result
    spec.pop('runtimeClassName')
    result['template']['metadata']['labels'].pop(RESERVE.LABEL)
    spec['nodeSelector'] = {'kubernetes.io/hostname': arm_node if profile == 'arm64' else 'mp',
                            'kubernetes.io/arch': 'arm64' if profile == 'arm64' else 'amd64'}
    spec['securityContext'] = {'runAsUser': 1001, 'runAsGroup': 121, 'fsGroup': 121,
                               'seccompProfile': {'type': 'RuntimeDefault'}}
    security = {'runAsNonRoot': True, 'allowPrivilegeEscalation': False,
                'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}
    mounts = {'home': '/home/runner', 'tools': '/opt/hostedtoolcache', 'tmp': '/tmp'}
    runner = {'name': 'runner', 'image': native_image, 'imagePullPolicy': 'IfNotPresent',
              'command': ['/bin/bash', '-c'], 'args': [NATIVE_BOOT], 'securityContext': security,
              'env': [{'name': 'HOME', 'value': '/home/runner'},
                      {'name': 'RUNNER_TOOL_CACHE', 'value': '/opt/hostedtoolcache'},
                      {'name': 'AGENT_TOOLSDIRECTORY', 'value': '/opt/hostedtoolcache'},
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
            'args': ['exec dockerd --host=unix:///docker-socket/docker.sock --group=121 --data-root=/docker-data --storage-driver=vfs'],
            'securityContext': {'privileged': True, 'runAsUser': 0, 'runAsGroup': 0},
            'resources': {'requests': {'cpu': '50m', 'memory': '512Mi', 'ephemeral-storage': '512Mi'},
                          'limits': {'cpu': '1', 'memory': '2Gi', 'ephemeral-storage': '16Gi'}},
            'volumeMounts': [{'name': 'docker-socket', 'mountPath': '/docker-socket'},
                             {'name': 'docker-data', 'mountPath': '/docker-data'},
                             {'name': 'home', 'mountPath': '/home/runner'}]}]
        spec['volumes'] += [{'name': 'docker-socket', 'emptyDir': {'sizeLimit': '64Mi'}},
                            {'name': 'docker-data', 'emptyDir': {'sizeLimit': '16Gi'}}]
    return result


def foundations(rows, sandbox_image, node_ip):
    # Copy the reviewed admission contract into a distinct private policy so
    # updating private job dependencies cannot change the public image boundary.
    rendered = RESERVE.foundations(['ci', 'automation', 'release'], sandbox_image, node_ip)
    resources = []
    for item in rendered['items']:
        item = copy.deepcopy(item)
        meta = item['metadata']
        if item['kind'] == 'ValidatingAdmissionPolicy':
            meta['name'] = 'private-ephemeral-workers'
            item['spec']['matchConstraints']['namespaceSelector']['matchLabels'] = {
                'runner-reserve.github.io/admission': 'private-worker'}
            resources.append(item)
            continue
        if item['kind'] == 'ValidatingAdmissionPolicyBinding':
            meta['name'] = 'private-ephemeral-workers'
            item['spec']['policyName'] = 'private-ephemeral-workers'
            resources.append(item)
            continue
        if item['kind'] == 'Namespace':
            meta['name'] = meta['name'].replace('runner-reserve-', 'arc-private-')
            meta['labels']['app.kubernetes.io/part-of'] = 'private-ephemeral-runners'
            meta['labels']['runner-reserve.github.io/admission'] = 'private-worker'
        else:
            meta['namespace'] = meta['namespace'].replace('runner-reserve-', 'arc-private-')
        if item['kind'] == 'ResourceQuota':
            # Listeners are persistent, workers are bounded in aggregate even if
            # many repositories queue simultaneously. Scheduler retains node limits.
            item['spec']['hard'] = {'pods': '30', 'requests.cpu': '2500m', 'requests.memory': '8Gi',
                                    'limits.cpu': '40', 'limits.memory': '48Gi',
                                    'requests.ephemeral-storage': '12Gi', 'limits.ephemeral-storage': '160Gi'}
        resources.append(item)
    for profile in ('lan', 'arm64'):
        if not any(profile in r['profiles'] for r in rows):
            continue
        ns = 'arc-private-' + profile
        resources += [
            {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': ns, 'labels': {
                'app.kubernetes.io/part-of': 'private-ephemeral-runners',
                'pod-security.kubernetes.io/enforce': 'privileged' if profile == 'arm64' else 'baseline'}}},
            {'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': {'name': 'private-worker', 'namespace': ns},
             'automountServiceAccountToken': False},
            {'apiVersion': 'v1', 'kind': 'ResourceQuota', 'metadata': {'name': 'private-budget', 'namespace': ns},
             'spec': {'hard': {'pods': '8', 'requests.cpu': '500m', 'requests.memory': '4Gi',
                               'limits.cpu': '12', 'limits.memory': '20Gi',
                               'requests.ephemeral-storage': '4Gi', 'limits.ephemeral-storage': '80Gi'}}}]
        base = next(copy.deepcopy(r) for r in resources if r['kind'] == 'NetworkPolicy')
        base['metadata'] = {'name': 'private-worker-isolation', 'namespace': ns}
        base['spec']['egress'] = [r for r in base['spec']['egress'] if r['ports'][0]['port'] != 19999]
        resources.append(base)
    for row in rows:
        if 'lan' in row['profiles']:
            resources.append({'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
                'metadata': {'name': name(row['name'], 'lan'), 'namespace': 'arc-private-lan'},
                'spec': {'podSelector': {'matchLabels': {'private-runners.github.io/scope': name(row['name'], 'scope')}},
                         'policyTypes': ['Egress'], 'egress': [
                             {'to': [{'ipBlock': {'cidr': d['cidr']}}],
                              'ports': [{'port': d['port'], 'protocol': 'TCP'}]} for d in row['lan_egress']]}})
    return {'apiVersion': 'v1', 'kind': 'List', 'items': resources}


def render(inventory, sandbox_image, native_image, node_ip):
    rows = validate_inventory(inventory)
    ipaddress.IPv4Address(node_ip)
    output = {'foundations.json': foundations(rows, sandbox_image, node_ip)}
    index, routing = [], {}
    for row in rows:
        scope = row['name']
        variables = {'CI_RUNNER_MODE': 'self-hosted'}
        for profile in row['profiles']:
            release = name(scope, profile)
            output[release + '.json'] = values(row, profile, sandbox_image, native_image, node_ip)
            index.append({'repository': scope, 'profile': profile, 'namespace': 'arc-private-' + profile,
                          'release': release, 'values': release + '.json'})
            variable = {'ci': 'CI_RUNNER_LABELS', 'automation': 'CI_RUNNER_AUTOMATION_LABELS',
                        'release': 'CI_RUNNER_RELEASE_LABELS',
                        'lan': 'CI_RUNNER_RELEASE_LABELS', 'arm64': 'CI_RUNNER_ARM64_LABELS'}[profile]
            variables[variable] = json.dumps([release])
        if 'CI_RUNNER_RELEASE_LABELS' not in variables:
            variables['CI_RUNNER_RELEASE_LABELS'] = variables['CI_RUNNER_AUTOMATION_LABELS']
        routing[scope] = variables
    output.update({'index.json': index, 'repository-variables.json': routing})
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True, help='Private operator inventory; never publish repository names')
    parser.add_argument('--sandbox-image', required=True)
    parser.add_argument('--native-image', required=True)
    parser.add_argument('--node-ip', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    documents = render(json.loads(args.inventory.read_text()), args.sandbox_image, args.native_image, args.node_ip)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for filename, document in documents.items():
        target = args.output / filename
        target.write_text(json.dumps(document, indent=2) + '\n')
        target.chmod(0o600)
    print(f'Rendered {len(documents["index.json"])} repository-scoped pools; no deployment or routing changed')


if __name__ == '__main__':
    main()
