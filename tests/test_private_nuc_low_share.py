"""Low-share CI retains the exclusive worker and exact admission contracts."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    'nuc_low_share_helpers', Path(__file__).with_name('test_private_nuc_light.py'))
h = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(h)
arc = h.arc
base = h.h
TARGET = h.EXCLUSIVE


def plan():
    return h.plan()._replace(low_share_ci_scopes=(TARGET[0],))


class NucLowShareTests(unittest.TestCase):
    def test_opt_in_changes_only_selected_cpu_request_and_admission_tuple(self):
        before = base.render(h.plan(), h.ROWS)
        after = base.render(plan(), h.ROWS)
        changed = {key for key in before if before[key] != after[key]}
        self.assertEqual(changed, {'foundations.json', arc.name(*TARGET) + '.json'})
        worker = base.worker(after, TARGET)
        self.assertEqual(worker['maxRunners'], 1)
        resources = worker['template']['spec']['containers'][0]['resources']
        self.assertEqual(resources['requests']['cpu'], '100m')
        self.assertEqual(base.quota_charge(after, TARGET)['requests.cpu'], base.quantity('200m'))
        restored = copy.deepcopy(worker)
        restored['template']['spec']['containers'][0]['resources']['requests']['cpu'] = '600m'
        self.assertEqual(restored, base.worker(before, TARGET))
        for key in ('requests', 'limits'):
            for resource in ('memory', 'ephemeral-storage'):
                self.assertEqual(resources[key][resource], base.worker(before, TARGET)
                                 ['template']['spec']['containers'][0]['resources'][key][resource])
        self.assertEqual(resources['limits']['cpu'], '2')

    def test_low_share_still_excludes_every_peer_in_both_orders(self):
        documents = base.render(plan(), h.ROWS)
        own = base.worker(documents, TARGET)['template']
        self.assertNotIn(arc.CONCURRENCY_LABEL, own['metadata']['labels'])
        for target in h.TARGETS:
            peer = base.worker(documents, target)['template']
            with self.subTest(target=target):
                self.assertFalse(h.can_share(own, peer))
                self.assertFalse(h.can_share(peer, own))

    def test_admission_rejects_borrowed_resources_and_labels(self):
        documents = base.render(plan(), h.ROWS)
        expression = next(rule['expression'] for rule in base.policy(documents, '-workers')
                          ['spec']['validations'] if rule['message'] == h.TUPLE_RULE)
        for target in h.TARGETS:
            pod = copy.deepcopy(base.worker(documents, target)['template'])
            ns = 'arc-private-' + target[1]
            pod['metadata']['namespace'] = ns
            context = {'object': pod, 'oldObject': None,
                       'request': {'operation': 'CREATE', 'namespace': ns}}
            self.assertTrue(base.identity_expression(expression, context))
            old_cpu = pod['spec']['containers'][0]['resources']['requests']['cpu']
            for cpu in ('0', '50m', '100m', '200m', '600m'):
                if cpu == old_cpu:
                    continue
                altered = copy.deepcopy(context)
                altered['object']['spec']['containers'][0]['resources']['requests']['cpu'] = cpu
                self.assertFalse(base.identity_expression(expression, altered), (target, cpu))
            altered = copy.deepcopy(context)
            altered['object']['metadata']['labels'][arc.CONCURRENCY_LABEL] = 'light'
            if target == TARGET:
                self.assertFalse(base.identity_expression(expression, altered))

    def test_invalid_or_conflicting_selection_fails_closed(self):
        cases = (None, [], ('missing/repository',), (TARGET[0], TARGET[0]),
                 (True,), ('bad scope',), (h.LIGHT[0],), (base.TARGETS[1][0],))
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                base.render(plan()._replace(low_share_ci_scopes=value), h.ROWS)
        # A repository with only an automation target cannot select its CI implicitly.
        candidate = plan()._replace(targets=tuple(t for t in plan().targets
                                                if t != arc.NucKvmTarget(*TARGET)))
        with self.assertRaises(ValueError):
            base.render(candidate, h.ROWS)

    def test_plan_round_trip_and_strict_types(self):
        data = {'targets': [dict(scope=t.scope, profile=t.profile) for t in plan().targets],
                'image': plan().image, 'node_ip': plan().node_ip,
                'light_ci_scopes': list(plan().light_ci_scopes),
                'low_share_ci_scopes': [TARGET[0]]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'plan.json'
            path.write_text(json.dumps(data))
            self.assertEqual(arc.load_nuc_kvm_plan(path), plan())
            for value in (None, True, 100, TARGET[0], [False], [{}]):
                path.write_text(json.dumps({**data, 'low_share_ci_scopes': value}))
                with self.subTest(value=value), self.assertRaises(ValueError):
                    arc.load_nuc_kvm_plan(path)
            path.write_text(json.dumps({**data, 'cpu_request': '0'}))
            with self.assertRaises(ValueError):
                arc.load_nuc_kvm_plan(path)


if __name__ == '__main__':
    unittest.main()
