"""Private ARC rendering keeps scope, lifecycle and network boundaries explicit."""
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('arc_private', ROOT / 'deploy/arc-private/render.py')
arc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(arc)
SANDBOX = 'localhost/sandbox@sha256:' + 'a' * 64
NATIVE = 'example/runner@sha256:' + 'b' * 64
ROW = {'name': 'owner/private', 'visibility': 'private',
       'profiles': ['ci', 'automation', 'release', 'lan', 'arm64'],
       'lan_egress': [{'cidr': '192.0.2.10/32', 'port': 9443}]}


class PrivatePoolTests(unittest.TestCase):
    def test_only_unique_explicit_private_scopes_are_admitted(self):
        for row in [{**ROW, 'visibility': 'public'}, {**ROW, 'name': 'owner/private;command'},
                    {**ROW, 'profiles': ['unknown']}, {**ROW, 'lan_egress': []},
                    {**ROW, 'lan_egress': [{'cidr': '0.0.0.0/0', 'port': 443}]}]:
            with self.subTest(row=row), self.assertRaises(ValueError):
                arc.validate_inventory({'repositories': [row]})
        with self.assertRaises(ValueError):
            arc.validate_inventory({'repositories': [ROW, ROW]})

    def test_all_profiles_are_repository_scoped_ephemeral_and_credential_free(self):
        for profile in ROW['profiles']:
            value = arc.values(ROW, profile, SANDBOX, NATIVE, '10.66.10.2')
            pod = value['template']['spec']
            with self.subTest(profile=profile):
                self.assertEqual(value['githubConfigUrl'], 'https://github.com/owner/private')
                self.assertEqual(value['githubConfigSecret'], 'private-runner-app-owner')
                self.assertEqual((value['minRunners'], value['maxRunners']), (0, 1))
                self.assertEqual(pod['restartPolicy'], 'Never')
                self.assertFalse(pod['automountServiceAccountToken'])
                self.assertEqual(pod['activeDeadlineSeconds'], 14400)
                self.assertTrue(all('emptyDir' in v for v in pod.get('volumes', [])))
                serialized = json.dumps(pod)
                for forbidden in ['secretKeyRef', 'secretName', 'hostPath', 'persistentVolumeClaim', 'hostNetwork']:
                    self.assertNotIn(forbidden, serialized)
                self.assertNotEqual(value['runnerScaleSetName'], arc.name('other/private', profile))

    def test_listeners_satisfy_restricted_namespace_security(self):
        for profile in ROW['profiles']:
            pod = arc.values(ROW, profile, SANDBOX, NATIVE, '10.66.10.2')['listenerTemplate']['spec']
            self.assertEqual(pod['securityContext']['seccompProfile']['type'], 'RuntimeDefault')
            security = pod['containers'][0]['securityContext']
            self.assertTrue(security['runAsNonRoot'])
            self.assertFalse(security['allowPrivilegeEscalation'])
            self.assertEqual(security['capabilities']['drop'], ['ALL'])

    def test_sandbox_profiles_preserve_network_guard_and_runtime(self):
        for profile in ('ci', 'automation', 'release'):
            value = arc.values(ROW, profile, SANDBOX, NATIVE, '10.66.10.2')
            pod = value['template']['spec']
            self.assertEqual(pod['runtimeClassName'], 'victron-reserve')
            self.assertEqual(pod['containers'][0]['command'], ['/opt/victron-ci-reserve/start-runner.sh'])
            self.assertEqual(pod['containers'][0]['imagePullPolicy'], 'Never')
            self.assertEqual(pod['volumes'], [{'name': 'docker-data', 'emptyDir': {'sizeLimit': '16Gi'}}])
            self.assertEqual(pod['containers'][0]['volumeMounts'],
                             [{'name': 'docker-data', 'mountPath': '/var/lib/docker'}])
            self.assertIn({'name': 'RUNNER_DOCKER_STORAGE_DRIVER', 'value': 'overlay2'},
                          pod['containers'][0]['env'])

    def test_private_scratch_policy_does_not_change_public_mount_policy(self):
        private = arc.foundations([ROW], SANDBOX, '10.66.10.2')['items']
        public = arc.RESERVE.foundations(['ci'], SANDBOX, '10.66.10.2')['items']
        def policy(items):
            return next(x for x in items if x['kind'] == 'ValidatingAdmissionPolicy')['spec']['validations']
        self.assertIn('Host, persistent and credential volumes are forbidden',
                      [x['message'] for x in policy(public)])
        expressions = '\n'.join(x['expression'] for x in policy(private))
        for condition in ["size(object.spec.volumes) == 1", "has(dyn(v.emptyDir).sizeLimit)",
                          "type(dyn(v.emptyDir).sizeLimit) == string", "dyn(v.emptyDir).sizeLimit == '16Gi'",
                          "v.emptyDir.medium == ''", "m.mountPath == '/var/lib/docker'",
                          '!has(m.subPath)', '!has(m.subPathExpr)', '!has(m.mountPropagation)',
                          "!k.startsWith('dev.gvisor.')"]:
            self.assertIn(condition, expressions)

    def test_listener_placement_does_not_move_workers(self):
        row = {**ROW, 'profiles': ['ci', 'automation', 'lan', 'arm64']}
        documents = arc.render({'repositories': [row]}, SANDBOX, NATIVE, '10.66.10.2', listener_node='control-node')
        for profile in row['profiles']:
            value = documents[arc.name(ROW['name'], profile) + '.json']
            self.assertEqual(value['listenerTemplate']['spec']['nodeSelector'],
                             {'kubernetes.io/hostname': 'control-node'})
            self.assertEqual(value['template']['spec']['nodeSelector']['kubernetes.io/hostname'],
                             'h7' if profile == 'arm64' else 'mp')
        with self.assertRaises(ValueError):
            arc.render({'repositories': [row]}, SANDBOX, NATIVE, '10.66.10.2', listener_node='')

    def test_arm_docker_is_native_sidecar_with_disposable_storage(self):
        pod = arc.values(ROW, 'arm64', SANDBOX, NATIVE, '10.66.10.2')['template']['spec']
        self.assertEqual(pod['nodeSelector']['kubernetes.io/arch'], 'arm64')
        self.assertEqual(len(pod['containers']), 1)
        self.assertEqual(pod['initContainers'][0]['restartPolicy'], 'Always')
        self.assertNotIn('runtimeClassName', pod)
        boot = pod['initContainers'][0]['args'][0]
        self.assertEqual(boot, (ROOT / 'deploy/arc-private/native-docker.sh').read_text())
        self.assertIn('--exec-opt=native.cgroupdriver=cgroupfs --cgroup-parent="$parent"', boot)
        self.assertFalse(pod['containers'][0]['securityContext']['allowPrivilegeEscalation'])
        # Docker bind mounts resolve in the daemon container, so both processes
        # must see the same fresh workspace path and volume.
        self.assertIn({'name': 'home', 'mountPath': '/home/runner'},
                      pod['initContainers'][0]['volumeMounts'])

    def test_lan_rule_is_restricted_to_exact_repository_and_destination(self):
        items = arc.foundations([ROW], SANDBOX, '10.66.10.2')['items']
        rule = next(r for r in items if r['metadata']['name'] == arc.name(ROW['name'], 'lan'))
        self.assertEqual(rule['spec']['podSelector']['matchLabels'],
                         {'private-runners.github.io/scope': arc.name(ROW['name'], 'scope')})
        self.assertEqual(rule['spec']['egress'], [{'to': [{'ipBlock': {'cidr': '192.0.2.10/32'}}],
                         'ports': [{'port': 9443, 'protocol': 'TCP'}]}])
        self.assertTrue(all('runner-reserve-' not in r['metadata'].get('namespace', '') for r in items))

    def test_images_must_be_immutable(self):
        for first, second in [('example:latest', NATIVE), (SANDBOX, 'example:v1')]:
            with self.assertRaises(ValueError):
                arc.values(ROW, 'ci', first, second, '10.66.10.2')

    def test_heavy_ci_reserves_capacity_without_admitting_mixed_workers(self):
        row = {**ROW, 'ci_resource_class': 'heavy'}
        pod = arc.values(row, 'ci', SANDBOX, NATIVE, '10.66.10.2')['template']['spec']
        self.assertEqual(pod['containers'][0]['resources']['requests']['memory'], '13Gi')
        self.assertEqual(pod['containers'][0]['resources']['limits']['memory'], '16Gi')
        quotas = [x for x in arc.foundations([row], SANDBOX, '10.66.10.2')['items']
                  if x['kind'] == 'ResourceQuota' and x['metadata']['namespace'] == 'arc-private-ci']
        # 16 GiB + runtime overhead fits, another 6 GiB standard worker does not.
        self.assertEqual(quotas[0]['spec']['hard']['limits.memory'], '17Gi')
        with self.assertRaises(ValueError):
            arc.validate_inventory({'repositories': [{**ROW, 'ci_resource_class': 'unbounded'}]})

    def test_returning_to_standard_preserves_route_and_isolation(self):
        standard = arc.values(ROW, 'ci', SANDBOX, NATIVE, '10.66.10.2')
        heavy = arc.values({**ROW, 'ci_resource_class': 'heavy'}, 'ci',
                           SANDBOX, NATIVE, '10.66.10.2')
        resources = standard['template']['spec']['containers'][0].pop('resources')
        self.assertEqual(resources['requests']['memory'], '1Gi')
        self.assertEqual(resources['limits']['memory'], '6Gi')
        heavy['template']['spec']['containers'][0].pop('resources')
        self.assertEqual(standard, heavy)

    def test_ci_capacity_accounts_for_runtime_overhead(self):
        overhead = yaml.safe_load((ROOT / 'deploy/runner-reserve/runtime-class.yaml').read_text())['overhead']['podFixed']
        quota = arc.sandbox_quota('ci')
        pods = {kind: arc.values({**ROW, 'ci_resource_class': kind}, 'ci',
                                SANDBOX, NATIVE, '10.66.10.2')['template']['spec']
                for kind in ('standard', 'heavy')}

        def quantity(value):
            for unit, scale in [('Gi', 1024 ** 3), ('Mi', 1024 ** 2), ('m', Decimal('0.001'))]:
                if value.endswith(unit):
                    return Decimal(value[:-len(unit)]) * scale
            return Decimal(value)

        for kinds, expected_cpu, fits in [
                (['standard'], '1', True),
                (['standard', 'standard'], '2', True),
                (['standard'] * 3, '3', False),
                (['heavy'], '1.6', True),
                (['heavy', 'standard'], '2.6', False),
                (['heavy', 'heavy'], '3.2', False)]:
            with self.subTest(kinds=kinds):
                usage = {'pods': Decimal(len(kinds))}
                for kind in kinds:
                    self.assertNotIn('initContainers', pods[kind])
                    for category in ('requests', 'limits'):
                        for container in pods[kind]['containers']:
                            for resource, value in container['resources'][category].items():
                                key = category + '.' + resource
                                usage[key] = usage.get(key, Decimal(0)) + quantity(value)
                        for resource, value in overhead.items():
                            key = category + '.' + resource
                            usage[key] += quantity(value)
                self.assertEqual(usage['requests.cpu'], Decimal(expected_cpu))
                self.assertEqual(all(usage[key] <= quantity(value) for key, value in quota.items()), fits)
                if kinds == ['standard', 'standard']:
                    self.assertEqual(usage['limits.cpu'], quantity(quota['limits.cpu']))

    def test_non_ci_profiles_keep_their_cpu_budgets(self):
        for profile in ('automation', 'release', 'lan', 'arm64'):
            with self.subTest(profile=profile):
                pod = arc.values(ROW, profile, SANDBOX, NATIVE, '10.66.10.2')['template']['spec']
                resources = pod['containers'][0]['resources']
                self.assertEqual(resources['requests']['cpu'], '50m')
                self.assertEqual(resources['limits']['cpu'], '1' if profile in ('automation', 'release') else '2')
                if profile == 'arm64':
                    self.assertEqual(pod['initContainers'][0]['resources']['requests']['cpu'], '50m')
                    self.assertEqual(pod['initContainers'][0]['resources']['limits']['cpu'], '1')

    def test_routing_uses_concrete_unique_labels_and_explicit_mode(self):
        row = {**ROW, 'profiles': ['ci', 'automation', 'lan', 'arm64']}
        rendered = arc.render({'repositories': [row]}, SANDBOX, NATIVE, '10.66.10.2')
        variables = rendered['repository-variables.json'][ROW['name']]
        self.assertEqual(variables['CI_RUNNER_MODE'], 'self-hosted')
        self.assertEqual(json.loads(variables['CI_RUNNER_LABELS']), [arc.name(ROW['name'], 'ci')])
        self.assertEqual(json.loads(variables['CI_RUNNER_RELEASE_LABELS']), [arc.name(ROW['name'], 'lan')])
        self.assertEqual(len(rendered['index.json']), 4)

    def test_minimal_inventory_and_ambiguous_routes(self):
        row = {**ROW, 'profiles': ['ci']}
        variables = arc.render({'repositories': [row]}, SANDBOX, NATIVE, '10.66.10.2')['repository-variables.json'][ROW['name']]
        self.assertEqual(set(variables), {'CI_RUNNER_MODE', 'CI_RUNNER_LABELS'})
        for profiles in (['ci', 'ci'], ['ci', 'lan', 'release']):
            with self.subTest(profiles=profiles), self.assertRaises(ValueError):
                arc.render({'repositories': [{**ROW, 'profiles': profiles}]}, SANDBOX, NATIVE, '10.66.10.2')

    def test_guard_namespace_extension_is_explicit_and_excludes_native_workers(self):
        spec = importlib.util.spec_from_file_location('private_guard', ROOT / 'deploy/runner-reserve/node_guard.py')
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        self.assertTrue({'arc-private-ci', 'arc-private-automation', 'arc-private-release'} <= guard.NAMESPACES)
        self.assertFalse({'arc-private-lan', 'arc-private-arm64'} & guard.NAMESPACES)


if __name__ == '__main__':
    unittest.main()
