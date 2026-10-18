"""Offline NUC opt-in contracts; actual Kubernetes CEL/runtime proof stays separate."""
import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
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


def identity_expression(expression, context):
    """Exercise the emitted boolean/map identity subset; not a CEL typechecker."""
    source = expression.replace('&&', ' and ').replace('||', ' or ')
    source = re.sub(r'!(?!=)', ' not ', source)
    source = re.sub(r'\bnull\b', 'None', source)

    def visit(node, values):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.Attribute):
            return visit(node.value, values)[node.attr]
        if isinstance(node, ast.Subscript):
            return visit(node.value, values)[visit(node.slice, values)]
        if isinstance(node, ast.Dict):
            return {visit(k, values): visit(v, values) for k, v in zip(node.keys, node.values)}
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not visit(node.operand, values)
        if isinstance(node, ast.BoolOp):
            results = (bool(visit(child, values)) for child in node.values)
            return all(results) if isinstance(node.op, ast.And) else any(results)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = visit(node.left, values), visit(node.comparators[0], values)
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
            if isinstance(node.ops[0], ast.In):
                return left in right
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == 'dyn' and len(node.args) == 1:
                return visit(node.args[0], values)
            if isinstance(node.func, ast.Name) and node.func.id == 'has' and len(node.args) == 1:
                try:
                    visit(node.args[0], values)
                except (KeyError, TypeError):
                    return False
                return True
            if isinstance(node.func, ast.Attribute):
                receiver = visit(node.func.value, values)
                if node.func.attr == 'all' and isinstance(node.args[0], ast.Name):
                    return all(visit(node.args[1], {**values, node.args[0].id: item}) for item in receiver)
                if node.func.attr == 'startsWith':
                    return receiver.startswith(visit(node.args[0], values))
        raise AssertionError('Unsupported identity expression syntax: ' + ast.dump(node))

    return bool(visit(ast.parse(source.strip(), mode='eval').body, context))


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
        self.assertEqual(guard['spec']['matchConstraints']['resourceRules'][:-1],
                         old['spec']['matchConstraints']['resourceRules'])
        self.assertEqual(guard['spec']['matchConstraints']['resourceRules'][-1], {
            'apiGroups': [''], 'apiVersions': ['v1'], 'operations': ['UPDATE'], 'resources': ['pods/status']})
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

    def identity_policy(self):
        documents = render()
        pod = copy.deepcopy(documents[arc.name(ROW['name'], 'ci') + '.json']['template'])
        pod['metadata']['namespace'] = 'arc-private-ci'
        pod['spec']['nodeName'] = 'nuc'
        guard = next(x for x in documents['foundations.json']['items']
                     if x['kind'] == 'ValidatingAdmissionPolicy'
                     and x['metadata']['name'] == arc.NUC_RUNTIME + '-workers')
        rules = {v['message']: v['expression'] for v in guard['spec']['validations']}
        return pod, guard['spec']['matchConditions'][0]['expression'], rules

    def test_status_update_preserves_identity_but_allows_status_and_arc_metadata(self):
        old, match, rules = self.identity_policy()
        new = copy.deepcopy(old)
        new['status'] = {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}
        new['metadata']['labels']['actions.github.com/job-result'] = 'running'
        context = {'object': new, 'oldObject': old,
                   'request': {'operation': 'UPDATE', 'namespace': 'arc-private-ci'}}
        self.assertTrue(identity_expression(match, context))
        for message in ['NUC updates preserve scope, profile, worker identity and the bound node',
                        'NUC KVM is restricted to the one selected private CI scope',
                        'NUC KVM cannot select another node or scheduler', 'gVisor is mandatory',
                        'Worker network identity is mandatory',
                        'Worker annotations cannot override sandbox filesystem policy']:
            self.assertTrue(identity_expression(rules[message], context), message)
        # CREATE has no oldObject; a newly scheduled node does not require an old binding.
        self.assertTrue(identity_expression(rules['NUC updates preserve scope, profile, worker identity and the bound node'],
                                            {**context, 'oldObject': None, 'request': {'operation': 'CREATE'}}))

    def test_status_and_regular_updates_cannot_drop_or_replace_network_labels(self):
        old, match, rules = self.identity_policy()
        immutable = rules['NUC updates preserve scope, profile, worker identity and the bound node']
        for subresource in ['', 'status', 'ephemeralcontainers']:
            for key in [arc.SCOPE_LABEL, arc.PROFILE_LABEL, 'runner-reserve.github.io/role']:
                for replacement in [None, 'other']:
                    with self.subTest(subresource=subresource, label=key, replacement=replacement):
                        new = copy.deepcopy(old)
                        if replacement is None:
                            del new['metadata']['labels'][key]
                        else:
                            new['metadata']['labels'][key] = replacement
                        context = {'object': new, 'oldObject': old, 'request': {
                            'operation': 'UPDATE', 'namespace': 'arc-private-ci', 'subResource': subresource}}
                        self.assertTrue(identity_expression(match, context))
                        self.assertFalse(identity_expression(immutable, context))
        for replacement in [None, 'mp']:
            new = copy.deepcopy(old)
            if replacement is None:
                del new['spec']['nodeName']
            else:
                new['spec']['nodeName'] = replacement
            self.assertFalse(identity_expression(immutable, {'object': new, 'oldObject': old,
                                                            'request': {'operation': 'UPDATE'}}))
        new = copy.deepcopy(old)
        new['metadata']['annotations'] = {'dev.gvisor.flag.file-access-mounts': 'exclusive'}
        self.assertFalse(identity_expression(rules['Worker annotations cannot override sandbox filesystem policy'],
                                             {'object': new}))

    def test_match_cannot_escape_through_runtime_or_selected_scope_transitions(self):
        old, match, rules = self.identity_policy()
        for runtime in [None, arc.SHARED_RUNTIME]:
            new = copy.deepcopy(old)
            new['metadata']['labels'] = {}
            if runtime is None:
                del new['spec']['runtimeClassName']
            else:
                new['spec']['runtimeClassName'] = runtime
            context = {'object': new, 'oldObject': old,
                       'request': {'operation': 'UPDATE', 'namespace': 'arc-private-ci'}}
            self.assertTrue(identity_expression(match, context))
            self.assertFalse(identity_expression(rules['gVisor is mandatory'], context))
        # A non-NUC Pod must not acquire (or shed) the selected NP label via status.
        foreign = copy.deepcopy(old)
        foreign['spec']['runtimeClassName'] = arc.SHARED_RUNTIME
        foreign['metadata']['labels'][arc.SCOPE_LABEL] = 'another-scope'
        selected = copy.deepcopy(foreign)
        selected['metadata']['labels'][arc.SCOPE_LABEL] = arc.name(ROW['name'], 'scope')
        for before, after in [(foreign, selected), (selected, foreign)]:
            context = {'object': after, 'oldObject': before,
                       'request': {'operation': 'UPDATE', 'namespace': 'arc-private-ci'}}
            self.assertTrue(identity_expression(match, context))
            self.assertFalse(identity_expression(rules['gVisor is mandatory'], context))
        self.assertFalse(identity_expression(match, {'object': foreign, 'oldObject': foreign,
                                                     'request': {'namespace': 'arc-private-ci'}}))

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

    def test_pod_overhead_uses_dynamic_parent_and_exact_runtime_bounds(self):
        _, _, rules = self.identity_policy()
        expression = rules['NUC worker overhead must match its RuntimeClass']
        # Kubernetes 1.31's Pod schema omits overhead: cast before field access,
        # including has(), rather than casting an already statically read field.
        self.assertIn('has(dyn(object.spec).overhead)', expression)
        self.assertNotIn('object.spec.overhead', expression)
        self.assertTrue(identity_expression(expression, {
            'object': {'spec': {'overhead': {'cpu': '100m', 'memory': '128Mi'}}}}))
        for spec in [{}, {'overhead': None}, {'overhead': {}},
                     {'overhead': {'cpu': '100m', 'memory': '256Mi'}},
                     {'overhead': {'cpu': '200m', 'memory': '128Mi'}},
                     {'overhead': {'cpu': '100m', 'memory': '128Mi', 'pods': '1'}},
                     {'overhead': {'cpu': 100, 'memory': '128Mi'}},
                     {'overhead': '100m/128Mi'}]:
            with self.subTest(spec=spec):
                self.assertFalse(identity_expression(expression, {'object': {'spec': spec}}))

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

    def test_class_overhead_uses_dynamic_parent_and_rejects_missing_or_wrong_bounds(self):
        items = render()['foundations.json']['items']
        runtime = next(x for x in items if x['kind'] == 'RuntimeClass')
        guard = next(x for x in items if x['kind'] == 'ValidatingAdmissionPolicy'
                     and x['metadata']['name'] == arc.NUC_RUNTIME + '-class')
        expression = guard['spec']['validations'][0]['expression']
        self.assertIn('has(dyn(object.overhead).podFixed)', expression)
        self.assertNotIn('object.overhead.podFixed', expression)
        self.assertTrue(identity_expression(expression, {'object': runtime}))
        for overhead in [None, {}, {'podFixed': None}, {'podFixed': {}},
                         {'podFixed': {'cpu': '200m', 'memory': '128Mi'}},
                         {'podFixed': {'cpu': '100m', 'memory': '256Mi'}},
                         {'podFixed': {'cpu': '100m', 'memory': '128Mi', 'pods': '1'}}]:
            with self.subTest(overhead=overhead):
                bad = copy.deepcopy(runtime)
                if overhead is None:
                    del bad['overhead']
                else:
                    bad['overhead'] = overhead
                self.assertFalse(identity_expression(expression, {'object': bad}))

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
