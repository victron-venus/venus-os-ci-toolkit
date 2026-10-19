#!/usr/bin/env python3
"""Render public standby pools; never changes GitHub routing or installs resources."""
from __future__ import annotations
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat

ROOT = Path(__file__).resolve().parent
LABEL = 'runner-reserve.github.io/role'
NETWORK = json.loads((ROOT / 'network-policy.json').read_text())
DENIED = NETWORK['denied_networks'] + NETWORK['extra_denied']


def release_name(scope, profile):
    """Kubernetes names must be unique even when repository routing labels match."""
    return 'reserve-' + profile + '-' + hashlib.sha256(scope.encode()).hexdigest()[:12]


def installation_secret(scope, inventory):
    """Validate separate organization installations before choosing a Secret."""
    organizations = inventory['organizations']
    organization_secrets = inventory.get('organization_secrets')
    if organization_secrets is not None:
        if (not isinstance(organization_secrets, dict)
                or set(organization_secrets) != set(organizations)
                or any(not isinstance(value, str)
                       or not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?', value)
                       for value in organization_secrets.values())
                or len(set(organization_secrets.values())) != len(organization_secrets)):
            raise ValueError('Each organization needs an explicit valid Secret name')
    elif len(organizations) > 1:
        raise ValueError('Multiple organizations need separate installation Secret references')
    if scope not in organizations:
        return 'reserve-personal-app'
    if organization_secrets is not None:
        return organization_secrets[scope]
    return 'reserve-org-app'


def valid_listener_node(value):
    return (isinstance(value, str) and 0 < len(value) <= 63
            and all(re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', label)
                    for label in value.split('.')))


def unique_placement_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate listener placement key')
        result[key] = value
    return result


def read_operator_json(path):
    """Read a bounded regular JSON input, rejecting final symlinks and duplicate keys."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Operator input must be a regular file')
        raw = stream.read(131073)
    if len(raw) > 131072:
        raise ValueError('Operator input exceeds 128 KiB')
    return json.loads(raw, object_pairs_hook=unique_placement_keys)


def validate_public_organizations(organizations):
    pattern = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?'
    if (not isinstance(organizations, list)
            or any(not isinstance(owner, str) or not re.fullmatch(pattern, owner)
                   for owner in organizations)
            or len({owner.lower() for owner in organizations}) != len(organizations)):
        raise ValueError('Organizations must be unique GitHub owner names')


def validate_public_profiles(profiles):
    if (not isinstance(profiles, dict) or set(profiles) != {'ci', 'automation', 'release'}
            or any(not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,62}', label)
                   for label in profiles.values())
            or len({label.lower() for label in profiles.values()}) != 3):
        raise ValueError('Three distinct CI, automation and release labels are required')


def validate_public_repositories(repositories, organizations):
    if not isinstance(repositories, list) or not repositories:
        raise ValueError('Public inventory requires repositories')
    seen = set()
    personal_owners = set()
    owner_pattern = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?'
    owners = {owner.lower(): owner for owner in organizations}
    for entry in repositories:
        if (not isinstance(entry, dict) or set(entry) != {'name', 'default_branch'}
                or not isinstance(entry['name'], str)
                or not re.fullmatch(owner_pattern + r'/[A-Za-z0-9_.-]{1,100}', entry['name'])
                or entry['name'].split('/')[1] in {'.', '..'}
                or entry['name'].lower() in seen
                or not isinstance(entry['default_branch'], str) or not entry['default_branch']
                or re.search(r'[\x00-\x20\x7f]', entry['default_branch'])):
            raise ValueError('Repositories need unique owner/name and a default branch')
        owner = entry['name'].split('/')[0]
        if owner.lower() in owners and owner != owners[owner.lower()]:
            raise ValueError('Organization spelling must match the repository owner')
        if owner.lower() not in owners:
            personal_owners.add(owner.lower())
        if len(personal_owners) > 1:
            raise ValueError('The personal installation Secret supports only one personal owner')
        seen.add(entry['name'].lower())


def load_public_inventory(path):
    """Validate explicit public scope data without changing routing or permissions."""
    data = read_operator_json(path)
    required = {'schema', 'default_mode', 'organizations', 'profiles', 'repositories'}
    if (not isinstance(data, dict) or not required <= set(data)
            or set(data) - required - {'organization_secrets'}
            or type(data['schema']) is not int or data['schema'] != 1
            or data['default_mode'] != 'github'):
        raise ValueError('Public inventory needs schema 1 and default_mode github')
    validate_public_organizations(data['organizations'])
    validate_public_profiles(data['profiles'])
    validate_public_repositories(data['repositories'], data['organizations'])
    for owner in data['organizations']:
        installation_secret(owner, data)
    installation_secret(data['repositories'][0]['name'], data)
    personal = any(row['name'].split('/')[0] not in data['organizations']
                   for row in data['repositories'])
    if personal and 'reserve-personal-app' in (data.get('organization_secrets') or {}).values():
        raise ValueError('Organization and personal installations need separate Secrets')
    return data


def load_listener_placement(path, scopes, profiles):
    """Require a complete placement plan before rendering any output."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Listener placement must be a regular file')
        raw = stream.read(131073)
    if len(raw) > 131072:
        raise ValueError('Listener placement file exceeds 128 KiB')
    plan = json.loads(raw, object_pairs_hook=unique_placement_keys)
    if (not isinstance(plan, dict) or set(plan) != {'schema', 'placements'}
            or type(plan['schema']) is not int or plan['schema'] != 1
            or not isinstance(plan['placements'], dict)
            or set(plan['placements']) != set(scopes)):
        raise ValueError('Listener placement must name every inventory scope exactly once')
    placement = {}
    for scope, entries in plan['placements'].items():
        if (not isinstance(entries, dict) or set(entries) != set(profiles)
                or any(not valid_listener_node(node) for node in entries.values())):
            raise ValueError('Every listener scope needs all profiles and valid node names')
        placement.update({(scope, profile): node for profile, node in entries.items()})
    return placement


def pool_values(scope, profile, image, node, node_ip, inventory, *, held=False,
                listener_node=None):
    """Separate profiles and owners; all workers start from a fresh filesystem."""
    organizations = inventory['organizations']
    personal = [r['name'] for r in inventory['repositories']
                if r['name'].split('/')[0] not in organizations]
    if scope not in [*organizations, *personal] or profile not in inventory['profiles']:
        raise ValueError('Scope/profile is not in the public reserve inventory')
    if type(held) is not bool:
        raise ValueError('Held state must be a boolean')
    secret = installation_secret(scope, inventory)
    if not re.fullmatch(r'[a-z0-9./:_-]+@sha256:[0-9a-f]{64}', image):
        raise ValueError('A qualified image pinned by digest is required')
    ipaddress.IPv4Address(node_ip)
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,62}', node):
        raise ValueError('Invalid node name')
    if listener_node is not None and not valid_listener_node(listener_node):
        raise ValueError('Invalid listener node name')
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
              'githubConfigSecret': secret,
              'runnerScaleSetName': release_name(scope, profile),
              'scaleSetLabels': [profile_label], 'minRunners': 0, 'maxRunners': 0 if held else 1,
              'controllerServiceAccount': {'namespace': 'runner-reserve-system',
                                           'name': 'victron-reserve-controller'},
              'listenerTemplate': {'spec': {'nodeSelector': {'kubernetes.io/hostname':
                                                          node if listener_node is None else listener_node},
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
    # ARC 0.15 injects one job-scoped JIT Secret named after its runner Pod.
    # CEL validates the structural reference, not existence/ownership of the
    # referenced Secret. Controller RBAC remains the authority for creating it.
    jit_reference = (
        "e.name == 'ACTIONS_RUNNER_INPUT_JITCONFIG' && "
        "e.valueFrom.secretKeyRef.name == object.metadata.name && "
        "e.valueFrom.secretKeyRef.key == 'jitToken' && "
        "(!has(e.valueFrom.secretKeyRef.optional) || !e.valueFrom.secretKeyRef.optional) && "
        "has(object.metadata.ownerReferences) && object.metadata.ownerReferences.exists(o, "
        "o.apiVersion == 'actions.github.com/v1alpha1' && o.kind == 'EphemeralRunner' && "
        "o.name == object.metadata.name && size(o.uid) > 0 && has(o.controller) && o.controller)")
    validations = [
        ("has(object.spec.runtimeClassName) && object.spec.runtimeClassName == 'victron-reserve'", 'gVisor is mandatory'),
        ("has(object.metadata.labels) && object.metadata.labels['runner-reserve.github.io/role'] == 'worker'", 'Worker network identity is mandatory'),
        ("has(object.spec.automountServiceAccountToken) && !object.spec.automountServiceAccountToken && object.spec.serviceAccountName == 'reserve-worker'", 'Workers have no Kubernetes API identity'),
        ("(!has(object.spec.hostNetwork) || !object.spec.hostNetwork) && (!has(object.spec.hostPID) || !object.spec.hostPID) && (!has(object.spec.hostIPC) || !object.spec.hostIPC)", 'Host namespaces are forbidden'),
        ("!has(object.spec.volumes) || size(object.spec.volumes) == 0", 'Host, persistent and credential volumes are forbidden'),
        ("(!has(object.spec.initContainers) || size(object.spec.initContainers) == 0) && (!has(object.spec.ephemeralContainers) || size(object.spec.ephemeralContainers) == 0)", 'Only the admitted worker may execute'),
        ("size(object.spec.containers) == 1 && object.spec.containers[0].name == 'runner' && object.spec.containers[0].image == " + json.dumps(image), 'Only the reviewed pinned worker image is allowed'),
        ("object.spec.containers.all(c, has(c.command) && c.command == ['/opt/victron-ci-reserve/start-runner.sh'] && (!has(c.args) || size(c.args) == 0) && (!has(c.envFrom) || size(c.envFrom) == 0) && (!has(c.lifecycle)) && has(c.securityContext) && (!has(c.securityContext.privileged) || !c.securityContext.privileged))", 'Bootstrap cannot be replaced or made host-privileged'),
        ("object.spec.containers.all(c, !has(c.env) || (size(c.env.filter(e, e.name == 'ACTIONS_RUNNER_INPUT_JITCONFIG')) <= 1 && c.env.all(e, !has(e.valueFrom) || (!has(e.valueFrom.configMapKeyRef) && (!has(e.valueFrom.secretKeyRef) || (" + jit_reference + "))))))", 'Only the structurally matched ARC job JIT Secret may be referenced by environment'),
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
    parser.add_argument('--inventory', type=Path, default=ROOT / 'repositories.json',
                        help='Explicit public inventory; defaults to the checked-in public catalog')
    parser.add_argument('--worker-image', required=True, help='Preloaded, qualified image reference with sha256 digest')
    parser.add_argument('--node', default='mp')
    parser.add_argument('--node-ip', required=True, help='Address covered by the guard certificate')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--held', action='store_true',
                        help='Render registration-only pools with minRunners=maxRunners=0')
    parser.add_argument('--listener-placement', type=Path,
                        help='Complete scope/profile listener node map; workers retain --node')
    args = parser.parse_args()
    try:
        inventory = load_public_inventory(args.inventory)
        scopes = [*inventory['organizations'], *[r['name'] for r in inventory['repositories']
                  if r['name'].split('/')[0] not in inventory['organizations']]]
        placement = (load_listener_placement(args.listener_placement, scopes, inventory['profiles'])
                     if args.listener_placement is not None else {})
    except (OSError, ValueError) as error:
        parser.error(str(error))
    rendered = {}
    index = []
    for scope in scopes:
        for profile in inventory['profiles']:
            release = release_name(scope, profile)
            values = pool_values(scope, profile, args.worker_image, args.node, args.node_ip, inventory,
                                 held=args.held, listener_node=placement.get((scope, profile)))
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
