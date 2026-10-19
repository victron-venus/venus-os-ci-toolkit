"""Portable public inventory input; rendering remains offline and fail closed."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/runner-reserve/render_pool.py'
SPEC = importlib.util.spec_from_file_location('public_inventory_renderer', SCRIPT)
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)
EXAMPLES = ROOT / 'docs/examples/arc-recovery'
IMAGE = 'registry.example/worker@sha256:' + 'a' * 64


class PublicInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.example = json.loads((EXAMPLES / 'public-repositories.json').read_text())

    def run_cli(self, inventory=None, extra=()):
        output = self.directory / 'rendered'
        command = [sys.executable, str(SCRIPT), '--worker-image', IMAGE,
                   '--node', 'worker-a', '--node-ip', '192.0.2.10',
                   '--output', str(output), '--held', *extra]
        if inventory is not None:
            command += ['--inventory', str(inventory)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        return result, output

    def test_another_org_renders_six_held_scopes_with_matching_labels_and_bindings(self):
        result, output = self.run_cli(EXAMPLES / 'public-repositories.json',
                                      ['--listener-placement', str(EXAMPLES / 'listener-placement.json')])
        self.assertEqual(result.returncode, 0, result.stderr)
        index = json.loads((output / 'index.json').read_text())
        self.assertEqual(len(index), 6)
        for row in index:
            values = json.loads((output / row['values']).read_text())
            self.assertEqual((values['minRunners'], values['maxRunners']), (0, 0))
            self.assertEqual(values['githubConfigUrl'], 'https://github.com/' + row['scope'])
            self.assertEqual(values['scaleSetLabels'], [self.example['profiles'][row['profile']]])
            expected_secret = 'example-org-app' if row['scope'] == 'example-org' else 'reserve-personal-app'
            self.assertEqual(values['githubConfigSecret'], expected_secret)
            self.assertEqual(values['template']['spec']['nodeSelector']['kubernetes.io/hostname'], 'worker-a')
        self.assertFalse((output / 'repository-variables.json').exists())

    def test_invalid_inventory_never_creates_output(self):
        invalid = []
        for key, value in [('schema', True), ('default_mode', 'self-hosted'),
                           ('organizations', ['same', 'SAME']), ('profiles', {'ci': 'only'}),
                           ('repositories', []), ('organization_secrets', {'wrong': 'key'})]:
            invalid.append({**copy.deepcopy(self.example), key: value})
        row = self.example['repositories'][0]
        invalid.extend([
            {**self.example, 'secret_value': 'not-accepted'},
            {**self.example, 'organizations': ['Example-Org'],
             'organization_secrets': {'Example-Org': 'example-org-app'}},
            {**self.example, 'repositories': [row, {**row, 'name': row['name'].upper()}]},
            {**self.example, 'repositories': [{**row, 'name': 'bad/../../path'}]},
            {**self.example, 'repositories': [{**row, 'default_branch': 'bad\nbranch'}]},
            {**self.example, 'profiles': {'ci': 'same', 'automation': 'SAME', 'release': 'third'}},
            {**self.example, 'organization_secrets': {'example-org': 'reserve-personal-app'}},
        ])
        path = self.directory / 'inventory.json'
        for data in invalid:
            with self.subTest(data=data):
                path.write_text(json.dumps(data))
                result, output = self.run_cli(path)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())

    def test_multiple_repositories_for_one_personal_owner_share_installation(self):
        self.example['repositories'].append({'name': 'EXAMPLE-PERSON/second', 'default_branch': 'main'})
        path = self.directory / 'inventory.json'
        path.write_text(json.dumps(self.example))
        result, output = self.run_cli(path)
        self.assertEqual(result.returncode, 0, result.stderr)
        index = json.loads((output / 'index.json').read_text())
        personal = [row for row in index if '/' in row['scope']]
        self.assertEqual(len(personal), 6)
        for row in personal:
            values = json.loads((output / row['values']).read_text())
            self.assertEqual(values['githubConfigSecret'], 'reserve-personal-app')

    def test_second_personal_owner_refuses_before_output(self):
        self.example['repositories'].append({'name': 'second-person/application', 'default_branch': 'main'})
        path = self.directory / 'inventory.json'
        path.write_text(json.dumps(self.example))
        result, output = self.run_cli(path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('only one personal owner', result.stderr)
        self.assertFalse(output.exists())

    def test_duplicate_malformed_and_oversized_json_refuse_before_output(self):
        path = self.directory / 'inventory.json'
        for raw in ['{"schema":1,"schema":1}', '{"profiles":{"ci":1,"ci":2}}',
                    '{', ' ' * 131073]:
            with self.subTest(size=len(raw)):
                path.write_text(raw)
                result, output = self.run_cli(path)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())

    def test_missing_symlink_fifo_and_directory_are_not_accepted_as_inventory(self):
        real = self.directory / 'real.json'
        real.write_text(json.dumps(self.example))
        link = self.directory / 'link.json'
        link.symlink_to(real)
        fifo = self.directory / 'fifo'
        os.mkfifo(fifo)
        for path in [self.directory / 'missing', link, fifo, self.directory]:
            with self.subTest(path=path.name):
                result, output = self.run_cli(path)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())

    def test_default_catalog_render_keeps_baseline_bytes(self):
        result, output = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = b''.join(path.name.encode() + b'\0' + path.read_bytes()
                           for path in sorted(output.iterdir()))
        # Captured with the unmodified renderer at 0aa9122, same fixed arguments.
        self.assertEqual(hashlib.sha256(payload).hexdigest(), 'c27fd45a187054cf15644b4f8199bda23eb8d33abb6e9257524ab10a2330c701')


if __name__ == '__main__':
    unittest.main()
