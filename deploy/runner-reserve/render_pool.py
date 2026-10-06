#!/usr/bin/env python3
"""Render public standby pools; never changes GitHub routing or installs resources."""
from __future__ import annotations
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
LABEL = 'runner-reserve.github.io/role'
NETWORK = json.loads((ROOT / 'network-policy.json').read_text())
DENIED = NETWORK['denied_networks'] + NETWORK['extra_denied']


def release_name(scope, profile):
    """Kubernetes names must be unique even when repository routing labels match."""
    return 'reserve-' + profile + '-' + hashlib.sha256(scope.encode()).hexdigest()[:12]


def pool_values(scope, profile, image, node, node_ip, inventory):
    """Separate profiles and owners; all workers start from a fresh filesystem."""
    organizations = inventory['organizations']
    personal = [r['name'] for r in inventory['repositories']
                if r['name'].split('/')[0] not in organizations]
    if scope not in [*organizations, *personal] or profile not in inventory['profiles']:
        raise ValueError('Scope/profile is not in the public reserve inventory')
    if not re.fullmatch(r'[a-z0-9./:_-]+@sha256:[0-9a-f]{64}', image):
        raise ValueError('A qualified image pinned by digest is required')
    ipaddress.IPv4Address(node_ip)
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,62}', node):
        raise ValueError('Invalid node name')
    profile_label = inventory['profiles'][profile]
    container = {'name': 'runner', 'image': image, 'imagePullPolicy': 'Never',
                 'command': ['/opt/victron-ci-reserve/start-runner.sh'],
                 'env': [{'name': 'RUNNER_RESERVE_NODE_IP', 'value': node_ip},
                         {'name': 'RUNNER_RESERVE_POD_UID', 'valueFrom': {
                             'fieldRef': {'fieldPath': 'metadata.uid'}}}],
                 'resources': {'requests': {'cpu': '500m', 'memory': '2Gi', 'ephemeral-storage': '5Gi'},
                               'limits': {'cpu': '2', 'memory': '4Gi', 'ephemeral-storage': '20Gi'}},
                 # Capabilities are emulated inside Sentry, never a privileged host container.
                 # Combining add/drop ALL makes containerd emit an empty capability set.
                 'securityContext': {'runAsUser': 0, 'privileged': False,
                                     'capabilities': {'add': ['ALL']}}}
    result = {'githubConfigUrl': 'https://github.com/' + scope,
              'githubConfigSecret': 'reserve-org-app' if scope in organizations else 'reserve-personal-app',
              'runnerScaleSetName': release_name(scope, profile),
              'scaleSetLabels': [profile_label], 'minRunners': 0, 'maxRunners': 1,
              'controllerServiceAccount': {'namespace': 'runner-reserve-system',
                                           'name': 'victron-reserve-controller'},
              'listenerTemplate': {'spec': {'nodeSelector': {'kubernetes.io/hostname': node},
                  'containers': [{'name': 'listener', 'resources': {
                      'requests': {'cpu': '10m', 'memory': '40Mi'},
                      'limits': {'cpu': '100m', 'memory': '128Mi'}},
                      'securityContext': {'runAsNonRoot': True, 'runAsUser': 65532,
                          'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                          'capabilities': {'drop': ['ALL']}}}]}},
              'template': {'metadata': {'labels': {LABEL: 'worker'}}, 'spec': {
                  'runtimeClassName': 'victron-reserve', 'serviceAccountName': 'reserve-worker',
                  'automountServiceAccountToken': False, 'enableServiceLinks': False,
                  'activeDeadlineSeconds': 14400, 'terminationGracePeriodSeconds': 30,
                  'nodeSelector': {'kubernetes.io/hostname': node, 'kubernetes.io/arch': 'amd64'},
                  'containers': [container]}}}
    if scope in organizations:
        result['runnerGroup'] = 'victron-public-reserve-' + profile
    return result


def foundations(profiles, image, node_ip):
    resources = []
    for profile in profiles:
        ns = 'runner-reserve-' + profile
        resources += [
            {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': ns, 'labels': {
                'app.kubernetes.io/part-of': 'victron-public-reserve',
                'runner-reserve.github.io/admission': 'worker',
                # RuntimeClass + the stricter validating policy govern sandbox capabilities.
                'pod-security.kubernetes.io/enforce': 'privileged'}}},
            {'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': {'name': 'reserve-worker', 'namespace': ns},
             'automountServiceAccountToken': False},
            {'apiVersion': 'v1', 'kind': 'ResourceQuota', 'metadata': {'name': 'reserve-budget', 'namespace': ns},
             'spec': {'hard': {'pods': '2', 'requests.cpu': '1200m', 'requests.memory': '4352Mi',
                               'limits.cpu': '4200m', 'limits.memory': '8448Mi',
                               'requests.ephemeral-storage': '10Gi', 'limits.ephemeral-storage': '40Gi'}}},
            {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
             'metadata': {'name': 'reserve-worker-isolation', 'namespace': ns}, 'spec': {
                 'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'], 'ingress': [],
                 'egress': [
                     {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                              'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
                      'ports': [{'port': 53, 'protocol': 'UDP'}, {'port': 53, 'protocol': 'TCP'}]},
                     {'to': [{'ipBlock': {'cidr': node_ip + '/32'}}],
                      'ports': [{'port': 19999, 'protocol': 'TCP'}]},
                     {'to': [{'ipBlock': {'cidr': '0.0.0.0/0', 'except': DENIED}}],
                      'ports': [{'port': 80, 'protocol': 'TCP'}, {'port': 443, 'protocol': 'TCP'}]}]}}]
    validations = [
        ("has(object.spec.runtimeClassName) && object.spec.runtimeClassName == 'victron-reserve'", 'gVisor is mandatory'),
        ("has(object.metadata.labels) && object.metadata.labels['runner-reserve.github.io/role'] == 'worker'", 'Worker network identity is mandatory'),
        ("has(object.spec.automountServiceAccountToken) && !object.spec.automountServiceAccountToken && object.spec.serviceAccountName == 'reserve-worker'", 'Workers have no Kubernetes API identity'),
        ("(!has(object.spec.hostNetwork) || !object.spec.hostNetwork) && (!has(object.spec.hostPID) || !object.spec.hostPID) && (!has(object.spec.hostIPC) || !object.spec.hostIPC)", 'Host namespaces are forbidden'),
        ("!has(object.spec.volumes) || size(object.spec.volumes) == 0", 'Host, persistent and credential volumes are forbidden'),
        ("(!has(object.spec.initContainers) || size(object.spec.initContainers) == 0) && (!has(object.spec.ephemeralContainers) || size(object.spec.ephemeralContainers) == 0)", 'Only the admitted worker may execute'),
        ("size(object.spec.containers) == 1 && object.spec.containers[0].name == 'runner' && object.spec.containers[0].image == " + json.dumps(image), 'Only the reviewed pinned worker image is allowed'),
        ("object.spec.containers.all(c, has(c.command) && c.command == ['/opt/victron-ci-reserve/start-runner.sh'] && (!has(c.args) || size(c.args) == 0) && (!has(c.envFrom) || size(c.envFrom) == 0) && (!has(c.lifecycle)) && has(c.securityContext) && (!has(c.securityContext.privileged) || !c.securityContext.privileged))", 'Bootstrap cannot be replaced or made host-privileged'),
        ("object.spec.containers.all(c, !has(c.ports) || c.ports.all(p, !has(p.hostPort) || p.hostPort == 0))", 'Host ports are forbidden'),
    ]
    resources += [
        {'apiVersion': 'admissionregistration.k8s.io/v1', 'kind': 'ValidatingAdmissionPolicy',
         'metadata': {'name': 'victron-reserve-workers'}, 'spec': {'failurePolicy': 'Fail',
          'matchConstraints': {'namespaceSelector': {'matchLabels': {'runner-reserve.github.io/admission': 'worker'}},
                               'resourceRules': [{'apiGroups': [''], 'apiVersions': ['v1'],
                                                  'operations': ['CREATE', 'UPDATE'], 'resources': ['pods', 'pods/ephemeralcontainers']}]},
          'validations': [{'expression': expression, 'message': message} for expression, message in validations]}},
        {'apiVersion': 'admissionregistration.k8s.io/v1', 'kind': 'ValidatingAdmissionPolicyBinding',
         'metadata': {'name': 'victron-reserve-workers'}, 'spec': {
             'policyName': 'victron-reserve-workers', 'validationActions': ['Deny']}}]
    return {'apiVersion': 'v1', 'kind': 'List', 'items': resources}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker-image', required=True, help='Preloaded, qualified image reference with sha256 digest')
    parser.add_argument('--node', default='mp')
    parser.add_argument('--node-ip', required=True, help='Address covered by the guard certificate')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    inventory = json.loads((ROOT / 'repositories.json').read_text())
    scopes = [*inventory['organizations'], *[r['name'] for r in inventory['repositories']
              if r['name'].split('/')[0] not in inventory['organizations']]]
    rendered = {}
    index = []
    for scope in scopes:
        for profile in inventory['profiles']:
            release = release_name(scope, profile)
            values = pool_values(scope, profile, args.worker_image, args.node, args.node_ip, inventory)
            rendered[release + '.json'] = values
            index.append({'scope': scope, 'profile': profile, 'release': release,
                          'namespace': 'runner-reserve-' + profile, 'values': release + '.json'})
    rendered['index.json'] = index
    rendered['foundations.json'] = foundations(inventory['profiles'], args.worker_image, args.node_ip)
    # Do not overwrite an earlier deployment plan or any credential file.
    args.output.mkdir(parents=True, exist_ok=False)
    for name, data in rendered.items():
        (args.output / name).write_text(json.dumps(data, indent=2) + '\n')
    print(f'Rendered {len(index)} standby pools; nothing installed and routing unchanged')


if __name__ == '__main__':
    main()
