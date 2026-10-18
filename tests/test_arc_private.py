"""Private ARC rendering keeps scope, lifecycle and network boundaries explicit."""
import importlib.util
import json
from pathlib import Path
import unittest

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

    def test_sandbox_profiles_preserve_network_guard_and_runtime(self):
        for profile in ('ci', 'automation', 'release'):
            value = arc.values(ROW, profile, SANDBOX, NATIVE, '10.66.10.2')
            pod = value['template']['spec']
            self.assertEqual(pod['runtimeClassName'], 'victron-reserve')
            self.assertEqual(pod['containers'][0]['command'], ['/opt/victron-ci-reserve/start-runner.sh'])
            self.assertEqual(pod['containers'][0]['imagePullPolicy'], 'Never')
            self.assertEqual(pod.get('volumes', []), [])

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
