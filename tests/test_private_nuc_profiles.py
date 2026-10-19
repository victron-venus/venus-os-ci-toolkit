"""Offline multi-profile output contracts, not a Kubernetes CEL evaluator."""
import copy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('private_nuc_profiles', ROOT / 'deploy/arc-private/render.py')
arc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(arc)
HELPER_SPEC = importlib.util.spec_from_file_location('nuc_identity_test_helpers', ROOT / 'tests/test_private_nuc_kvm.py')
helpers = importlib.util.module_from_spec(HELPER_SPEC)
HELPER_SPEC.loader.exec_module(helpers)
identity_expression = helpers.identity_expression
MP_IMAGE = 'localhost/shared@sha256:' + 'a' * 64
NATIVE_IMAGE = 'registry.example/native@sha256:' + 'b' * 64
NUC_IMAGE = 'localhost/candidate@sha256:' + 'c' * 64
MP_IP, NUC_IP = '192.0.2.1', '192.0.2.2'
SERIAL_LABEL = 'private-runners.github.io/serial-domain'
ROWS = [
    {'name': 'example/standard', 'visibility': 'private',
     'profiles': ['ci', 'automation', 'release'], 'ci_storage': 'tmpfs-2gi'},
    {'name': 'example/heavy', 'visibility': 'private',
     'profiles': ['ci', 'automation'], 'ci_storage': 'tmpfs-6gi-heavy',
     'ci_resource_class': 'heavy', 'automation_storage': 'tmpfs-3gi'},
    {'name': 'example/unselected', 'visibility': 'private',
     'profiles': ['ci', 'automation', 'lan', 'arm64'],
     'lan_egress': [{'cidr': '192.0.2.20/32', 'port': 9443}]},
]
TARGETS = (('example/standard', 'ci'), ('example/heavy', 'ci'), ('example/heavy', 'automation'))
RESOURCE_TUPLES = {
    TARGETS[0]: {'requests': {'cpu': '600m', 'memory': '3Gi', 'ephemeral-storage': '1Gi'},
                 'limits': {'cpu': '2', 'memory': '6Gi', 'ephemeral-storage': '20Gi'}},
    TARGETS[1]: {'requests': {'cpu': '600m', 'memory': '13Gi', 'ephemeral-storage': '2Gi'},
                 'limits': {'cpu': '2', 'memory': '16Gi', 'ephemeral-storage': '30Gi'}},
    TARGETS[2]: {'requests': {'cpu': '50m', 'memory': '5Gi', 'ephemeral-storage': '1Gi'},
                 'limits': {'cpu': '1', 'memory': '6Gi', 'ephemeral-storage': '20Gi'}},
}
AFFINITY = {'podAntiAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': [{
    'labelSelector': {'matchLabels': {SERIAL_LABEL: 'nuc-kvm'}},
    'namespaceSelector': {}, 'topologyKey': 'kubernetes.io/hostname',
}]}}


def plan(targets=TARGETS, image=NUC_IMAGE, node_ip=NUC_IP):
    return arc.NucKvmPlan(tuple(arc.NucKvmTarget(*target) for target in targets), image, node_ip)


def render(config, rows=None):
    return arc.render({'repositories': copy.deepcopy(ROWS if rows is None else rows)},
                      MP_IMAGE, NATIVE_IMAGE, MP_IP, listener_node='control-node', nuc_kvm=config)


def worker(documents, target):
    return documents[arc.name(*target) + '.json']


def policy(documents, suffix):
    return next(item for item in documents['foundations.json']['items']
                if item['kind'] == 'ValidatingAdmissionPolicy'
                and item['metadata']['name'] == arc.NUC_RUNTIME + suffix)


def quantity(value):
    """Convert the renderer's fixed units for quota arithmetic, not arbitrary K8s input."""
    for unit, scale in (('Gi', 1024 ** 3), ('Mi', 1024 ** 2), ('m', Decimal('0.001'))):
        if value.endswith(unit):
            return Decimal(value[:-len(unit)]) * scale
    return Decimal(value)


def quota_charge(documents, target):
    resources = worker(documents, target)['template']['spec']['containers'][0]['resources']
    runtime = next(item for item in documents['foundations.json']['items']
                   if item['kind'] == 'RuntimeClass' and item['metadata']['name'] == arc.NUC_RUNTIME)
    overhead = runtime['overhead']['podFixed']
    return {'pods': Decimal(1), **{
        category + '.' + resource: quantity(amount) + quantity(overhead.get(resource, '0'))
        for category, values in resources.items() for resource, amount in values.items()}}


def namespace_budget(documents, profile):
    return next(item['spec']['hard'] for item in documents['foundations.json']['items']
                if item['kind'] == 'ResourceQuota'
                and item['metadata']['namespace'] == 'arc-private-' + profile)


def split_top_level(expression, operator):
    """Inspect emitted grouping only; do not claim to execute CEL semantics."""
    brackets, quote, escaped, start, parts = [], None, False, 0, []
    index = 0
    while index < len(expression):
        char = expression[index]
        if quote:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in '([{':
            brackets.append(char)
        elif char in ')]}':
            if not brackets or brackets.pop() != {')': '(', ']': '[', '}': '{'}[char]:
                raise AssertionError('Mismatched emitted policy grouping')
        elif not brackets and expression.startswith(operator, index):
            parts.append(expression[start:index].strip())
            index += len(operator)
            start = index
            continue
        index += 1
    if brackets or quote:
        raise AssertionError('Unbalanced emitted policy expression')
    parts.append(expression[start:].strip())
    return parts


def selected_scopes(selector):
    if 'matchLabels' in selector:
        return {selector['matchLabels'][arc.SCOPE_LABEL]}
    clauses = selector['matchExpressions']
    if len(clauses) != 1 or clauses[0]['key'] != arc.SCOPE_LABEL or clauses[0]['operator'] != 'In':
        raise AssertionError('Unexpected selected-scope selector')
    return set(clauses[0]['values'])


class NucProfileOutputTests(unittest.TestCase):
    def test_repository_listener_override_only_changes_its_listener_placement(self):
        rows = copy.deepcopy(ROWS)
        rows[0]['listener_node'] = 'nuc'
        rows[2]['listener_node'] = 'h7'
        original = render(plan())
        candidate = render(plan(), rows)
        for row in rows:
            expected = row.get('listener_node', 'control-node')
            for profile in row['profiles']:
                value = worker(candidate, (row['name'], profile))
                selector = value['listenerTemplate']['spec']['nodeSelector']
                self.assertEqual(selector, {arc.HOSTNAME_LABEL: expected})
                # Includes release, ARM and LAN: only the listener moves.
                selector[arc.HOSTNAME_LABEL] = 'control-node'
        self.assertEqual(candidate, original)

    def test_explicit_listener_default_equals_omitted_override(self):
        rows = copy.deepcopy(ROWS)
        for row in rows:
            row['listener_node'] = 'control-node'
        self.assertEqual(render(plan(), rows), render(plan()))

    def test_invalid_listener_override_never_falls_back_to_global_node(self):
        for value in (None, '', 123, True, [], {}, '-nuc', 'nuc-', 'NUC',
                      'nuc/worker', 'nuc worker', 'a' * 64):
            with self.subTest(value=value):
                rows = copy.deepcopy(ROWS)
                rows[0]['listener_node'] = value
                with self.assertRaisesRegex(ValueError, 'Listener node'):
                    render(plan(), rows)
                with self.assertRaisesRegex(ValueError, 'Listener node'):
                    arc.values(rows[0], 'ci', MP_IMAGE, NATIVE_IMAGE, MP_IP,
                               listener_node='control-node')

    def test_fleet_size_compacts_only_identical_contracts_and_preserves_outputs(self):
        rows = [dict(name=f'example/project-{i}', visibility='private',
                     profiles=['ci', 'automation'], automation_storage='tmpfs-3gi',
                     ci_storage='tmpfs-6gi-heavy' if i < 3 else 'tmpfs-2gi',
                     ci_resource_class='heavy' if i < 3 else 'standard') for i in range(20)]
        rows[0]['profiles'].append('release')
        rows[1]['profiles'].append('arm64')
        rows[2]['profiles'].append('lan')
        rows[2]['lan_egress'] = [{'cidr': '192.0.2.20/32', 'port': 9443}]
        targets = tuple((row['name'], profile) for row in rows for profile in ('ci', 'automation'))
        candidate = render(plan(targets), rows)

        def expanded(values):
            return ' || '.join('(' + arc.nuc_target_validation(target, values[target]) + ')'
                               for target in sorted(values))

        with mock.patch.object(arc, 'nuc_targets_validation', expanded):
            previous = render(plan(targets), rows)
        self.assertEqual(len(candidate['index.json']), 43)
        old_guard = policy(previous, '-workers')
        new_guard = policy(candidate, '-workers')
        changed = [(old, new) for old, new in zip(old_guard['spec']['validations'],
                                                new_guard['spec']['validations']) if old != new]
        self.assertEqual(len(changed), 1)
        old, new = changed[0]
        self.assertGreater(len(old['expression']), 100_000)
        self.assertLess(len(new['expression']), 20_000)
        branches = split_top_level(new['expression'], '||')
        self.assertEqual(len(branches), 3)
        for scope, profile in targets:
            matching = [branch for branch in branches
                        if json.dumps(arc.name(scope, 'scope')) in branch
                        and 'request.namespace == ' + json.dumps('arc-private-' + profile) in branch]
            self.assertEqual(len(matching), 1, (scope, profile))
        # Normalizing only the changed rule restores the entire prior render:
        # pools, security rules, network selection and unselected native routes.
        new['expression'] = old['expression']
        self.assertEqual(candidate, previous)

    def test_grouping_separates_resource_and_environment_differences(self):
        documents = render(plan())
        first = arc.NucKvmTarget(*TARGETS[0])
        second = arc.NucKvmTarget('example/other', first.profile)
        value = worker(documents, first)
        cases = (('requests', 'cpu'), ('limits', 'memory'), ('env', 'value'))
        for section, field in cases:
            changed = copy.deepcopy(value)
            runner = changed['template']['spec']['containers'][0]
            if section == 'env':
                runner['env'][0]['value'] = 'different-bootstrap-value'
            else:
                runner['resources'][section][field] = 'different-resource'
            with self.subTest(section=section):
                expression = arc.nuc_targets_validation({first: value, second: changed})
                self.assertEqual(len(split_top_level(expression, '||')), 2)
        identical = {first: value, second: copy.deepcopy(value)}
        expression = arc.nuc_targets_validation(identical)
        self.assertEqual(len(split_top_level(expression, '||')), 1)
        self.assertEqual(expression, arc.nuc_targets_validation(dict(reversed(list(identical.items())))))

    def test_selected_profiles_fit_quota_only_after_the_reviewed_automation_delta(self):
        documents = render(plan())
        for target in TARGETS:
            with self.subTest(target=target):
                budget = namespace_budget(documents, target[1])
                for resource, charge in quota_charge(documents, target).items():
                    self.assertLessEqual(charge, quantity(budget[resource]), resource)
        previous_rows = copy.deepcopy(ROWS)
        previous_rows[1].pop('automation_storage')
        previous = namespace_budget(render(None, previous_rows), 'automation')
        proposed = namespace_budget(documents, 'automation')
        self.assertEqual(previous['requests.memory'], '3Gi')
        self.assertEqual(proposed['requests.memory'], '6Gi')
        self.assertEqual({key for key in previous if previous[key] != proposed[key]}, {'requests.memory'})
        charge = quota_charge(documents, TARGETS[2])
        self.assertEqual(charge['requests.memory'], quantity('5248Mi'))
        self.assertEqual(charge['limits.memory'], quantity('6272Mi'))
        self.assertGreater(charge['requests.memory'], quantity(previous['requests.memory']))

    def test_cpu_headroom_does_not_replace_cross_namespace_serialization(self):
        documents = render(plan())
        standard, heavy, automation = [quota_charge(documents, target) for target in TARGETS]
        ci = namespace_budget(documents, 'ci')
        auto = namespace_budget(documents, 'automation')
        self.assertEqual(standard['requests.cpu'] + heavy['requests.cpu'], Decimal('1.4'))
        self.assertLessEqual(2 * standard['requests.cpu'], quantity(ci['requests.cpu']))
        for target in TARGETS:
            self.assertEqual(worker(documents, target)['template']['spec']['affinity'], AFFINITY)
        # Leave capacity for persistent services on a small shared node.
        self.assertLessEqual(quantity('2750m') + standard['requests.cpu'], quantity('3500m'))
        self.assertGreater(2 * automation['requests.memory'], quantity(auto['requests.memory']))
        # Cross-namespace quota is independent: admission can pass for both.
        # Their actual Pending reason still requires live scheduler evidence.
        for charge, budget in ((standard, ci), (automation, auto)):
            for resource, amount in charge.items():
                self.assertLessEqual(amount, quantity(budget[resource]))

    def test_complete_plan_changes_only_three_workers_and_foundations(self):
        original, candidate = render(None), render(plan())
        self.assertEqual(set(original), set(candidate))
        changed = {key for key in original if original[key] != candidate[key]}
        self.assertEqual(changed, {'foundations.json'} | {arc.name(*target) + '.json' for target in TARGETS})
        self.assertEqual(original['repository-variables.json'], candidate['repository-variables.json'])
        self.assertEqual(original['index.json'], candidate['index.json'])
        for target in TARGETS:
            with self.subTest(target=target):
                before, after = worker(original, target), worker(candidate, target)
                for key in ('githubConfigUrl', 'githubConfigSecret', 'runnerScaleSetName',
                            'listenerTemplate', 'minRunners', 'maxRunners'):
                    self.assertEqual(before[key], after[key])
                self.assertEqual((after['minRunners'], after['maxRunners']), (0, 1))
                self.assertNotIn(SERIAL_LABEL, json.dumps(after['listenerTemplate']))

    def test_each_profile_has_its_own_exact_resource_storage_and_lifecycle_contract(self):
        candidate, original = render(plan()), render(None)
        modes = dict(zip(TARGETS, ('ci-tmpfs-2gi', 'ci-heavy-tmpfs-6gi', 'automation-tmpfs-3gi')))
        for target in TARGETS:
            with self.subTest(target=target):
                pod = worker(candidate, target)['template']['spec']
                baseline = worker(original, target)['template']['spec']
                self.assertEqual(pod['runtimeClassName'], arc.NUC_RUNTIME)
                self.assertEqual(pod['nodeSelector'], {'kubernetes.io/hostname': 'nuc', 'kubernetes.io/arch': 'amd64'})
                self.assertEqual(len(pod['containers']), 1)
                runner = pod['containers'][0]
                self.assertEqual(runner['resources'], RESOURCE_TUPLES[target])
                self.assertEqual(runner['image'], NUC_IMAGE)
                self.assertEqual(runner['imagePullPolicy'], 'Never')
                self.assertEqual(runner['command'], baseline['containers'][0]['command'])
                self.assertEqual(pod['activeDeadlineSeconds'], baseline['activeDeadlineSeconds'])
                self.assertEqual(pod['restartPolicy'], 'Never')
                self.assertFalse(pod['automountServiceAccountToken'])
                self.assertEqual(pod['volumes'], [{'name': 'docker-data', 'emptyDir': {'sizeLimit': '16Gi'}}])
                self.assertEqual(runner['volumeMounts'], baseline['containers'][0]['volumeMounts'])
                env = {entry['name']: entry for entry in runner['env']}
                self.assertEqual(len(env), len(runner['env']))
                self.assertEqual(env['RUNNER_RESERVE_NODE_IP']['value'], NUC_IP)
                self.assertEqual(env['RUNNER_PRIVATE_DOCKER_STORAGE']['value'], modes[target])
                self.assertEqual(env['RUNNER_PRIVATE_PROFILE']['valueFrom']['fieldRef'], {
                    'fieldPath': "metadata.labels['private-runners.github.io/profile']"})
                self.assertEqual(worker(candidate, target)['template']['metadata']['labels'][arc.PROFILE_LABEL], target[1])
                for kind in ('requests', 'limits'):
                    name = 'RUNNER_PRIVATE_MEMORY_' + ('REQUEST' if kind == 'requests' else 'LIMIT')
                    self.assertEqual(env[name]['valueFrom']['resourceFieldRef'], {
                        'containerName': 'runner', 'resource': kind + '.memory', 'divisor': '1'})

    def test_all_workers_share_required_cross_namespace_serialization(self):
        documents = render(plan())
        for target in TARGETS:
            with self.subTest(target=target):
                template = worker(documents, target)['template']
                self.assertEqual(template['metadata']['labels'][SERIAL_LABEL], 'nuc-kvm')
                self.assertEqual(template['spec']['schedulerName'], 'default-scheduler')
                self.assertEqual(template['spec']['affinity'], AFFINITY)
                self.assertNotIn('nodeName', template['spec'])
        rules = '\n'.join(rule['expression'] for rule in policy(documents, '-workers')['spec']['validations'])
        for text in (SERIAL_LABEL, 'nuc-kvm', 'oldObject', 'namespaceSelector',
                     'requiredDuringSchedulingIgnoredDuringExecution', 'default-scheduler', 'request.operation'):
            self.assertIn(text, rules)
        self.assertIn('oldObject.spec.nodeName', rules)

    def test_target_policy_branches_keep_identity_resources_and_environment_together(self):
        documents = render(plan())
        guard = policy(documents, '-workers')
        expression = next(rule['expression'] for rule in guard['spec']['validations']
                          if rule['message'] == 'NUC workers require one exact scope, namespace, profile, resource and environment tuple')
        branches = split_top_level(expression, '||')
        self.assertEqual(len(branches), len(TARGETS), expression)
        for target in TARGETS:
            scope, profile = target
            namespace = 'arc-private-' + profile
            resources = RESOURCE_TUPLES[target]
            matching = [branch for branch in branches
                        if json.dumps(arc.name(scope, 'scope')) in branch
                        and re.search(r"request\.namespace\s*==\s*['\"]" + namespace + r"['\"]", branch)]
            self.assertEqual(len(matching), 1, target)
            branch = matching[0]
            self.assertRegex(branch, r"object\.metadata\.namespace\s*==\s*['\"]" + namespace + r"['\"]")
            self.assertIn(arc.PROFILE_LABEL, branch)
            self.assertRegex(branch, r"==\s*['\"]" + profile + r"['\"]")
            self.assertIn('dyn(c.resources).requests == ' + json.dumps(resources['requests']), branch)
            self.assertIn('dyn(c.resources).limits == ' + json.dumps(resources['limits']), branch)
            env = worker(documents, target)['template']['spec']['containers'][0]['env']
            self.assertIn(arc.nuc_environment_validation(env), branch)
            for other in TARGETS:
                if other != target:
                    self.assertNotIn('dyn(c.resources).requests == ' + json.dumps(RESOURCE_TUPLES[other]['requests']), branch)

    def test_every_emitted_policy_expression_has_balanced_grouping(self):
        # This is a lexical regression check only. API-server CEL typechecking
        # and actual AdmissionReview positive/negative cases remain separate.
        for item in render(plan())['foundations.json']['items']:
            if item['kind'] != 'ValidatingAdmissionPolicy':
                continue
            for field in ('matchConditions', 'validations'):
                for number, rule in enumerate(item['spec'].get(field, [])):
                    with self.subTest(policy=item['metadata']['name'], field=field, number=number):
                        split_top_level(rule['expression'], '||')

    def test_serialization_admission_limits_the_affinity_shape_and_freezes_identity(self):
        guard = policy(render(plan()), '-workers')['spec']
        rules = {rule['message']: rule['expression'] for rule in guard['validations']}
        expression = rules['NUC workers require cross-namespace serialization']
        required = 'object.spec.affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution'
        for clause in (
                '!has(object.spec.affinity.nodeAffinity)',
                '!has(object.spec.affinity.podAffinity)',
                'size(object.spec.affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution) == 0',
                'size(' + required + ') == 1', required + '.all(t,',
                't.topologyKey == "kubernetes.io/hostname"',
                't.labelSelector.matchLabels == ' + json.dumps({SERIAL_LABEL: 'nuc-kvm'}),
                'size(t.labelSelector.matchExpressions) == 0', 'has(t.namespaceSelector)',
                'size(t.namespaceSelector.matchLabels) == 0',
                'size(t.namespaceSelector.matchExpressions) == 0',
                'size(t.namespaces) == 0', 'size(t.matchLabelKeys) == 0', 'size(t.mismatchLabelKeys) == 0'):
            self.assertIn(clause, expression)
        updates = rules['NUC updates preserve scope, profile, worker identity and the bound node']
        for label in (arc.SCOPE_LABEL, arc.PROFILE_LABEL, 'runner-reserve.github.io/role', SERIAL_LABEL):
            key = json.dumps(label)
            self.assertIn('object.metadata.labels[' + key + '] == oldObject.metadata.labels[' + key + ']', updates)
        scheduling = rules['NUC KVM cannot select another node or scheduler']
        for clause in ("object.spec.schedulerName == 'default-scheduler'", "request.operation == 'CREATE'",
                       "request.operation == 'UPDATE'", "oldObject.spec.nodeName == 'nuc'",
                       'object.spec.nodeName == oldObject.spec.nodeName'):
            self.assertIn(clause, scheduling)

    def test_complete_namespace_network_rules_replace_not_add_old_guard_egress(self):
        documents, original = render(plan()), render(None)
        items, old_items = documents['foundations.json']['items'], original['foundations.json']['items']
        expected = {'arc-private-ci': {arc.name(target[0], 'scope') for target in TARGETS if target[1] == 'ci'},
                    'arc-private-automation': {arc.name('example/heavy', 'scope')}}
        for namespace, scopes in expected.items():
            with self.subTest(namespace=namespace):
                networks = [item for item in items if item['kind'] == 'NetworkPolicy'
                            and item['metadata']['namespace'] == namespace]
                self.assertEqual(len(networks), 2)
                old = next(item for item in networks if item['metadata']['name'] == 'reserve-worker-isolation')
                new = next(item for item in networks if item != old)
                self.assertEqual(old['spec']['podSelector'], {'matchExpressions': [{
                    'key': arc.SCOPE_LABEL, 'operator': 'NotIn', 'values': sorted(scopes)}]})
                self.assertEqual(selected_scopes(new['spec']['podSelector']), scopes)
                baseline = next(item for item in old_items if item['kind'] == 'NetworkPolicy'
                                and item['metadata']['namespace'] == namespace)
                expected_egress = copy.deepcopy(baseline['spec']['egress'])
                for rule in expected_egress:
                    if any(port['port'] == 19999 for port in rule.get('ports', [])):
                        rule['to'] = [{'ipBlock': {'cidr': NUC_IP + '/32'}}]
                self.assertEqual(new['spec']['egress'], expected_egress)
                self.assertEqual(new['spec']['ingress'], [])
                self.assertNotIn(MP_IP + '/32', json.dumps(new['spec']))
        untouched = [item for item in old_items if item['kind'] == 'NetworkPolicy'
                     and item['metadata']['namespace'] not in expected]
        for item in untouched:
            self.assertIn(item, items)

    def test_global_guards_cover_updates_subresources_and_direct_binding(self):
        documents = render(plan())
        items = documents['foundations.json']['items']
        identities = [(item['kind'], item['metadata'].get('namespace'), item['metadata']['name']) for item in items]
        self.assertEqual(len(identities), len(set(identities)))
        for suffix in ('-workers', '-class', '-binding'):
            guard = policy(documents, suffix)
            self.assertEqual(guard['spec']['failurePolicy'], 'Fail')
            binding = next(item for item in items if item['kind'] == 'ValidatingAdmissionPolicyBinding'
                           and item['metadata']['name'] == guard['metadata']['name'])
            self.assertEqual(binding['spec'], {'policyName': guard['metadata']['name'], 'validationActions': ['Deny']})
        guard = policy(documents, '-workers')['spec']
        self.assertNotIn('namespaceSelector', guard['matchConstraints'])
        resources = {resource for rule in guard['matchConstraints']['resourceRules'] for resource in rule['resources']}
        self.assertTrue({'pods', 'pods/status', 'pods/ephemeralcontainers'} <= resources)
        for resource in ('pods', 'pods/status', 'pods/ephemeralcontainers'):
            operations = {operation for rule in guard['matchConstraints']['resourceRules']
                          if resource in rule['resources'] for operation in rule['operations']}
            self.assertIn('UPDATE', operations)
            if resource == 'pods':
                self.assertIn('CREATE', operations)
        self.assertIn('oldObject', json.dumps(guard['matchConditions']))
        binding = policy(documents, '-binding')['spec']
        self.assertNotIn('namespaceSelector', binding['matchConstraints'])
        resources = {resource for rule in binding['matchConstraints']['resourceRules'] for resource in rule['resources']}
        self.assertEqual(resources, {'pods/binding', 'bindings'})
        self.assertTrue(all(rule['operations'] == ['CREATE'] for rule in binding['matchConstraints']['resourceRules']))
        serialized = json.dumps(binding)
        for namespace in ('arc-private-ci', 'arc-private-automation'):
            self.assertIn(namespace, serialized)
        self.assertNotIn('arc-private-release', serialized)
        expressions = '\n'.join(rule['expression'] for rule in binding['validations'])
        self.assertRegex(expressions, r"request\.userInfo\.username\s*==\s*['\"]system:kube-scheduler['\"]")
        self.assertNotIn('object.metadata.labels', serialized)

    def test_no_mp_fallback_is_bound_to_each_scope_and_namespace_not_profile_label(self):
        documents = render(plan())
        shared = next(item for item in documents['foundations.json']['items']
                      if item['kind'] == 'ValidatingAdmissionPolicy' and item['metadata']['name'] == arc.PRIVATE_ADMISSION)
        baseline = next(item for item in render(None)['foundations.json']['items']
                        if item['kind'] == 'ValidatingAdmissionPolicy' and item['metadata']['name'] == arc.PRIVATE_ADMISSION)
        extra = [rule for rule in shared['spec']['validations'] if rule not in baseline['spec']['validations']]
        for scope, profile in TARGETS:
            matches = [rule['expression'] for rule in extra if arc.name(scope, 'scope') in rule['expression']
                       and 'arc-private-' + profile in rule['expression']]
            self.assertTrue(matches, (scope, profile))
            for expression in matches:
                self.assertNotIn(arc.PROFILE_LABEL, expression)

    def test_legacy_single_scope_and_explicit_plan_agree_but_legacy_remains_standard_only(self):
        self.assertEqual(render(arc.NucKvmConfig(TARGETS[0][0], NUC_IMAGE, NUC_IP)), render(plan((TARGETS[0],))))
        with self.assertRaises(ValueError):
            render(arc.NucKvmConfig('example/heavy', NUC_IMAGE, NUC_IP))


class NucProfileDecisionTests(unittest.TestCase):
    """Truth tables for the existing AST subset, not live CEL admission proof."""

    def setUp(self):
        self.documents = render(plan())
        self.guard = policy(self.documents, '-workers')['spec']
        self.rules = {rule['message']: rule['expression'] for rule in self.guard['validations']}

    def context(self, target):
        pod = copy.deepcopy(worker(self.documents, target)['template'])
        namespace = 'arc-private-' + target[1]
        pod['metadata']['namespace'] = namespace
        return {'object': pod, 'oldObject': None,
                'request': {'operation': 'CREATE', 'namespace': namespace}}

    def test_tuple_decisions_reject_cross_swapped_resources_environment_and_identity(self):
        expression = self.rules['NUC workers require one exact scope, namespace, profile, resource and environment tuple']
        paths = {
            'resources': ('object', 'spec', 'containers', 0, 'resources'),
            'environment': ('object', 'spec', 'containers', 0, 'env'),
            'scope': ('object', 'metadata', 'labels', arc.SCOPE_LABEL),
            'profile': ('object', 'metadata', 'labels', arc.PROFILE_LABEL),
            'object_namespace': ('object', 'metadata', 'namespace'),
            'request_namespace': ('request', 'namespace'),
        }

        def value_at(context, path):
            for key in path:
                context = context[key]
            return context

        def replace(context, path, value):
            for key in path[:-1]:
                context = context[key]
            context[path[-1]] = copy.deepcopy(value)

        for target in TARGETS:
            baseline = self.context(target)
            self.assertTrue(identity_expression(expression, baseline), target)
            for donor in TARGETS:
                if donor == target:
                    continue
                other = self.context(donor)
                # Coherent resource+environment and namespace swaps must not
                # bypass the branch's remaining identity/resource constraints.
                groups = [(key,) for key in paths] + [
                    ('resources', 'environment'), ('object_namespace', 'request_namespace'),
                    ('scope', 'profile', 'object_namespace', 'request_namespace')]
                for group in groups:
                    changed = copy.deepcopy(baseline)
                    for component in group:
                        path = paths[component]
                        replace(changed, path, value_at(other, path))
                    if changed == baseline:
                        continue
                    with self.subTest(target=target, donor=donor, swapped=group):
                        self.assertFalse(identity_expression(expression, changed))

    def test_serial_decisions_reject_removed_labels_and_weakened_affinity(self):
        expression = self.rules['NUC workers require cross-namespace serialization']
        for target in TARGETS:
            baseline = self.context(target)
            self.assertTrue(identity_expression(expression, baseline), target)
            for mutation in ('no-label', 'per-scope-label', 'no-affinity', 'soft-only',
                             'no-namespace-selector', 'local-namespaces', 'filtered-namespaces',
                             'extra-term', 'extra-match-label', 'other-topology', 'match-label-keys'):
                context = copy.deepcopy(baseline)
                pod = context['object']
                anti = pod['spec']['affinity']['podAntiAffinity']
                terms = anti['requiredDuringSchedulingIgnoredDuringExecution']
                term = terms[0]
                if mutation == 'no-label':
                    del pod['metadata']['labels'][SERIAL_LABEL]
                elif mutation == 'per-scope-label':
                    pod['metadata']['labels'][SERIAL_LABEL] = arc.name(target[0], 'scope')
                elif mutation == 'no-affinity':
                    del pod['spec']['affinity']
                elif mutation == 'soft-only':
                    del anti['requiredDuringSchedulingIgnoredDuringExecution']
                    anti['preferredDuringSchedulingIgnoredDuringExecution'] = [{'weight': 100, 'podAffinityTerm': term}]
                elif mutation == 'no-namespace-selector':
                    del term['namespaceSelector']
                elif mutation == 'local-namespaces':
                    term['namespaces'] = [baseline['request']['namespace']]
                elif mutation == 'filtered-namespaces':
                    term['namespaceSelector']['matchLabels'] = {'scope': 'one-namespace'}
                elif mutation == 'extra-term':
                    terms.append(copy.deepcopy(term))
                elif mutation == 'extra-match-label':
                    term['labelSelector']['matchLabels']['scope'] = 'one-repository'
                elif mutation == 'other-topology':
                    term['topologyKey'] = 'topology.kubernetes.io/zone'
                elif mutation == 'match-label-keys':
                    term['matchLabelKeys'] = [arc.SCOPE_LABEL]
                with self.subTest(target=target, mutation=mutation):
                    self.assertFalse(identity_expression(expression, context))

    def test_scheduling_decisions_reject_direct_assignment_and_allow_bound_updates(self):
        scheduling = self.rules['NUC KVM cannot select another node or scheduler']
        immutable = self.rules['NUC updates preserve scope, profile, worker identity and the bound node']
        for target in TARGETS:
            baseline = self.context(target)
            for operation, old_node, new_node, accepted in (
                    ('CREATE', None, None, True), ('CREATE', None, 'nuc', False),
                    ('CREATE', None, 'mp', False), ('UPDATE', None, None, True),
                    ('UPDATE', 'nuc', 'nuc', True), ('UPDATE', None, 'nuc', False),
                    ('UPDATE', 'nuc', None, False), ('UPDATE', 'nuc', 'mp', False)):
                context = copy.deepcopy(baseline)
                context['request']['operation'] = operation
                if operation == 'UPDATE':
                    context['oldObject'] = copy.deepcopy(baseline['object'])
                    if old_node is not None:
                        context['oldObject']['spec']['nodeName'] = old_node
                if new_node is not None:
                    context['object']['spec']['nodeName'] = new_node
                with self.subTest(target=target, operation=operation, old_node=old_node, new_node=new_node):
                    self.assertEqual(identity_expression(scheduling, context), accepted)
                    if accepted:
                        self.assertTrue(identity_expression(immutable, context))
            changed = copy.deepcopy(baseline)
            changed['object']['spec']['schedulerName'] = 'other-scheduler'
            self.assertFalse(identity_expression(scheduling, changed))

    def test_binding_decisions_allow_only_scheduler_in_each_affected_namespace(self):
        guard = policy(self.documents, '-binding')['spec']
        condition = guard['matchConditions'][0]['expression']
        expression = guard['validations'][0]['expression']
        for namespace in ('arc-private-ci', 'arc-private-automation'):
            for actor in ('system:kube-scheduler', 'operator',
                          'system:serviceaccount:arc-systems:arc-controller'):
                context = {'request': {'namespace': namespace, 'operation': 'CREATE',
                                       'userInfo': {'username': actor}}}
                with self.subTest(namespace=namespace, actor=actor):
                    self.assertTrue(identity_expression(condition, context))
                    self.assertEqual(identity_expression(expression, context), actor == 'system:kube-scheduler')
        self.assertFalse(identity_expression(condition, {'request': {'namespace': 'arc-private-release'}}))


class NucReleaseTests(unittest.TestCase):
    """An explicitly selected release keeps its own disk and credential boundary."""

    target = ('example/standard', 'release')

    def setUp(self):
        self.original = render(plan())
        self.documents = render(plan((*TARGETS, self.target)))
        self.guard = policy(self.documents, '-workers')['spec']
        self.rules = {rule['message']: rule['expression'] for rule in self.guard['validations']}

    def context(self):
        pod = copy.deepcopy(worker(self.documents, self.target)['template'])
        pod['metadata']['namespace'] = 'arc-private-release'
        return {'object': pod, 'oldObject': None,
                'request': {'operation': 'CREATE', 'namespace': 'arc-private-release'}}

    def test_release_is_opt_in_and_existing_workers_are_unchanged(self):
        changed = {key for key in self.documents if self.documents[key] != self.original[key]}
        self.assertEqual(changed, {'foundations.json', arc.name(*self.target) + '.json'})
        before, after = worker(self.original, self.target), worker(self.documents, self.target)
        for key in ('githubConfigUrl', 'githubConfigSecret', 'runnerScaleSetName',
                    'listenerTemplate', 'minRunners', 'maxRunners'):
            self.assertEqual(before[key], after[key])
        self.assertEqual(after['runnerScaleSetName'], arc.name(*self.target))
        self.assertEqual(self.documents['repository-variables.json'], self.original['repository-variables.json'])

    def test_release_keeps_disk_resources_bootstrap_and_no_cluster_credentials(self):
        before = worker(self.original, self.target)['template']['spec']
        after = worker(self.documents, self.target)['template']['spec']
        self.assertEqual(after['runtimeClassName'], arc.NUC_RUNTIME)
        self.assertEqual(after['affinity'], AFFINITY)
        self.assertFalse(after['automountServiceAccountToken'])
        runner = after['containers'][0]
        self.assertEqual(runner['resources'], {
            'requests': {'cpu': '50m', 'memory': '512Mi', 'ephemeral-storage': '1Gi'},
            'limits': {'cpu': '1', 'memory': '2Gi', 'ephemeral-storage': '20Gi'}})
        expected = copy.deepcopy(before)
        expected['runtimeClassName'] = arc.NUC_RUNTIME
        expected['nodeSelector'] = {'kubernetes.io/hostname': 'nuc', 'kubernetes.io/arch': 'amd64'}
        expected['schedulerName'] = 'default-scheduler'
        expected['affinity'] = AFFINITY
        expected['containers'][0]['image'] = NUC_IMAGE
        for env in expected['containers'][0]['env']:
            if env['name'] == 'RUNNER_RESERVE_NODE_IP':
                env['value'] = NUC_IP
        self.assertEqual(after, expected)
        env = {item['name']: item for item in runner['env']}
        self.assertEqual(env['RUNNER_DOCKER_STORAGE_DRIVER']['value'], 'overlay2')
        self.assertNotIn('RUNNER_PRIVATE_DOCKER_STORAGE', env)
        budget = namespace_budget(self.documents, 'release')
        self.assertEqual(budget, namespace_budget(self.original, 'release'))
        for resource, amount in quota_charge(self.documents, self.target).items():
            self.assertLessEqual(amount, quantity(budget[resource]))

    def test_release_tuple_rejects_borrowed_profile_or_bootstrap_overrides(self):
        expression = self.rules['NUC workers require one exact scope, namespace, profile, resource and environment tuple']
        self.assertTrue(identity_expression(expression, self.context()))
        for mutation in ('resources', 'profile', 'scope', 'namespace', 'tmpfs', 'driver', 'proxy'):
            context = self.context()
            runner = context['object']['spec']['containers'][0]
            labels = context['object']['metadata']['labels']
            if mutation == 'resources':
                runner['resources'] = RESOURCE_TUPLES[TARGETS[0]]
            elif mutation == 'profile':
                labels[arc.PROFILE_LABEL] = 'ci'
            elif mutation == 'scope':
                labels[arc.SCOPE_LABEL] = arc.name('example/heavy', 'scope')
            elif mutation == 'namespace':
                context['request']['namespace'] = context['object']['metadata']['namespace'] = 'arc-private-ci'
            elif mutation == 'driver':
                next(item for item in runner['env'] if item['name'] == 'RUNNER_DOCKER_STORAGE_DRIVER')['value'] = 'overlayfs'
            else:
                name, value = ('RUNNER_PRIVATE_DOCKER_STORAGE', 'ci-tmpfs-2gi') if mutation == 'tmpfs' else ('HTTPS_PROXY', 'http://redirect')
                runner['env'].append({'name': name, 'value': value})
            with self.subTest(mutation=mutation):
                self.assertFalse(identity_expression(expression, context))

    def test_release_status_keeps_identity_and_scheduler_binding_boundary(self):
        context = self.context()
        context['request']['operation'] = 'UPDATE'
        context['object']['spec']['nodeName'] = 'nuc'
        context['oldObject'] = copy.deepcopy(context['object'])
        context['object']['status'] = {'phase': 'Succeeded'}
        immutable = self.rules['NUC updates preserve scope, profile, worker identity and the bound node']
        self.assertTrue(identity_expression(immutable, context))
        for label in (arc.SCOPE_LABEL, arc.PROFILE_LABEL, 'runner-reserve.github.io/role', SERIAL_LABEL):
            altered = copy.deepcopy(context)
            del altered['object']['metadata']['labels'][label]
            self.assertFalse(identity_expression(immutable, altered), label)
        binding = policy(self.documents, '-binding')['spec']
        request = {'request': {'namespace': 'arc-private-release', 'userInfo': {'username': 'system:kube-scheduler'}}}
        self.assertTrue(identity_expression(binding['matchConditions'][0]['expression'], request))
        self.assertTrue(identity_expression(binding['validations'][0]['expression'], request))
        request['request']['userInfo']['username'] = 'release-job'
        self.assertFalse(identity_expression(binding['validations'][0]['expression'], request))

    def test_release_network_does_not_gain_lan_or_keep_the_old_guard(self):
        items = self.documents['foundations.json']['items']
        old = next(item for item in self.original['foundations.json']['items']
                   if item['kind'] == 'NetworkPolicy' and item['metadata']['namespace'] == 'arc-private-release')
        new = next(item for item in items if item['kind'] == 'NetworkPolicy'
                   and item['metadata']['namespace'] == 'arc-private-release'
                   and item['metadata']['name'] == arc.NUC_RUNTIME + '-isolation')
        self.assertEqual(selected_scopes(new['spec']['podSelector']), {arc.name(self.target[0], 'scope')})
        expected = copy.deepcopy(old['spec']['egress'])
        for rule in expected:
            if any(port['port'] == 19999 for port in rule.get('ports', [])):
                rule['to'] = [{'ipBlock': {'cidr': NUC_IP + '/32'}}]
        self.assertEqual(new['spec']['egress'], expected)
        self.assertEqual(new['spec']['ingress'], [])

    def test_release_still_requires_private_declared_profile(self):
        for changes in ({'visibility': 'public'}, {'profiles': ['ci', 'automation']}):
            rows = copy.deepcopy(ROWS)
            rows[0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                render(plan((self.target,)), rows)


class NucProfileInputTests(unittest.TestCase):
    def test_invalid_typed_plans_cannot_silently_fall_back(self):
        bad = [
            arc.NucKvmPlan((), NUC_IMAGE, NUC_IP),
            arc.NucKvmPlan([arc.NucKvmTarget(*TARGETS[0])], NUC_IMAGE, NUC_IP),
            arc.NucKvmPlan(({'scope': TARGETS[0][0], 'profile': 'ci'},), NUC_IMAGE, NUC_IP),
            plan((TARGETS[0], TARGETS[0])),
            plan((('example/missing', 'ci'),)),
            plan((('example/heavy', 'release'),)),
            plan((('example/unselected', 'lan'),)),
            plan((('example/unselected', 'arm64'),)),
            plan((('example/standard', 'unknown'),)),
            plan((('example/standard;bad', 'ci'),)),
            plan((('example/standard', None),)),
            plan(image='image:latest'), plan(image=None), plan(node_ip=None),
            dict(targets=TARGETS, image=NUC_IMAGE, node_ip=NUC_IP),
        ]
        for config in bad:
            with self.subTest(config=config), self.assertRaises(ValueError):
                render(config)
        for address in (MP_IP, '127.0.0.1', '0.0.0.0', '169.254.169.254', '224.0.0.1',
                        '::1', 'host.invalid', NUC_IP + '/32', NUC_IP + ':19999'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                render(plan(node_ip=address))

    def test_selected_inventory_requires_private_declared_compatible_profiles(self):
        mutations = [
            (0, {'visibility': 'public'}), (0, {'ci_storage': 'disk'}),
            (0, {'ci_resource_class': 'heavy'}),
            (1, {'ci_storage': 'tmpfs-2gi'}), (1, {'ci_resource_class': 'standard'}),
            (1, {'automation_storage': 'disk'}), (1, {'profiles': ['ci']}),
        ]
        for index, changes in mutations:
            rows = copy.deepcopy(ROWS)
            rows[index].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                render(plan(), rows)
        with self.assertRaises(ValueError):
            render(plan(), ROWS + [copy.deepcopy(ROWS[0])])

    def test_strict_plan_json_and_cli_reject_bad_input_before_output_is_created(self):
        valid = {'targets': [{'scope': scope, 'profile': profile} for scope, profile in TARGETS],
                 'image': NUC_IMAGE, 'node_ip': NUC_IP}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / 'inventory.json'
            inventory.write_text(json.dumps({'repositories': ROWS}))
            plan_file = root / 'plan.json'
            base = [sys.executable, '-B', str(ROOT / 'deploy/arc-private/render.py'),
                    '--inventory', str(inventory), '--sandbox-image', MP_IMAGE,
                    '--native-image', NATIVE_IMAGE, '--node-ip', MP_IP,
                    '--listener-node', 'control-node']
            invalid_json = [
                '{}', '[]', 'null', '{"targets": [],',
                json.dumps({**valid, 'extra': True}),
                json.dumps({**valid, 'targets': []}),
                json.dumps({**valid, 'targets': [{'scope': TARGETS[0][0]}]}),
                json.dumps({**valid, 'targets': [{'scope': TARGETS[0][0], 'profile': 'ci', 'extra': True}]}),
                json.dumps({**valid, 'targets': valid['targets'] + [valid['targets'][0]]}),
                json.dumps({key: value for key, value in valid.items() if key != 'node_ip'}),
                '{"targets": [], "targets": ' + json.dumps(valid['targets'])
                + ', "image": ' + json.dumps(NUC_IMAGE) + ', "node_ip": ' + json.dumps(NUC_IP) + '}',
            ]
            for number, content in enumerate(invalid_json):
                plan_file.write_text(content)
                output = root / ('invalid-' + str(number))
                result = subprocess.run(base + ['--output', str(output), '--nuc-kvm-plan', str(plan_file)],
                                        capture_output=True, text=True, timeout=10, check=False)
                with self.subTest(content=content):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(output.exists())
            plan_file.write_text(json.dumps(valid))
            for number, option in enumerate(('--nuc-kvm-scope', '--nuc-kvm-image', '--nuc-kvm-node-ip')):
                value = (TARGETS[0][0], NUC_IMAGE, NUC_IP)[number]
                for mixed in (False, True):
                    output = root / ('partial-' + str(number) + '-' + str(mixed))
                    args = base + ['--output', str(output), option, value]
                    if mixed:
                        args += ['--nuc-kvm-plan', str(plan_file)]
                    result = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
                    with self.subTest(option=option, mixed=mixed):
                        self.assertNotEqual(result.returncode, 0)
                        self.assertFalse(output.exists())
            output = root / 'complete'
            result = subprocess.run(base + ['--output', str(output), '--nuc-kvm-plan', str(plan_file)],
                                    capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = {path.name: json.loads(path.read_text()) for path in output.glob('*.json')}
            self.assertEqual(actual, render(plan()))


if __name__ == '__main__':
    unittest.main()
