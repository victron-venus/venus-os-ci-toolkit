"""Offline boundaries for an opt-in, not-yet-qualified private runtime."""
import copy
import hashlib
import importlib.util
from pathlib import Path
import tomllib
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


arc = load('private_exclusive_render', 'deploy/arc-private/render.py')
composer = load('private_exclusive_compose', 'deploy/arc-private/compose_exclusive_template.py')
SANDBOX = 'localhost/sandbox@sha256:' + 'a' * 64
NATIVE = 'example/runner@sha256:' + 'b' * 64
ROW = {'name': 'owner/private', 'visibility': 'private', 'profiles': ['ci', 'automation', 'release']}


class PrivateExclusiveTests(unittest.TestCase):
    def test_opt_in_changes_only_private_sandbox_runtime(self):
        for profile in ('ci', 'automation', 'release', 'lan', 'arm64'):
            row = {**ROW, 'profiles': [profile], 'lan_egress': [{'cidr': '192.0.2.10/32', 'port': 9443}]}
            base = arc.values(row, profile, SANDBOX, NATIVE, '10.66.10.2')
            candidate = arc.values(row, profile, SANDBOX, NATIVE, '10.66.10.2',
                                   sandbox_runtime=arc.EXCLUSIVE_RUNTIME)
            if profile in ('ci', 'automation', 'release'):
                self.assertEqual(candidate['template']['spec']['runtimeClassName'], arc.EXCLUSIVE_RUNTIME)
                candidate['template']['spec']['runtimeClassName'] = arc.SHARED_RUNTIME
            self.assertEqual(base, candidate)

    def test_default_does_not_emit_exclusive_class_or_policy(self):
        items = arc.foundations([ROW], SANDBOX, '10.66.10.2')['items']
        self.assertFalse(any(x['metadata']['name'] in
                             (arc.EXCLUSIVE_RUNTIME, 'private-exclusive-runtime-use') for x in items))
        with self.assertRaises(ValueError):
            arc.foundations([ROW], SANDBOX, '10.66.10.2', sandbox_runtime='arbitrary-runtime')

    def test_global_guard_cannot_be_bypassed_by_namespace_labels(self):
        items = arc.foundations([ROW], SANDBOX, '10.66.10.2', arc.EXCLUSIVE_RUNTIME)['items']
        policies = {x['metadata']['name']: x for x in items if x['kind'] == 'ValidatingAdmissionPolicy'}
        private = policies[arc.PRIVATE_ADMISSION]['spec']
        guard = policies['private-exclusive-runtime-use']['spec']
        self.assertNotIn('namespaceSelector', guard['matchConstraints'])
        self.assertIn(arc.EXCLUSIVE_RUNTIME, guard['matchConditions'][0]['expression'])
        self.assertEqual(guard['validations'][0]['expression'],
                         "request.namespace in ['arc-private-ci', 'arc-private-automation', 'arc-private-release']")
        self.assertEqual(guard['validations'][1:], private['validations'])
        self.assertEqual(guard['failurePolicy'], 'Fail')
        rule = guard['matchConstraints']['resourceRules'][0]
        self.assertEqual(rule['operations'], ['CREATE', 'UPDATE'])
        self.assertIn('pods/ephemeralcontainers', rule['resources'])
        bindings = [x for x in items if x['kind'] == 'ValidatingAdmissionPolicyBinding']
        self.assertTrue(any(x['spec']['policyName'] == 'private-exclusive-runtime-use'
                            and x['spec']['validationActions'] == ['Deny'] for x in bindings))

    def test_single_lifetime_and_no_annotation_escape(self):
        items = arc.foundations([ROW], SANDBOX, '10.66.10.2', arc.EXCLUSIVE_RUNTIME)['items']
        policy = next(x for x in items if x['kind'] == 'ValidatingAdmissionPolicy')
        expressions = '\n'.join(x['expression'] for x in policy['spec']['validations'])
        for condition in ["object.spec.restartPolicy == 'Never'", "size(object.spec.containers) == 1",
                          'size(object.spec.initContainers) == 0', 'size(object.spec.ephemeralContainers) == 0',
                          "!k.startsWith('dev.gvisor.')", "c.terminationMessagePath == '/dev/termination-log'",
                          "c.terminationMessagePolicy == 'File'", '!has(m.subPath)', '!has(m.mountPropagation)']:
            self.assertIn(condition, expressions)

    def test_class_keeps_scheduling_and_quota_overhead(self):
        items = arc.foundations([ROW], SANDBOX, '10.66.10.2', arc.EXCLUSIVE_RUNTIME)['items']
        candidate = next(x for x in items if x['kind'] == 'RuntimeClass')
        expected = yaml.safe_load((ROOT / 'deploy/runner-reserve/runtime-class.yaml').read_text())
        expected['metadata']['name'] = expected['handler'] = arc.EXCLUSIVE_RUNTIME
        self.assertEqual(candidate, expected)

    def test_public_policy_is_not_mutated(self):
        before = arc.RESERVE.foundations(['ci'], SANDBOX, '10.66.10.2')
        saved = copy.deepcopy(before)
        arc.foundations([ROW], SANDBOX, '10.66.10.2', arc.EXCLUSIVE_RUNTIME)
        self.assertEqual(before, saved)
        self.assertEqual(arc.RESERVE.foundations(['ci'], SANDBOX, '10.66.10.2'), saved)

    def test_static_config_changes_bind_access_only(self):
        shared = tomllib.loads((ROOT / 'deploy/runner-reserve/runsc.toml').read_text())
        private = tomllib.loads((ROOT / 'deploy/arc-private/runsc-exclusive.toml').read_text())
        self.assertEqual(private['runsc_config'].pop('file-access-mounts'), 'exclusive')
        self.assertEqual(private, shared)
        self.assertNotIn('allow-flag-override', private['runsc_config'])

    def test_template_extension_preserves_all_original_bytes(self):
        original = b'# captured full K3s template\nversion = 2\n[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc]\nruntime_type = "io.containerd.runc.v2"\n'
        digest = hashlib.sha256(original).hexdigest()
        result = composer.compose(original, digest)
        self.assertTrue(result.startswith(original + b'\n'))
        parsed = tomllib.loads(result.decode())['plugins']['io.containerd.grpc.v1.cri']['containerd']['runtimes']
        self.assertEqual(parsed['runc']['runtime_type'], 'io.containerd.runc.v2')
        self.assertEqual(parsed[arc.EXCLUSIVE_RUNTIME]['options']['ConfigPath'],
                         '/etc/victron-ci-private-exclusive/runsc.toml')
        self.assertNotIn('{{ template "base"', result.decode())
        with self.assertRaises(ValueError):
            composer.compose(original, '0' * 64)
        with self.assertRaises(ValueError):
            composer.compose(result, hashlib.sha256(result).hexdigest())


if __name__ == '__main__':
    unittest.main()
