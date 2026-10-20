"""Offline capacity plans must bind every pool and the aggregate quotas."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('capacity_renderer', ROOT / 'deploy/runner-reserve/render_pool.py')
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)
GIB = 1024 ** 3


class CapacityPlanTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / 'capacity.json'
        self.now = datetime.now(timezone.utc)
        self.inventory = renderer.load_public_inventory(ROOT / 'deploy/runner-reserve/repositories.json')
        self.profiles = self.inventory['profiles']
        self.scopes = [*self.inventory['organizations'], *[r['name'] for r in self.inventory['repositories']
                       if r['name'].split('/')[0] not in self.inventory['organizations']]]
        self.plan = {'schema': 1, 'node': 'mp', 'captured_at': self.now.isoformat().replace('+00:00', 'Z'),
                     'host_available_bytes': 123 * GIB // 2, 'host_reserve_bytes': 8 * GIB,
                     'image_growth_bytes': 5 * GIB, 'other_growth_bytes': 4 * GIB,
                     'overshoot_bytes': 4 * GIB,
                     'allocations': {scope: dict.fromkeys(self.profiles, 0) for scope in self.scopes}}
        self.plan['allocations'][self.scopes[0]]['ci'] = 1
        self.plan['allocations'][self.scopes[1]]['release'] = 1

    def load(self, plan=None):
        self.path.write_text(json.dumps(self.plan if plan is None else plan))
        return renderer.load_capacity_plan(self.path, self.scopes, self.profiles, 'mp', now=self.now)

    def cli(self, *extra):
        args = ['render_pool.py', '--worker-image', 'localhost/reserve@sha256:' + 'a' * 64,
                '--node', 'mp', '--node-ip', '192.0.2.1', '--output', str(self.root / 'output'), *extra]
        with patch('sys.argv', args), patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
            renderer.main()

    def test_complete_plan_uses_live_sized_budget_and_binds_aggregate_quotas(self):
        allocations, totals = self.load()
        self.assertEqual(sum(totals.values()), 2)
        self.cli('--capacity-plan', str(self.path))
        output = self.root / 'output'
        index = json.loads((output / 'index.json').read_text())
        for entry in index:
            values = json.loads((output / entry['values']).read_text())
            self.assertEqual(values['maxRunners'], allocations[entry['scope']][entry['profile']])
            self.assertEqual(values['minRunners'], 0)
        quotas = [item for item in json.loads((output / 'foundations.json').read_text())['items']
                  if item['kind'] == 'ResourceQuota']
        self.assertEqual(sum(int(q['spec']['hard']['pods']) for q in quotas), 2)
        for quota in quotas:
            profile = quota['metadata']['namespace'].removeprefix('runner-reserve-')
            count = totals[profile]
            self.assertEqual(quota['spec']['hard']['limits.ephemeral-storage'], str(count * 20) + 'Gi')
            self.assertEqual(quota['spec']['hard']['limits.memory'], str(count * 4224) + 'Mi')
        self.assertEqual(next(q for q in quotas if q['metadata']['namespace'].endswith('automation'))['spec']['hard']['pods'], '0')

    def test_six_workers_fail_even_with_user_old_65_gib_value(self):
        for scope in self.scopes[:2]:
            self.plan['allocations'][scope] = dict.fromkeys(self.profiles, 1)
        self.plan['host_available_bytes'] = 65 * GIB
        with self.assertRaisesRegex(ValueError, 'Aggregate'):
            self.load()

    def test_boundary_includes_every_budget_and_one_byte_short_fails(self):
        self.plan['host_available_bytes'] = 61 * GIB
        self.load()
        self.plan['host_available_bytes'] -= 1
        with self.assertRaisesRegex(ValueError, 'Aggregate'):
            self.load()

    def test_stale_future_wrong_node_and_bad_timestamp_fail(self):
        invalid = [('captured_at', (self.now - timedelta(seconds=301)).isoformat().replace('+00:00', 'Z')),
                   ('captured_at', (self.now + timedelta(seconds=1)).isoformat().replace('+00:00', 'Z')),
                   ('captured_at', 'not-a-dateZ'), ('captured_at', None), ('node', 'nuc')]
        for key, value in invalid:
            plan = copy.deepcopy(self.plan)
            plan[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.load(plan)

    def test_allocation_keys_types_and_budget_types_fail_closed(self):
        invalid = []
        for value in (True, -1, 2, 0.5, '1'):
            plan = copy.deepcopy(self.plan)
            plan['allocations'][self.scopes[0]]['ci'] = value
            invalid.append(plan)
        for key in ('host_available_bytes', 'host_reserve_bytes', 'image_growth_bytes', 'other_growth_bytes', 'overshoot_bytes'):
            for value in (-1, True, '1', 2 ** 63):
                plan = copy.deepcopy(self.plan)
                plan[key] = value
                invalid.append(plan)
        plan = copy.deepcopy(self.plan)
        del plan['allocations'][self.scopes[0]]
        invalid.append(plan)
        plan = copy.deepcopy(self.plan)
        plan['allocations'][self.scopes[0]]['unknown'] = 0
        invalid.append(plan)
        plan = copy.deepcopy(self.plan)
        plan['host_reserve_bytes'] = 2 * GIB - 1
        invalid.append(plan)
        plan = copy.deepcopy(self.plan)
        plan['overshoot_bytes'] = 0
        invalid.append(plan)
        for plan in invalid:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                self.load(plan)

    def test_rejected_or_missing_plan_never_creates_output(self):
        with self.assertRaises(SystemExit):
            self.cli()
        self.assertFalse((self.root / 'output').exists())
        self.plan['host_available_bytes'] = 1
        self.path.write_text(json.dumps(self.plan))
        with self.assertRaises(SystemExit):
            self.cli('--capacity-plan', str(self.path))
        self.assertFalse((self.root / 'output').exists())

    def test_duplicate_keys_and_symlink_are_rejected(self):
        self.path.write_text('{"schema":1,"schema":1}')
        with self.assertRaises(ValueError):
            renderer.load_capacity_plan(self.path, self.scopes, self.profiles, 'mp')
        self.load()
        alias = self.root / 'alias.json'
        alias.symlink_to(self.path)
        with self.assertRaises(OSError):
            renderer.load_capacity_plan(alias, self.scopes, self.profiles, 'mp')

    def test_held_output_preserves_previous_quotas_and_zero_pools(self):
        self.cli('--held')
        output = self.root / 'output'
        index = json.loads((output / 'index.json').read_text())
        self.assertTrue(all(json.loads((output / entry['values']).read_text())['maxRunners'] == 0 for entry in index))
        quotas = [item for item in json.loads((output / 'foundations.json').read_text())['items']
                  if item['kind'] == 'ResourceQuota']
        self.assertTrue(all(q['spec']['hard']['pods'] == '2' for q in quotas))


if __name__ == '__main__':
    unittest.main()
