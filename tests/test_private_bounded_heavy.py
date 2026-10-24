"""Bounded-heavy v1 contracts; offline evaluator is not Kubernetes CEL admission."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = load('bounded_base', 'tests/test_private_nuc_profiles.py')
tmpfs = load('bounded_tmpfs', 'tests/test_private_tmpfs.py')
arc = base.arc
DERIVATIVE = 'localhost/bounded-v1@sha256:' + 'd' * 64
SCOPES = ('example/heavy', 'example/second-heavy')
ROWS = copy.deepcopy(base.ROWS) + [{**copy.deepcopy(base.ROWS[1]), 'name': SCOPES[1]}]
TARGETS = (*base.TARGETS, (SCOPES[1], 'ci'), (SCOPES[1], 'automation'))
MODE = 'ci-bounded-heavy-v1-tmpfs-6gi'


def plan(**changes):
    return base.plan(TARGETS)._replace(
        low_share_ci_scopes=SCOPES, bounded_heavy_ci_scopes=SCOPES,
        bounded_heavy_image=DERIVATIVE)._replace(**changes)


def render(config=None):
    return base.render(plan() if config is None else config, ROWS)


class BoundedHeavyTests(unittest.TestCase):
    def test_only_selected_ci_image_memory_and_mode_change(self):
        original = render(plan(bounded_heavy_ci_scopes=(), bounded_heavy_image=None))
        candidate = render()
        self.assertEqual({key for key in candidate if candidate[key] != original[key]},
                         {'foundations.json', *(arc.name(scope, 'ci') + '.json' for scope in SCOPES)})
        for scope in SCOPES:
            key = arc.name(scope, 'ci') + '.json'
            runner = candidate[key]['template']['spec']['containers'][0]
            self.assertEqual(runner['image'], DERIVATIVE)
            self.assertEqual(runner['resources'], {
                'requests': {'cpu': '100m', 'memory': '9Gi', 'ephemeral-storage': '2Gi'},
                'limits': {'cpu': '2', 'memory': '12Gi', 'ephemeral-storage': '30Gi'}})
            env = next(e for e in runner['env'] if e['name'] == 'RUNNER_PRIVATE_DOCKER_STORAGE')
            self.assertEqual(env['value'], MODE)
            runner['image'] = base.NUC_IMAGE
            runner['resources']['requests']['memory'] = '13Gi'
            runner['resources']['limits']['memory'] = '16Gi'
            env['value'] = 'ci-heavy-tmpfs-6gi'
            self.assertEqual(candidate[key], original[key])
        # Runtime/namespace quotas and networking are unaffected; only two policy clauses change.
        old = base.policy(original, '-workers')
        new = base.policy(candidate, '-workers')
        changed = [i for i, (a, b) in enumerate(zip(old['spec']['validations'], new['spec']['validations'])) if a != b]
        self.assertEqual(len(changed), 2)
        for i in changed:
            new['spec']['validations'][i] = old['spec']['validations'][i]
        self.assertEqual(candidate['foundations.json'], original['foundations.json'])

    def test_partial_duplicate_untyped_or_wrong_target_selection_fails_closed(self):
        invalid = [
            {'bounded_heavy_image': None}, {'bounded_heavy_ci_scopes': ()},
            {'bounded_heavy_image': base.NUC_IMAGE}, {'bounded_heavy_image': 'image:latest'},
            {'bounded_heavy_image': True}, {'bounded_heavy_ci_scopes': list(SCOPES)},
            {'bounded_heavy_ci_scopes': (SCOPES[0], SCOPES[0])},
            {'bounded_heavy_ci_scopes': ('example/missing',)},
            {'bounded_heavy_ci_scopes': ('example/standard',)},
            {'bounded_heavy_ci_scopes': ('bad scope',)},
            {'light_ci_scopes': (SCOPES[0],)},
            {'targets': tuple(arc.NucKvmTarget(*t) for t in TARGETS if t != (SCOPES[0], 'ci'))},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                render(plan(**changes))
        public = copy.deepcopy(ROWS)
        public[1]['visibility'] = 'public'
        with self.assertRaises(ValueError):
            base.render(plan(), public)

    def test_json_requires_complete_nonempty_explicit_selection(self):
        data = {'targets': [dict(scope=s, profile=p) for s, p in TARGETS],
                'image': base.NUC_IMAGE, 'node_ip': base.NUC_IP,
                'low_share_ci_scopes': list(SCOPES),
                'bounded_heavy_ci_scopes': list(SCOPES), 'bounded_heavy_image': DERIVATIVE}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'plan.json'
            path.write_text(json.dumps(data))
            self.assertEqual(render(arc.load_nuc_kvm_plan(path)), render())
            cases = [dict(data, bounded_heavy_ci_scopes=[]), dict(data, bounded_heavy_image=None),
                     dict(data, bounded_heavy_ci_scopes='example/heavy'), dict(data, unknown=True)]
            for missing in ('bounded_heavy_ci_scopes', 'bounded_heavy_image'):
                case = dict(data); del case[missing]; cases.append(case)
            for case in cases:
                with self.subTest(case=case), self.assertRaises(ValueError):
                    path.write_text(json.dumps(case))
                    arc.load_nuc_kvm_plan(path)

    def test_image_mode_resource_swaps_and_missing_identity_are_rejected(self):
        docs = render()
        policy = base.policy(docs, '-workers')['spec']['validations']
        image_rule = next(r['expression'] for r in policy if r['message'] == 'Only the reviewed pinned worker image is allowed')
        tuple_rule = next(r['expression'] for r in policy if r['message'].startswith('NUC workers require one exact'))

        def context(target):
            pod = copy.deepcopy(base.worker(docs, target)['template'])
            namespace = 'arc-private-' + target[1]
            pod['metadata']['namespace'] = namespace
            return {'request': {'namespace': namespace}, 'object': pod}

        def accepted(value):
            return all(base.identity_expression(rule, value) for rule in (image_rule, tuple_rule))

        for target in TARGETS:
            value = context(target)
            self.assertTrue(accepted(value))
            value['object']['spec']['containers'][0]['image'] = (
                base.NUC_IMAGE if target[0] in SCOPES and target[1] == 'ci' else DERIVATIVE)
            self.assertFalse(accepted(value), target)
        for field, value in [('requests', '13Gi'), ('requests', '8Gi'), ('limits', '16Gi'), ('limits', '6Gi')]:
            ctx = context((SCOPES[0], 'ci'))
            ctx['object']['spec']['containers'][0]['resources'][field]['memory'] = value
            self.assertFalse(accepted(ctx))
        for mode in ['ci-heavy-tmpfs-6gi', 'automation-tmpfs-3gi', 'ci-tmpfs-2gi', 'disk']:
            ctx = context((SCOPES[0], 'ci'))
            next(e for e in ctx['object']['spec']['containers'][0]['env']
                 if e['name'] == 'RUNNER_PRIVATE_DOCKER_STORAGE')['value'] = mode
            self.assertFalse(accepted(ctx))
        for key in (arc.SCOPE_LABEL, arc.PROFILE_LABEL):
            ctx = context((SCOPES[0], 'ci')); del ctx['object']['metadata']['labels'][key]
            self.assertFalse(accepted(ctx))
        ctx = context((SCOPES[0], 'ci')); ctx['request']['namespace'] = 'arc-private-automation'
        self.assertFalse(accepted(ctx))

    def test_absent_opt_in_preserves_existing_nuc_bytes(self):
        docs = base.render(base.plan())
        # Reviewed baseline generated from the unchanged parent renderer.
        payload = json.dumps(docs, sort_keys=True, separators=(',', ':')).encode()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), '1de79e20b174ccf3f8f69ab286a50abda629bb84db7deb143baa915de4748acb')


class BoundedHeavyStorageTests(unittest.TestCase):
    SETUP = '''
RUNNER_PRIVATE_DOCKER_STORAGE=ci-bounded-heavy-v1-tmpfs-6gi
RUNNER_PRIVATE_MEMORY_REQUEST=9663676416
RUNNER_PRIVATE_MEMORY_LIMIT=12884901888
stat() { printf 'tmpfs 4096 1572864 524288'; }
'''

    def test_exact_mount_and_memory_contract(self):
        result, mounted = tmpfs.PrivateTmpfsTests().gate(self.SETUP)
        self.assertEqual((result.returncode, result.stdout), (0, 'overlayfs'), result.stderr)
        self.assertIn('size=6442450944,nr_inodes=524288', mounted)
        self.assertIn('memory_request=9663676416 memory_limit=12884901888', result.stderr)

    def test_mismatched_memory_or_profile_fails_before_mount(self):
        for mismatch in ['RUNNER_PRIVATE_MEMORY_REQUEST=13958643712',
                         'RUNNER_PRIVATE_MEMORY_REQUEST=8589934592',
                         'RUNNER_PRIVATE_MEMORY_LIMIT=17179869184',
                         'RUNNER_PRIVATE_MEMORY_LIMIT=6442450944',
                         'RUNNER_PRIVATE_NAMESPACE=arc-private-automation',
                         'RUNNER_PRIVATE_PROFILE=automation', 'RUNNER_PRIVATE_PROFILE=release',
                         'RUNNER_DOCKER_STORAGE_DRIVER=vfs']:
            with self.subTest(mismatch=mismatch):
                result, mounted = tmpfs.PrivateTmpfsTests().gate(self.SETUP + mismatch)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(mounted)

    def test_failed_mount_or_wrong_active_size_never_emits_backend(self):
        for failure in ['mount() { return 1; }', "stat() { printf 'tmpfs 4096 524288 262144'; }"]:
            result, _ = tmpfs.PrivateTmpfsTests().gate(self.SETUP + failure)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')
            self.assertNotIn('PRIVATE_DOCKER_TMPFS_READY', result.stderr)


if __name__ == '__main__':
    unittest.main()
