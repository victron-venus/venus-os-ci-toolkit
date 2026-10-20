"""Offline opt-in contracts; actual scheduler and CEL qualification is separate."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    'nuc_light_helpers', Path(__file__).with_name('test_private_nuc_profiles.py'))
h = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(h)
arc = h.arc
LIGHT = ('example/standard', 'ci')
OTHER = ('example/other-light', 'ci')
EXCLUSIVE = ('example/serial', 'ci')
RELEASE = ('example/standard', 'release')
TARGETS = (*h.TARGETS, OTHER, EXCLUSIVE, RELEASE)
ROWS = [*h.ROWS, {'name': OTHER[0], 'visibility': 'private',
                  'profiles': ['ci'], 'ci_storage': 'tmpfs-2gi'},
        {'name': EXCLUSIVE[0], 'visibility': 'private',
         'profiles': ['ci'], 'ci_storage': 'tmpfs-2gi'}]
LABEL = arc.CONCURRENCY_LABEL
TUPLE_RULE = 'NUC workers require one exact scope, namespace, profile, resource and environment tuple'
SCHEDULE_RULE = 'NUC workers require bounded light concurrency or exclusive execution'
UPDATE_RULE = 'NUC updates preserve scope, profile, worker identity and the bound node'


def plan(scopes=(LIGHT[0], OTHER[0])):
    return h.plan(TARGETS)._replace(light_ci_scopes=scopes)


def selector_matches(selector, labels):
    """Small label-selector truth table, not a replacement Kubernetes scheduler."""
    if any(labels.get(key) != value for key, value in selector.get('matchLabels', {}).items()):
        return False
    for expression in selector.get('matchExpressions', []):
        if expression['operator'] != 'NotIn':
            raise AssertionError('Unexpected operator in fixture')
        if labels.get(expression['key']) in expression['values']:
            return False
    return True


def can_share(first, second):
    # The scheduler checks incoming affinity and existing Pods' anti-affinity.
    for owner, peer in ((first, second), (second, first)):
        terms = owner['spec']['affinity']['podAntiAffinity']['requiredDuringSchedulingIgnoredDuringExecution']
        for term in terms:
            if term['namespaceSelector'] != {} or term['topologyKey'] != arc.HOSTNAME_LABEL:
                raise AssertionError('This truth table requires the global hostname contract')
            if selector_matches(term['labelSelector'], peer['metadata']['labels']):
                return False
    return True


class NucLightTests(unittest.TestCase):
    def setUp(self):
        self.original = h.render(plan(()), ROWS)
        self.documents = h.render(plan(), ROWS)
        self.guard = h.policy(self.documents, '-workers')['spec']
        self.rules = {rule['message']: rule['expression'] for rule in self.guard['validations']}

    def context(self, target):
        pod = copy.deepcopy(h.worker(self.documents, target)['template'])
        namespace = 'arc-private-' + target[1]
        pod['metadata']['namespace'] = namespace
        return {'object': pod, 'oldObject': None,
                'request': {'operation': 'CREATE', 'namespace': namespace}}

    def test_only_selected_standard_ci_changes_and_default_plan_stays_compatible(self):
        self.assertEqual(self.original, h.render(h.plan(TARGETS), ROWS))
        changed = {key for key in self.original if self.original[key] != self.documents[key]}
        self.assertEqual(changed, {'foundations.json', *(arc.name(*t) + '.json' for t in (LIGHT, OTHER))})
        for target in (LIGHT, OTHER):
            before = h.worker(self.original, target)
            after = copy.deepcopy(h.worker(self.documents, target))
            self.assertEqual(after['maxRunners'], 2)
            self.assertEqual(after['minRunners'], before['minRunners'])
            resources = after['template']['spec']['containers'][0]['resources']
            self.assertEqual(resources['requests']['cpu'], '200m')
            self.assertEqual(resources['limits']['cpu'], '1')
            self.assertEqual(resources['requests']['memory'], '3Gi')
            self.assertEqual(resources['limits']['memory'], '6Gi')
            after['maxRunners'] = before['maxRunners']
            after['template']['metadata']['labels'].pop(LABEL)
            after['template']['spec']['affinity'] = before['template']['spec']['affinity']
            resources['requests']['cpu'] = '600m'
            resources['limits']['cpu'] = '2'
            self.assertEqual(after, before)

    def test_two_light_workers_fit_unchanged_quota_but_third_is_rejected(self):
        before, after = h.namespace_budget(self.original, 'ci'), h.namespace_budget(self.documents, 'ci')
        self.assertEqual(before, after)
        self.assertEqual(after['pods'], '2')
        quotas = [item for item in self.documents['foundations.json']['items']
                  if item['kind'] == 'ResourceQuota' and item['metadata']['namespace'] == 'arc-private-ci']
        self.assertEqual(len(quotas), 1)
        self.assertNotIn('scopes', quotas[0]['spec'])
        self.assertNotIn('scopeSelector', quotas[0]['spec'])
        charges = [h.quota_charge(self.documents, target) for target in (LIGHT, OTHER)]
        for dimension in charges[0]:
            self.assertLessEqual(sum(charge[dimension] for charge in charges), h.quantity(after[dimension]), dimension)
        self.assertEqual(sum(charge['requests.cpu'] for charge in charges), h.quantity('600m'))
        self.assertEqual(sum(charge['limits.cpu'] for charge in charges), h.quantity('2200m'))
        self.assertEqual(sum(charge['requests.memory'] for charge in charges), h.quantity('6400Mi'))
        self.assertEqual(sum(charge['limits.memory'] for charge in charges), h.quantity('12544Mi'))
        self.assertEqual(sum(charge['limits.ephemeral-storage'] for charge in charges), h.quantity('40Gi'))
        self.assertGreater(3 * charges[0]['pods'], h.quantity(after['pods']))
        # Includes the RuntimeClass overhead and the observed service requests.
        self.assertEqual(h.quantity('3500m') - h.quantity('2790m') - h.quantity('600m'), h.quantity('110m'))

    def test_rendered_selectors_allow_light_pairs_and_exclude_every_other_worker_in_both_orders(self):
        pods = {target: self.context(target)['object'] for target in TARGETS}
        for first in TARGETS:
            for second in TARGETS:
                with self.subTest(first=first, second=second):
                    self.assertEqual(can_share(pods[first], pods[second]),
                                     first in (LIGHT, OTHER) and second in (LIGHT, OTHER))
        old_exclusive = h.worker(self.original, LIGHT)['template']
        self.assertNotIn(LABEL, old_exclusive['metadata']['labels'])
        self.assertFalse(can_share(old_exclusive, pods[LIGHT]))
        self.assertFalse(can_share(pods[LIGHT], old_exclusive))

    def test_tuple_rejects_class_spoofing_namespace_moves_and_heavy_resource_substitution(self):
        expression = self.rules[TUPLE_RULE]
        for target in TARGETS:
            baseline = self.context(target)
            self.assertTrue(h.identity_expression(expression, baseline))
            for replacement in (None, 'light', 'exclusive', 'unknown'):
                context = copy.deepcopy(baseline)
                labels = context['object']['metadata']['labels']
                if replacement is None:
                    labels.pop(LABEL, None)
                else:
                    labels[LABEL] = replacement
                if context != baseline:
                    with self.subTest(target=target, replacement=replacement):
                        self.assertFalse(h.identity_expression(expression, context))
            for namespace in ('arc-private-other-ci', 'arc-private-automation', 'arc-private-release'):
                if namespace == baseline['request']['namespace']:
                    continue
                changed = copy.deepcopy(baseline)
                changed['request']['namespace'] = namespace
                changed['object']['metadata']['namespace'] = namespace
                self.assertFalse(h.identity_expression(expression, changed))
        changed = self.context(h.TARGETS[1])
        changed['object']['metadata']['labels'][LABEL] = 'light'
        changed['object']['spec']['containers'][0]['resources'] = self.context(LIGHT)['object']['spec']['containers'][0]['resources']
        self.assertFalse(h.identity_expression(expression, changed))

    def test_scheduling_rejects_weakened_selectors_and_class_mismatch(self):
        expression = self.rules[SCHEDULE_RULE]
        for target in TARGETS:
            self.assertTrue(h.identity_expression(expression, self.context(target)))
        baseline = self.context(LIGHT)
        for mutation in ('no-expressions', 'wrong-operator', 'wrong-value', 'extra-expression',
                         'local-namespace', 'filtered-namespace', 'extra-term', 'no-class', 'unknown-class'):
            context = copy.deepcopy(baseline)
            pod = context['object']
            terms = pod['spec']['affinity']['podAntiAffinity']['requiredDuringSchedulingIgnoredDuringExecution']
            term = terms[0]
            if mutation == 'no-expressions':
                term['labelSelector'].pop('matchExpressions')
            elif mutation == 'wrong-operator':
                term['labelSelector']['matchExpressions'][0]['operator'] = 'In'
            elif mutation == 'wrong-value':
                term['labelSelector']['matchExpressions'][0]['values'] = ['exclusive']
            elif mutation == 'extra-expression':
                term['labelSelector']['matchExpressions'].append({'key': arc.SCOPE_LABEL, 'operator': 'NotIn', 'values': ['other']})
            elif mutation == 'local-namespace':
                term.pop('namespaceSelector')
            elif mutation == 'filtered-namespace':
                term['namespaceSelector'] = {'matchLabels': {'name': 'one-namespace'}}
            elif mutation == 'extra-term':
                terms.append(copy.deepcopy(term))
            elif mutation == 'no-class':
                pod['metadata']['labels'].pop(LABEL)
            else:
                pod['metadata']['labels'][LABEL] = 'unknown'
            with self.subTest(mutation=mutation):
                self.assertFalse(h.identity_expression(expression, context))

    def test_pending_and_bound_updates_cannot_add_remove_or_change_class_including_status(self):
        resources = {resource for rule in self.guard['matchConstraints']['resourceRules'] for resource in rule['resources']}
        self.assertIn('pods/status', resources)
        expression = self.rules[UPDATE_RULE]
        for target in TARGETS:
            for bound in (False, True):
                baseline = self.context(target)
                baseline['request']['operation'] = 'UPDATE'
                if bound:
                    baseline['object']['spec']['nodeName'] = 'nuc'
                baseline['oldObject'] = copy.deepcopy(baseline['object'])
                self.assertTrue(h.identity_expression(expression, baseline))
                for replacement in (None, 'light', 'exclusive', 'unknown'):
                    context = copy.deepcopy(baseline)
                    labels = context['object']['metadata']['labels']
                    if replacement is None:
                        labels.pop(LABEL, None)
                    else:
                        labels[LABEL] = replacement
                    if context != baseline:
                        with self.subTest(target=target, bound=bound, replacement=replacement):
                            self.assertFalse(h.identity_expression(expression, context))

    def test_other_runtime_guards_and_resources_are_unchanged(self):
        before = {item['metadata']['name']: item for item in self.original['foundations.json']['items']
                  if item['kind'] in ('RuntimeClass', 'ValidatingAdmissionPolicy')}
        for item in self.documents['foundations.json']['items']:
            if item['kind'] in ('RuntimeClass', 'ValidatingAdmissionPolicy') and item['metadata']['name'] != arc.NUC_RUNTIME + '-workers':
                self.assertEqual(item, before[item['metadata']['name']])
        for rule in self.guard['validations']:
            h.split_top_level(rule['expression'], '||')
            self.assertLess(len(rule['expression']), 100_000)

    def test_compaction_does_not_merge_classes_even_if_resources_and_environment_match(self):
        light = copy.deepcopy(h.worker(self.documents, LIGHT))
        exclusive = copy.deepcopy(light)
        exclusive['template']['metadata']['labels'].pop(LABEL)
        expression = arc.nuc_targets_validation({arc.NucKvmTarget(*LIGHT): light,
                                                 arc.NucKvmTarget(*EXCLUSIVE): exclusive})
        self.assertEqual(len(h.split_top_level(expression, '||')), 2)
        baseline = self.context(LIGHT)
        self.assertTrue(h.identity_expression(expression, baseline))
        baseline['object']['metadata']['labels'][arc.SCOPE_LABEL] = arc.name(EXCLUSIVE[0], 'scope')
        self.assertFalse(h.identity_expression(expression, baseline))
        baseline['object']['metadata']['labels'].pop(LABEL)
        self.assertTrue(h.identity_expression(expression, baseline))

    def test_invalid_light_plans_fail_closed(self):
        for scopes in (None, LIGHT[0], [LIGHT[0]], (None,), ('bad scope',),
                       (LIGHT[0], LIGHT[0]), ('example/missing',), ('example/heavy',)):
            with self.subTest(scopes=scopes), self.assertRaises(ValueError):
                h.render(plan(scopes), ROWS)
        for target in (('example/heavy', 'automation'), RELEASE):
            config = h.plan((target,))._replace(light_ci_scopes=(target[0],))
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'selected NUC CI target'):
                h.render(config, ROWS)

    def test_json_opt_in_is_explicit_strict_and_preserves_default(self):
        data = {'targets': [dict(scope=scope, profile=profile) for scope, profile in TARGETS],
                'image': h.NUC_IMAGE, 'node_ip': h.NUC_IP}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'plan.json'
            for explicit in (False, True):
                path.write_text(json.dumps({**data, **({'light_ci_scopes': []} if explicit else {})}))
                self.assertEqual(h.render(arc.load_nuc_kvm_plan(path), ROWS), self.original)
            path.write_text(json.dumps({**data, 'light_ci_scopes': [OTHER[0], LIGHT[0]]}))
            self.assertEqual(h.render(arc.load_nuc_kvm_plan(path), ROWS), self.documents)
            for invalid in (None, LIGHT[0], True, {}, [None], [LIGHT[0], LIGHT[0]]):
                path.write_text(json.dumps({**data, 'light_ci_scopes': invalid}))
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    h.render(arc.load_nuc_kvm_plan(path), ROWS)


if __name__ == '__main__':
    unittest.main()
