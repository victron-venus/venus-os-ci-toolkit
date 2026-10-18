"""Offline NUC opt-in contracts; actual Kubernetes CEL/runtime proof stays separate."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('nuc_arc', ROOT / 'deploy/arc-private/render.py')
arc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(arc)
MP = 'localhost/mp@sha256:' + 'a' * 64
NATIVE = 'registry/native@sha256:' + 'b' * 64
NUC = 'localhost/nuc@sha256:' + 'c' * 64
ROW = {'name': 'owner/selected', 'visibility': 'private', 'profiles': ['ci', 'automation', 'release'],
       'ci_storage': 'tmpfs-2gi'}
CONFIG = arc.NucKvmConfig(ROW['name'], NUC, '192.0.2.2')


def render(rows=None, config=CONFIG):
    return arc.render({'repositories': rows or [copy.deepcopy(ROW)]}, MP, NATIVE, '192.0.2.1',
                      listener_node='nuc', nuc_kvm=config)


class NucKvmTests(unittest.TestCase):
    def test_no_opt_in_preserves_base_rendered_bytes(self):
        # Canonical serialized output from the pre-NUC renderer at 6b6cc55.
        # Cover disk, both CI tmpfs sizes, automation tmpfs, release and native.
        rows = [
            {'name': 'owner/standard', 'visibility': 'private',
             'profiles': ['ci', 'automation', 'release'], 'ci_storage': 'tmpfs-2gi'},
            {'name': 'owner/disk', 'visibility': 'private', 'profiles': ['ci', 'automation', 'release']},
            {'name': 'owner/heavy', 'visibility': 'private', 'profiles': ['ci', 'automation'],
             'ci_storage': 'tmpfs-6gi-heavy', 'ci_resource_class': 'heavy', 'automation_storage': 'tmpfs-3gi'},
            {'name': 'owner/native', 'visibility': 'private', 'profiles': ['lan', 'arm64'],
             'lan_egress': [{'cidr': '192.0.2.20/32', 'port': 9443}]},
        ]
        documents = arc.render({'repositories': rows}, MP, NATIVE, '192.0.2.1', listener_node='nuc')
        payload = json.dumps(documents, sort_keys=True, separators=(',', ':')).encode()
        self.assertEqual(hashlib.sha256(payload).hexdigest(),
                         '27d6170b7f92aed10426b2777b93bf2ef2843e21670b283143cda4e1c99b9e64')

    def test_opt_in_changes_only_selected_worker_and_keeps_routing(self):
        rows = [copy.deepcopy(ROW), {**ROW, 'name': 'other/private',
                                    'profiles': ['ci', 'automation', 'lan', 'arm64'],
                                    'lan_egress': [{'cidr': '192.0.2.20/32', 'port': 9443}]}]
        old, new = render(rows, None), render(rows)
        changed = {k for k in old if old[k] != new[k]}
        key = arc.name(ROW['name'], 'ci') + '.json'
        self.assertEqual(changed, {'foundations.json', key})
        self.assertEqual(new['index.json'], old['index.json'])
        self.assertEqual(new['repository-variables.json'], old['repository-variables.json'])
        before, after = copy.deepcopy(old[key]), copy.deepcopy(new[key])
        self.assertEqual((after['minRunners'], after['maxRunners']), (0, 1))
        spec = after['template']['spec']
        self.assertEqual(spec['runtimeClassName'], arc.NUC_RUNTIME)
        self.assertEqual(spec['nodeSelector'], {'kubernetes.io/hostname': 'nuc', 'kubernetes.io/arch': 'amd64'})
        self.assertEqual(spec['containers'][0]['resources'], {
            'requests': {'cpu': '1250m', 'memory': '3Gi', 'ephemeral-storage': '1Gi'},
            'limits': {'cpu': '2', 'memory': '6Gi', 'ephemeral-storage': '20Gi'}})
        for field in ['runtimeClassName', 'nodeSelector']:
            spec[field] = before['template']['spec'][field]
        runner, original = spec['containers'][0], before['template']['spec']['containers'][0]
        for field in ['resources', 'image']:
            runner[field] = original[field]
        for env in runner['env']:
            if env['name'] == 'RUNNER_RESERVE_NODE_IP':
                env['value'] = '192.0.2.1'
        self.assertEqual(before, after)

    def test_subset_inventory_is_supported_but_wrong_scope_or_profile_is_not(self):
        self.assertEqual(len(render([{**ROW, 'profiles': ['ci']}])['index.json']), 1)
        for row in [{**ROW, 'profiles': ['automation'], 'ci_storage': 'disk'},
                    {**ROW, 'ci_storage': 'disk'},
                    {**ROW, 'ci_storage': 'tmpfs-6gi-heavy', 'ci_resource_class': 'heavy'},
                    {**ROW, 'visibility': 'public'}, {**ROW, 'name': 'other/repo'}]:
            with self.subTest(row=row), self.assertRaises(ValueError):
                render([row])
        for config in [dict(CONFIG._asdict()), arc.NucKvmConfig(ROW['name'], 'image:latest', '192.0.2.2'),
                       arc.NucKvmConfig('owner/repo;bad', NUC, '192.0.2.2'),
                       arc.NucKvmConfig(ROW['name'], NUC, None)]:
            with self.subTest(config=config), self.assertRaises(ValueError):
                render(config=config)

    def test_guard_address_must_not_be_an_ambiguous_endpoint_or_old_node(self):
        for ip in ['192.0.2.1', '127.0.0.1', '0.0.0.0', '169.254.169.254', '224.0.0.1',
                   '::1', 'host.invalid', '192.0.2.2/32', '192.0.2.2:19999']:
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                render(config=arc.NucKvmConfig(ROW['name'], NUC, ip))
        with self.assertRaises(ValueError):
            arc.render({'repositories': [ROW]}, MP, NATIVE, '192.0.2.1',
                       sandbox_runtime=arc.EXCLUSIVE_RUNTIME, nuc_kvm=CONFIG)

    def test_partial_cli_cannot_silently_fall_back_or_write_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / 'inventory.json'
            inventory.write_text(json.dumps({'repositories': [ROW]}))
            for args in [['--nuc-kvm-scope', ROW['name']], ['--nuc-kvm-image', NUC],
                         ['--nuc-kvm-node-ip', CONFIG.node_ip]]:
                output = root / 'output'
                result = subprocess.run([sys.executable, str(ROOT / 'deploy/arc-private/render.py'),
                                         '--inventory', str(inventory), '--sandbox-image', MP,
                                         '--native-image', NATIVE, '--node-ip', '192.0.2.1',
                                         '--output', str(output), *args], capture_output=True, text=True,
                                        timeout=10, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('required together', result.stderr)
                self.assertFalse(output.exists())

    def test_global_admission_retains_security_checks_without_namespace_escape(self):
        base = render(config=None)['foundations.json']['items']
        items = render()['foundations.json']['items']
        old = next(x for x in base if x['kind'] == 'ValidatingAdmissionPolicy')
        shared = next(x for x in items if x['kind'] == 'ValidatingAdmissionPolicy'
                      and x['metadata']['name'] == arc.PRIVATE_ADMISSION)
        guard = next(x for x in items if x['metadata']['name'] == arc.NUC_RUNTIME + '-workers'
                     and x['kind'] == 'ValidatingAdmissionPolicy')
        self.assertEqual(shared['spec']['validations'][:-1], old['spec']['validations'])
        self.assertIn("request.namespace != 'arc-private-ci'", shared['spec']['validations'][-1]['expression'])
        self.assertNotIn('namespaceSelector', guard['spec']['matchConstraints'])
        self.assertEqual(guard['spec']['failurePolicy'], 'Fail')
        self.assertEqual(guard['spec']['matchConstraints']['resourceRules'],
                         old['spec']['matchConstraints']['resourceRules'])
        rules = {v['message']: v['expression'] for v in guard['spec']['validations']}
        changed = {'gVisor is mandatory', 'Only the reviewed pinned worker image is allowed',
                   'The tmpfs candidate requires its exact private profile and resource tuple'}
        for rule in old['spec']['validations']:
            if rule['message'] not in changed:
                self.assertEqual(rules[rule['message']], rule['expression'])
        for text in ["request.namespace == 'arc-private-ci'", arc.name(ROW['name'], 'scope'),
                     "== 'ci'"]:
            self.assertIn(text, rules['NUC KVM is restricted to the one selected private CI scope'])
        self.assertIn(json.dumps(NUC), rules['Only the reviewed pinned worker image is allowed'])
        self.assertIn('1250m', rules['NUC workers require the exact bounded CI resource tuple'])
        self.assertIn('dyn(c.resources).requests ==', rules['NUC workers require the exact bounded CI resource tuple'])
        self.assertIn('dyn(c.resources).limits ==', rules['NUC workers require the exact bounded CI resource tuple'])
        self.assertIn("object.spec.nodeName == 'nuc'", rules['NUC KVM cannot select another node or scheduler'])
        self.assertIn("object.spec.restartPolicy == 'Never'", rules['NUC workers keep the bounded single-job lifecycle'])
        for policy in [guard, next(x for x in items if x['kind'] == 'ValidatingAdmissionPolicy'
                                  and x['metadata']['name'] == arc.NUC_RUNTIME + '-class')]:
            binding = next(x for x in items if x['kind'] == 'ValidatingAdmissionPolicyBinding'
                           and x['metadata']['name'] == policy['metadata']['name'])
            self.assertEqual(binding['spec'], {'policyName': policy['metadata']['name'],
                                              'validationActions': ['Deny']})

    def test_bootstrap_environment_is_an_exact_whitelist_with_no_proxy_or_identity_escape(self):
        documents = render()
        expected = documents[arc.name(ROW['name'], 'ci') + '.json']['template']['spec']['containers'][0]['env']
        expression = arc.nuc_environment_validation(expected)
        names = [e['name'] for e in expected] + ['ACTIONS_RUNNER_INPUT_JITCONFIG']
        self.assertIn('c.env.all(e, e.name in ' + json.dumps(names) + ')', expression)
        for env in expected:
            self.assertIn('size(c.env.filter(e, e.name == ' + json.dumps(env['name']) + ')) == 1', expression)
        self.assertIn('e.value == "192.0.2.2"', expression)
        self.assertIn('e.valueFrom.fieldRef.fieldPath == "metadata.uid"', expression)
        self.assertIn('e.valueFrom.fieldRef.fieldPath == "metadata.namespace"', expression)
        self.assertIn("e.valueFrom.fieldRef.apiVersion == 'v1'", expression)
        self.assertIn("e.valueFrom.resourceFieldRef.containerName == 'runner'", expression)
        self.assertIn("dyn(e.valueFrom.resourceFieldRef).divisor == '1'", expression)
        for forbidden in ['LD_PRELOAD', 'HTTPS_PROXY', 'RUNNER_RESERVE_QUALIFICATION']:
            self.assertNotIn(forbidden, names)

    def test_network_policies_cannot_add_old_guard_access_to_nuc_scope(self):
        items = render()['foundations.json']['items']
        policies = [x for x in items if x['kind'] == 'NetworkPolicy'
                    and x['metadata']['namespace'] == 'arc-private-ci']
        self.assertEqual(len(policies), 2)
        old = next(x for x in policies if x['metadata']['name'] == 'reserve-worker-isolation')
        new = next(x for x in policies if x != old)
        scope = arc.name(ROW['name'], 'scope')
        self.assertEqual(old['spec']['podSelector'], {'matchExpressions': [
            {'key': arc.SCOPE_LABEL, 'operator': 'NotIn', 'values': [scope]}]})
        self.assertEqual(new['spec']['podSelector'], {'matchLabels': {arc.SCOPE_LABEL: scope}})
        old_egress = copy.deepcopy(old['spec']['egress'])
        for rule in old_egress:
            if rule['ports'][0]['port'] == 19999:
                rule['to'] = [{'ipBlock': {'cidr': CONFIG.node_ip + '/32'}}]
        self.assertEqual(new['spec']['egress'], old_egress)
        self.assertEqual(new['spec']['ingress'], [])

    def test_runtime_alias_guard_and_fixed_capacity(self):
        items = render()['foundations.json']['items']
        runtime = next(x for x in items if x['kind'] == 'RuntimeClass')
        self.assertEqual(runtime['metadata']['name'], runtime['handler'])
        self.assertEqual(runtime['handler'], arc.NUC_RUNTIME)
        self.assertEqual(runtime['scheduling']['nodeSelector'],
                         {'kubernetes.io/hostname': 'nuc', 'kubernetes.io/arch': 'amd64'})
        self.assertEqual(runtime['overhead']['podFixed'], {'cpu': '100m', 'memory': '128Mi'})
        alias = next(x for x in items if x['kind'] == 'ValidatingAdmissionPolicy'
                     and x['metadata']['name'] == arc.NUC_RUNTIME + '-class')
        self.assertIn(' || object.handler == ', alias['spec']['matchConditions'][0]['expression'])
        self.assertIn(' && object.handler == ', alias['spec']['validations'][0]['expression'])
        self.assertLess(items.index(alias), items.index(runtime))
        quota = arc.sandbox_quota('ci')
        self.assertEqual(quota['requests.cpu'], '2000m')
        self.assertLessEqual(1250 + 100, 2000)
        self.assertGreater(2 * (1250 + 100), 2000)

    def test_host_assets_select_complete_versioned_bundle_and_reuse_guard(self):
        directory = ROOT / 'deploy/arc-private'
        config = tomllib.loads((directory / 'runsc-nuc-kvm.toml').read_text())
        old = tomllib.loads((ROOT / 'deploy/runner-reserve/runsc.toml').read_text())
        self.assertEqual(config['binary_name'], '/opt/victron-ci-reserve/gvisor-20261012/runsc')
        old['runsc_config']['platform'] = 'kvm'
        self.assertEqual(config['runsc_config'], old['runsc_config'])
        fragment = tomllib.loads((directory / 'containerd-nuc-kvm.toml.fragment').read_text())
        runtimes = fragment['plugins']['io.containerd.grpc.v1.cri']['containerd']['runtimes']
        self.assertEqual(set(runtimes), {arc.NUC_RUNTIME})
        runtime = runtimes[arc.NUC_RUNTIME]
        self.assertEqual(runtime['runtime_type'], 'io.containerd.runsc.v1')
        self.assertEqual(runtime['runtime_path'],
                         '/opt/victron-ci-reserve/gvisor-20261012/containerd-shim-runsc-v1')
        self.assertEqual(runtime['options'], {'TypeUrl': 'io.containerd.runsc.v1.options',
                                             'ConfigPath': '/etc/victron-ci-reserve/runsc-nuc-kvm.toml'})
        source = (ROOT / 'deploy/runner-reserve/node-guard.service').read_text()
        candidate = (directory / 'nuc-node-guard.service').read_text()
        self.assertEqual(candidate, source.replace('Description=Network admission for isolated CI reserve workers',
                                                   'Description=Network admission for private NUC KVM CI workers'))

    def test_source_inventory_packages_candidate_assets(self):
        inventory = json.loads((ROOT / 'source-files.json').read_text())
        for path in ['NUC_KVM_CANDIDATE.md', 'runsc-nuc-kvm.toml',
                     'containerd-nuc-kvm.toml.fragment', 'nuc-node-guard.service']:
            self.assertIn('deploy/arc-private/' + path, inventory)


if __name__ == '__main__':
    unittest.main()
