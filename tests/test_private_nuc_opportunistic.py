"""Scoped 25m opt-in; offline truth tables do not qualify live CEL or throughput."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    'opportunistic_helpers', Path(__file__).with_name('test_private_nuc_light.py'))
light = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(light)
h = light.h
arc = h.arc
STANDARD, HEAVY, AUTOMATION = h.TARGETS


def plan(targets=h.TARGETS):
    return h.plan()._replace(
        opportunistic_cpu_targets=tuple(arc.NucKvmTarget(*target) for target in targets))


def context(documents, target):
    pod = copy.deepcopy(h.worker(documents, target)['template'])
    namespace = 'arc-private-' + target[1]
    pod['metadata']['namespace'] = namespace
    return {'object': pod, 'oldObject': None,
            'request': {'namespace': namespace, 'operation': 'CREATE'}}


def rules(documents):
    return {rule['message']: rule['expression'] for rule in
            h.policy(documents, '-workers')['spec']['validations']}


class NucOpportunisticTests(unittest.TestCase):
    def test_empty_opt_in_is_identical_and_json_round_trip_is_explicit(self):
        data = {'targets': [t._asdict() for t in h.plan().targets],
                'image': h.plan().image, 'node_ip': h.plan().node_ip}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'plan.json'
            for extra in ({}, {'opportunistic_cpu_targets': []}):
                path.write_text(json.dumps(dict(data, **extra)))
                self.assertEqual(arc.load_nuc_kvm_plan(path), h.plan())
                self.assertEqual(h.render(arc.load_nuc_kvm_plan(path)), h.render(h.plan()))
            data['opportunistic_cpu_targets'] = [t._asdict() for t in plan().opportunistic_cpu_targets]
            path.write_text(json.dumps(data))
            self.assertEqual(arc.load_nuc_kvm_plan(path), plan())
            data['opportunistic_cpu_targets'].reverse()
            path.write_text(json.dumps(data))
            self.assertEqual(h.render(arc.load_nuc_kvm_plan(path)), h.render(plan()))

    def test_only_selected_cpu_and_exact_admission_tuple_change(self):
        before, after = h.render(h.plan()), h.render(plan())
        self.assertEqual({k for k in before if before[k] != after[k]},
                         {'foundations.json', *(arc.name(*t) + '.json' for t in h.TARGETS)})
        for target in h.TARGETS:
            original = h.worker(before, target)
            changed = copy.deepcopy(h.worker(after, target))
            resources = changed['template']['spec']['containers'][0]['resources']
            self.assertEqual(resources['requests']['cpu'], '25m')
            self.assertEqual(changed['maxRunners'], 1)
            self.assertNotIn(arc.CONCURRENCY_LABEL, changed['template']['metadata']['labels'])
            self.assertEqual(h.quota_charge(after, target)['requests.cpu'], h.quantity('125m'))
            resources['requests']['cpu'] = original['template']['spec']['containers'][0]['resources']['requests']['cpu']
            self.assertEqual(changed, original)
        old, new = before['foundations.json'], copy.deepcopy(after['foundations.json'])
        changed_items = [(a, b) for a, b in zip(old['items'], new['items']) if a != b]
        self.assertEqual(len(changed_items), 1)
        a, b = changed_items[0]
        self.assertEqual(b['metadata']['name'], arc.NUC_RUNTIME + '-workers')
        changed_rules = [(i, x) for i, (x, y) in enumerate(zip(
            a['spec']['validations'], b['spec']['validations'])) if x != y]
        self.assertEqual(len(changed_rules), 1)
        index, original_rule = changed_rules[0]
        self.assertEqual(original_rule['message'], light.TUPLE_RULE)
        b['spec']['validations'][index] = original_rule
        self.assertEqual(new, old)

    def test_selection_does_not_spread_to_other_roles_or_scopes(self):
        for target in h.TARGETS:
            before, after = h.render(h.plan()), h.render(plan((target,)))
            self.assertEqual({k for k in before if before[k] != after[k]},
                             {'foundations.json', arc.name(*target) + '.json'})
        # A separate automation opt-in does not replace that scope's 100m CI choice.
        documents = h.render(plan((AUTOMATION,))._replace(low_share_ci_scopes=(HEAVY[0],)))
        self.assertEqual(h.worker(documents, HEAVY)['template']['spec']['containers'][0]
                         ['resources']['requests']['cpu'], '100m')
        self.assertEqual(h.worker(documents, AUTOMATION)['template']['spec']['containers'][0]
                         ['resources']['requests']['cpu'], '25m')

    def test_full_memory_and_bounded_heavy_contracts_are_preserved(self):
        configurations = (
            h.plan()._replace(full_memory_ci_scopes=(STANDARD[0],)),
            h.plan()._replace(bounded_heavy_ci_scopes=(HEAVY[0],),
                             bounded_heavy_image='localhost/bounded@sha256:' + 'd' * 64),
        )
        for config in configurations:
            before = h.render(config)
            after = h.render(config._replace(opportunistic_cpu_targets=plan().opportunistic_cpu_targets))
            for target in h.TARGETS:
                worker = copy.deepcopy(h.worker(after, target))
                worker['template']['spec']['containers'][0]['resources']['requests']['cpu'] = (
                    h.worker(before, target)['template']['spec']['containers'][0]['resources']['requests']['cpu'])
                self.assertEqual(worker, h.worker(before, target))

    def test_typed_selection_rejects_invalid_roles_scope_duplicates_and_overlap(self):
        invalid = (None, [], (True,), (('example/standard', 'ci'),),
                   (arc.NucKvmTarget(True, 'ci'),), (arc.NucKvmTarget('bad scope', 'ci'),),
                   (arc.NucKvmTarget(*STANDARD),) * 2,
                   (arc.NucKvmTarget('missing/repository', 'ci'),),
                   (arc.NucKvmTarget('example/unselected', 'ci'),),
                   (arc.NucKvmTarget(STANDARD[0], 'automation'),),
                   *( (arc.NucKvmTarget(STANDARD[0], role),) for role in
                      ('release', 'lan', 'arm64', '', True)))
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                h.render(h.plan()._replace(opportunistic_cpu_targets=value))
        with self.assertRaises(ValueError):
            h.render(plan()._replace(low_share_ci_scopes=(STANDARD[0],)))
        with self.assertRaises(ValueError):
            h.render(plan()._replace(light_ci_scopes=(STANDARD[0],)))
        # Even an automation-only selection may not share its scope with light CI.
        rows = copy.deepcopy(h.ROWS)
        rows[0]['automation_storage'] = 'tmpfs-3gi'
        config = h.plan((*h.TARGETS, (STANDARD[0], 'automation')))._replace(
            light_ci_scopes=(STANDARD[0],),
            opportunistic_cpu_targets=(arc.NucKvmTarget(STANDARD[0], 'automation'),))
        with self.assertRaises(ValueError):
            h.render(config, rows)
        for mutation in ('public', 'missing-profile', 'storage'):
            rows = copy.deepcopy(h.ROWS)
            if mutation == 'public':
                rows[1]['visibility'] = 'public'
            elif mutation == 'missing-profile':
                rows[1]['profiles'].remove('automation')
            else:
                rows[1]['automation_storage'] = 'disk'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                h.render(plan(), rows)

    def test_json_rejects_malformed_or_arbitrary_cpu_contracts(self):
        data = {'targets': [t._asdict() for t in h.plan().targets],
                'image': h.plan().image, 'node_ip': h.plan().node_ip}
        invalid = (None, True, 25, 'example/standard', [False], [{}],
                   [{'scope': STANDARD[0]}], [{'scope': STANDARD[0], 'profile': 'ci', 'cpu': '25m'}],
                   [{'scope': [], 'profile': 'ci'}], [{'scope': STANDARD[0], 'profile': 'release'}])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'plan.json'
            for value in invalid:
                path.write_text(json.dumps(dict(data, opportunistic_cpu_targets=value)))
                with self.subTest(value=value), self.assertRaises(ValueError):
                    h.render(arc.load_nuc_kvm_plan(path))
            path.write_text(json.dumps(dict(data, cpu_request='25m')))
            with self.assertRaises(ValueError):
                arc.load_nuc_kvm_plan(path)
            path.write_text(json.dumps(data)[:-1] + ', "opportunistic_cpu_targets": [], "opportunistic_cpu_targets": []}')
            with self.assertRaises(ValueError):
                arc.load_nuc_kvm_plan(path)

    def test_exact_admission_rejects_cpu_substitutions_and_unselected_borrowing(self):
        documents = h.render(plan((STANDARD, AUTOMATION)))
        expression = rules(documents)[light.TUPLE_RULE]
        for target in h.TARGETS:
            baseline = context(documents, target)
            self.assertTrue(h.identity_expression(expression, baseline))
            current = baseline['object']['spec']['containers'][0]['resources']['requests']['cpu']
            for cpu in ('0', '24m', '25m', '26m', '100m', '600m'):
                if cpu == current:
                    continue
                altered = copy.deepcopy(baseline)
                altered['object']['spec']['containers'][0]['resources']['requests']['cpu'] = cpu
                with self.subTest(target=target, cpu=cpu):
                    self.assertFalse(h.identity_expression(expression, altered))
            for category, resource, value in (('limits', 'cpu', '3'), ('requests', 'memory', '1Gi')):
                altered = copy.deepcopy(baseline)
                altered['object']['spec']['containers'][0]['resources'][category][resource] = value
                self.assertFalse(h.identity_expression(expression, altered))
        baseline = context(documents, STANDARD)
        for label, value in ((arc.SCOPE_LABEL, arc.name(HEAVY[0], 'scope')),
                             (arc.SCOPE_LABEL, 'unselected'), (arc.PROFILE_LABEL, 'release')):
            altered = copy.deepcopy(baseline)
            altered['object']['metadata']['labels'][label] = value
            self.assertFalse(h.identity_expression(expression, altered))
        altered = copy.deepcopy(baseline)
        altered['request']['namespace'] = altered['object']['metadata']['namespace'] = 'arc-private-automation'
        self.assertFalse(h.identity_expression(expression, altered))
        for label in (arc.SCOPE_LABEL, arc.PROFILE_LABEL):
            altered = copy.deepcopy(baseline)
            del altered['object']['metadata']['labels'][label]
            self.assertFalse(h.identity_expression(expression, altered))

    def test_runtime_overhead_stays_mandatory_and_exact(self):
        documents = h.render(plan())
        expression = rules(documents)['NUC worker overhead must match its RuntimeClass']
        for target in h.TARGETS:
            baseline = context(documents, target)
            self.assertFalse(h.identity_expression(expression, baseline))
            baseline['object']['spec']['overhead'] = {'cpu': '100m', 'memory': '128Mi'}
            self.assertTrue(h.identity_expression(expression, baseline))
            for cpu in ('0', '25m', '99m', '101m'):
                altered = copy.deepcopy(baseline)
                altered['object']['spec']['overhead']['cpu'] = cpu
                self.assertFalse(h.identity_expression(expression, altered))
            for overhead in ({'cpu': '100m'}, {'cpu': '100m', 'memory': '0'}):
                altered = copy.deepcopy(baseline)
                altered['object']['spec']['overhead'] = overhead
                self.assertFalse(h.identity_expression(expression, altered))

    def test_mixed_light_plan_still_serializes_opportunistic_workers(self):
        config = light.plan()._replace(opportunistic_cpu_targets=(
            arc.NucKvmTarget(*HEAVY), arc.NucKvmTarget(*AUTOMATION)))
        documents = h.render(config, light.ROWS)
        expressions = rules(documents)
        for target in (HEAVY, AUTOMATION):
            baseline = context(documents, target)
            self.assertTrue(h.identity_expression(expressions[light.TUPLE_RULE], baseline))
            self.assertTrue(h.identity_expression(expressions[light.SCHEDULE_RULE], baseline))
            for peer in light.TARGETS:
                own, other = baseline['object'], context(documents, peer)['object']
                self.assertFalse(light.can_share(own, other))
                self.assertFalse(light.can_share(other, own))
            altered = copy.deepcopy(baseline)
            altered['object']['metadata']['labels'][arc.CONCURRENCY_LABEL] = 'light'
            self.assertFalse(h.identity_expression(expressions[light.TUPLE_RULE], altered))
            self.assertFalse(h.identity_expression(expressions[light.SCHEDULE_RULE], altered))
            for mutation in ('local-namespace', 'empty-selector', 'two-terms', 'wrong-domain'):
                altered = copy.deepcopy(baseline)
                term = altered['object']['spec']['affinity']['podAntiAffinity']['requiredDuringSchedulingIgnoredDuringExecution'][0]
                if mutation == 'local-namespace':
                    del term['namespaceSelector']
                elif mutation == 'empty-selector':
                    term['labelSelector']['matchLabels'] = {}
                elif mutation == 'two-terms':
                    altered['object']['spec']['affinity']['podAntiAffinity']['requiredDuringSchedulingIgnoredDuringExecution'].append(copy.deepcopy(term))
                else:
                    altered['object']['metadata']['labels'][arc.SERIAL_LABEL] = 'other'
                with self.subTest(mutation=mutation):
                    self.assertFalse(h.identity_expression(expressions[light.SCHEDULE_RULE], altered))


if __name__ == '__main__':
    unittest.main()
