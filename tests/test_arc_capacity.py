"""Resource arithmetic and fail-closed boundaries, using synthetic identities only."""
import copy
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import io
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PATH = Path(__file__).parents[1] / 'deploy/arc-private/capacity_preflight.py'
SPEC = importlib.util.spec_from_file_location('arc_capacity', PATH)
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def obj(kind, name, **kwargs):
    return {'kind': kind, 'apiVersion': 'node.k8s.io/v1' if kind == 'RuntimeClass' else 'v1',
            'metadata': {'name': name, 'namespace': 'private-ci', 'uid': name, 'resourceVersion': '17'}, **kwargs}


def listing(kind, items=None):
    return {'kind': kind + 'List', 'apiVersion': 'node.k8s.io/v1' if kind == 'RuntimeClass' else 'v1',
            'metadata': {'resourceVersion': '42'}, 'items': items or []}


def container(name='runner', cpu='100m'):
    return {'name': name, 'resources': {
        'requests': {'cpu': cpu, 'memory': '3Gi', 'ephemeral-storage': '1Gi'},
        'limits': {'cpu': '4', 'memory': '6Gi', 'ephemeral-storage': '20Gi'}}}


def pod(name, cpu='100m', assigned=False, phase='Pending'):
    spec = {'containers': [container(cpu=cpu)]}
    if assigned:
        spec['nodeName'] = 'worker-node'
    return obj('Pod', name, spec=spec, status={'phase': phase})


def candidate(cpu='100m'):
    return {'kind': 'Pod', 'apiVersion': 'v1', 'metadata': {'namespace': 'private-ci'},
            'spec': {'containers': [container(cpu=cpu)], 'runtimeClassName': 'test-sandbox'}}


def snapshot():
    background = pod('background', '3260m', assigned=True, phase='Running')
    background['metadata']['namespace'] = 'system'
    return {'schema': 1, 'coverage': C.COVERAGE, 'namespace': 'private-ci',
            'started_at': (NOW - timedelta(seconds=2)).isoformat(), 'finished_at': NOW.isoformat(),
            'server_version': {'gitVersion': 'v1.31.4+k3s1'},
            'node': obj('Node', 'worker-node', status={'allocatable': {
                'cpu': '3500m', 'memory': '32Gi', 'ephemeral-storage': '100Gi', 'pods': '110'}}),
            'pods': listing('Pod', [background]), 'quotas': listing('ResourceQuota'),
            'limitranges': listing('LimitRange'),
            'runtimeclasses': listing('RuntimeClass', [obj('RuntimeClass', 'test-sandbox',
                overhead={'podFixed': {'cpu': '100m', 'memory': '128Mi'}})])}


def quota(name='budget', hard=None, used=None, **spec):
    hard = hard or {'pods': '2', 'limits.ephemeral-storage': '40Gi'}
    return obj('ResourceQuota', name, spec={'hard': hard, **spec},
               status={'hard': copy.deepcopy(hard), 'used': used or {key: '0' for key in hard}})


class CapacityTests(unittest.TestCase):
    def check(self, data=None, incoming=None):
        return C.analyze(data or snapshot(), incoming or candidate(), now=NOW)

    def test_observed_700_millicores_cannot_fit_240(self):
        result = self.check(incoming=candidate('600m'))
        self.assertEqual(result['verdict'], 'blocked')
        self.assertEqual(result['node_resources']['cpu'], {
            'allocatable': 3500, 'assigned': 3260, 'available': 240, 'candidate': 700})
        self.assertIn('700, only 240', result['blockers'][0])

    def test_100_plus_100_fits_without_claiming_scheduler_qualification(self):
        result = self.check()
        self.assertEqual(result['verdict'], 'resource_headroom_passes')
        self.assertEqual(result['candidate_requests']['cpu'], 200)
        self.assertIn('not scheduler', result['boundaries'][0])

    def test_pending_pods_starve_quota_without_consuming_node_requests(self):
        data = snapshot()
        data['pods']['items'] += [pod('pending-a', '600m'), pod('pending-b', '600m')]
        data['quotas']['items'] = [quota(used={'pods': '2', 'limits.ephemeral-storage': '40Gi'})]
        result = self.check(data)
        self.assertEqual(result['verdict'], 'blocked')
        self.assertEqual(result['node_resources']['cpu']['available'], 240)
        self.assertEqual(len(result['pending_reservations']), 2)
        self.assertEqual(len(result['blockers']), 2)

    def test_completed_objects_count_only_for_count_pods(self):
        data = snapshot()
        data['pods']['items'] += [pod('done', phase='Succeeded'), pod('failed', phase='Failed')]
        data['quotas']['items'] = [quota(hard={'pods': '1', 'count/pods': '2'}, used={'pods': '0', 'count/pods': '2'})]
        result = self.check(data)
        self.assertEqual(result['verdict'], 'blocked')
        self.assertEqual(result['blockers'], ['Quota budget: count/pods needs 1, only 0 available'])

    def test_candidate_overhead_once_and_mismatch_rejected(self):
        incoming = candidate()
        incoming['spec']['overhead'] = {'cpu': '100m', 'memory': '128Mi'}
        self.assertEqual(self.check(incoming=incoming)['candidate_requests']['cpu'], 200)
        incoming['spec']['overhead']['cpu'] = '200m'
        self.assertEqual(self.check(incoming=incoming)['verdict'], 'unable_to_qualify')

    def test_admitted_overhead_authoritative_even_when_class_changes(self):
        data = snapshot()
        existing = data['pods']['items'][0]
        existing['spec'].update(runtimeClassName='test-sandbox', overhead={'cpu': '10m'})
        self.assertEqual(self.check(data)['node_resources']['cpu']['assigned'], 3270)
        del existing['spec']['overhead']
        self.assertEqual(self.check(data)['node_resources']['cpu']['assigned'], 3260)

    def test_overhead_only_added_to_nonzero_limits(self):
        incoming = candidate()
        incoming['spec']['containers'][0]['resources']['limits']['memory'] = '0'
        incoming['spec']['containers'][0]['resources']['requests']['memory'] = '0'
        result = self.check(incoming=incoming)
        self.assertEqual(result['candidate_limits']['memory'], 0)
        self.assertEqual(result['candidate_requests']['memory'], 128 * 1024 ** 2)

    def test_ordinary_init_uses_maximum_not_sum(self):
        incoming = candidate()
        incoming['spec']['initContainers'] = [container('init-a', '150m'), container('init-b', '180m')]
        self.assertEqual(self.check(incoming=incoming)['candidate_requests']['cpu'], 280)

    def test_restartable_init_overlap_uses_ordered_peak(self):
        incoming = candidate()
        sidecar = container('sidecar', '100m')
        sidecar['restartPolicy'] = 'Always'
        incoming['spec']['initContainers'] = [container('early-init', '180m'), sidecar, container('later-init', '200m')]
        result = self.check(incoming=incoming)
        self.assertEqual(result['candidate_requests']['cpu'], 400)
        self.assertEqual(result['candidate_limits']['cpu'], 8100)

    def test_every_overlapping_quota_applies(self):
        data = snapshot()
        data['quotas']['items'] = [quota('large', {'pods': '10'}), quota('small', {'pods': '0'})]
        result = self.check(data)
        self.assertEqual(result['verdict'], 'blocked')
        self.assertEqual(len(result['quotas']), 2)
        self.assertIn('Quota small', result['blockers'][0])

    def test_scoped_and_unknown_other_quotas_are_not_ignored(self):
        for item in [quota(spec='x', scopes=['BestEffort']), quota(scopeSelector={'matchExpressions': []}),
                     quota(hard={'requests.storage': '1Gi'}), quota(hard={'requests.example.com/gpu': '1'})]:
            with self.subTest(item=item):
                data = snapshot(); data['quotas']['items'] = [item]
                self.assertEqual(self.check(data)['verdict'], 'unable_to_qualify')

    def test_quota_used_convergence_and_observed_pod_lower_bound(self):
        for mode in ('hard', 'missing-used', 'undercount'):
            with self.subTest(mode=mode):
                data = snapshot(); q = quota(hard={'pods': '2'})
                data['quotas']['items'] = [q]
                if mode == 'hard': q['status']['hard']['pods'] = '3'
                elif mode == 'missing-used': q['status']['used'] = {}
                else: data['pods']['items'].append(pod('pending'))
                self.assertEqual(self.check(data)['verdict'], 'unable_to_qualify')

    def test_invalid_quantities_fail_closed(self):
        for value in ('banana', '-1', 'NaN', '1e9999', '0.5m', '0.10000000000000000000000000000001', None, True):
            with self.subTest(value=value):
                data = snapshot(); data['quotas']['items'] = [quota(hard={'requests.cpu': value})]
                self.assertEqual(self.check(data)['verdict'], 'unable_to_qualify')

    def test_stale_incomplete_or_future_snapshots(self):
        mutations = [lambda d: d.pop('pods'), lambda d: d.pop('server_version'),
                     lambda d: d['pods']['metadata'].update(continue_='ignored', **{'continue': 'next'}),
                     lambda d: d.update(finished_at=(NOW - timedelta(seconds=61)).isoformat(), started_at=(NOW - timedelta(seconds=62)).isoformat()),
                     lambda d: d.update(finished_at=(NOW + timedelta(seconds=1)).isoformat()),
                     lambda d: d.update(started_at=(NOW - timedelta(seconds=31)).isoformat())]
        for mutate in mutations:
            data = snapshot(); mutate(data)
            self.assertEqual(self.check(data)['verdict'], 'unable_to_qualify')

    def test_pod_level_resize_claims_and_unknown_dimensions_fail_closed(self):
        for mode in ('pod-level', 'resize', 'allocated', 'claims', 'gpu'):
            incoming = candidate()
            if mode == 'pod-level': incoming['spec']['resources'] = {'requests': {'cpu': '1'}}
            elif mode == 'resize': incoming['spec']['containers'][0]['resizePolicy'] = [{'resourceName': 'cpu'}]
            elif mode == 'allocated':
                data = snapshot(); data['pods']['items'][0]['status']['containerStatuses'] = [{'allocatedResources': {'cpu': '1'}}]
                self.assertEqual(self.check(data)['verdict'], 'unable_to_qualify'); continue
            elif mode == 'claims': incoming['spec']['resourceClaims'] = [{'name': 'claim'}]
            else: incoming['spec']['containers'][0]['resources']['requests']['example.com/gpu'] = '1'
            self.assertEqual(self.check(incoming=incoming)['verdict'], 'unable_to_qualify')

    def test_limit_range_constraints_and_defaulting(self):
        for rule, verdict in [({'min': {'cpu': '150m'}}, 'blocked'),
                              ({'max': {'memory': '5Gi'}}, 'blocked'),
                              ({'maxLimitRequestRatio': {'cpu': '10'}}, 'blocked'),
                              ({'defaultRequest': {'cpu': '500m'}}, 'resource_headroom_passes'),
                              ({'type': 'Pod'}, 'unable_to_qualify')]:
            data = snapshot()
            data['limitranges']['items'] = [obj('LimitRange', 'range', spec={'limits': [{'type': 'Container', **rule}]})]
            self.assertEqual(self.check(data)['verdict'], verdict)
        data = snapshot(); incoming = candidate()
        del incoming['spec']['containers'][0]['resources']['limits']['ephemeral-storage']
        data['limitranges']['items'] = [obj('LimitRange', 'range', spec={'limits': [{'type': 'Container', 'default': {'ephemeral-storage': '1Gi'}}]})]
        self.assertEqual(self.check(data, incoming)['verdict'], 'unable_to_qualify')

    def test_projection_removes_secret_values_but_preserves_accounting(self):
        source = pod('private-name')
        source['metadata']['annotations'] = {'password': 'never-copy-me'}
        source['spec']['containers'][0].update(env=[{'name': 'TOKEN', 'value': 'never-copy-me'}], command=['never-copy-me'])
        projected = C.project_object(source)
        self.assertNotIn('never-copy-me', json.dumps(projected))
        self.assertEqual(C.pod_resources(source, {}), C.pod_resources(projected, {}))

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(C.Unable): C.parse_json(b'{"a": 1, "a": 2}')
        with self.assertRaises(C.Unable): C.parse_json(b'{"a": NaN}')

    def test_valid_quantity_forms_are_exact(self):
        self.assertEqual(C.amount('cpu', '0.1'), 100)
        self.assertEqual(C.amount('cpu', '1e-1'), 100)
        self.assertEqual(C.amount('memory', '1.5Gi'), 1610612736)
        self.assertEqual(C.amount('ephemeral-storage', '2G'), 2000000000)

    def test_bounded_reader_rejects_nonzero_partial_and_deadline(self):
        for script in ('import sys; print("{}"); sys.exit(1)', 'print("not json")'):
            with self.assertRaises((C.Unable, ValueError)):
                C.bounded_get([sys.executable, '-c', script], time.monotonic() + 2)
        with self.assertRaises(C.Unable):
            C.bounded_get([sys.executable, '-c', 'import time; time.sleep(1)'], time.monotonic() + .05)

    def test_both_reader_pipes_are_size_bounded(self):
        with patch.object(C, 'MAX_BYTES', 1024):
            with self.assertRaisesRegex(C.Unable, 'output limit'):
                C.bounded_get([sys.executable, '-c', 'print("x"*2048)'], time.monotonic() + 2)
        with self.assertRaisesRegex(C.Unable, 'output limit'):
            C.bounded_get([sys.executable, '-c', 'import sys; sys.stderr.write("x"*1100000)'], time.monotonic() + 2)

    def test_capture_missing_list_is_not_accepted(self):
        with patch.object(C, 'bounded_get', return_value={}):
            with self.assertRaises(C.Unable):
                C.capture('explicit-context', 'worker-node', 'private-ci')

    def test_capture_file_is_private_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.json'
            argv = ['capacity_preflight.py', 'capture', '--context', 'test', '--node', 'worker-node',
                    '--namespace', 'private-ci', '--output', str(path)]
            with patch.object(sys, 'argv', argv), patch.object(C, 'capture', return_value=snapshot()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(C.main(), 0)
                original = path.read_bytes()
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(C.main(), 2)
                self.assertEqual(path.read_bytes(), original)

    def test_file_read_itself_is_bounded_without_relying_on_stat_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'growing.json'
            path.write_bytes(b' ' * 33)
            with patch.object(C, 'MAX_BYTES', 32):
                with self.assertRaisesRegex(C.Unable, 'exceeds'):
                    C.load_json(path)
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('Unbounded read forbidden')):
                path.write_text('{}')
                self.assertEqual(C.load_json(path), {})

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO check')
    def test_fifo_is_rejected_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fifo'
            os.mkfifo(path)
            with self.assertRaisesRegex(C.Unable, 'regular file'):
                C.load_json(path)

    def test_deep_json_returns_unable_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'deep.json'
            path.write_text('[' * 2000 + '0' + ']' * 2000)
            argv = ['capacity_preflight.py', 'check', '--snapshot', str(path), '--candidate', str(path)]
            output = io.StringIO()
            with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(output):
                self.assertEqual(C.main(), 2)
            self.assertEqual(json.loads(output.getvalue())['verdict'], 'unable_to_qualify')

    def test_capture_uses_only_get_and_one_shared_deadline(self):
        responses = snapshot()
        calls = []
        def fake(command, deadline):
            calls.append((command, deadline))
            key = {'/version': 'server_version', '/api/v1/nodes/worker-node': 'node', '/api/v1/pods': 'pods',
                   '/api/v1/namespaces/private-ci/resourcequotas': 'quotas',
                   '/api/v1/namespaces/private-ci/limitranges': 'limitranges',
                   '/apis/node.k8s.io/v1/runtimeclasses': 'runtimeclasses'}
            self.assertEqual(command[5], '--raw')
            return responses[key[command[6]]]
        with patch.object(C, 'bounded_get', side_effect=fake):
            C.capture('explicit-context', 'worker-node', 'private-ci')
        self.assertEqual(len(calls), 6)
        self.assertEqual(len({deadline for _, deadline in calls}), 1)
        self.assertTrue(all(command[4] == 'get' for command, _ in calls))

    def test_raw_typed_items_inherit_type_but_generic_lists_cannot(self):
        raw = listing('Pod', [pod('one')])
        del raw['items'][0]['kind']; del raw['items'][0]['apiVersion']
        result = C.project_object(raw)
        self.assertEqual(result['metadata']['resourceVersion'], '42')
        self.assertEqual(result['items'][0]['kind'], 'Pod')
        self.assertEqual(result['items'][0]['apiVersion'], 'v1')
        raw['kind'] = 'List'
        with self.assertRaises(C.Unable): C.project_object(raw)

    def test_route_components_cannot_inject_an_api_path(self):
        with patch.object(C, 'bounded_get', side_effect=AssertionError('No request expected')):
            for node, ns in [('nuc/../pods', 'private-ci'), ('nuc', '../secrets')]:
                with self.assertRaises(C.Unable): C.capture('test', node, ns)

    def test_native_windows_is_explicitly_unsupported(self):
        path = Path('unused')
        with patch.object(C.os, 'name', 'nt'):
            with self.assertRaisesRegex(C.Unable, 'native Windows'):
                C.load_json(path)

    def test_capture_rejects_malformed_json_shapes_explicitly(self):
        for value in (None, [], True):
            with patch.object(C, 'bounded_get', return_value=value):
                with self.assertRaisesRegex(C.Unable, 'response must be an object'):
                    C.capture('test', 'worker-node', 'private-ci')
        for field, value in [('items', None), ('items', [None]), ('metadata', None)]:
            data = snapshot(); data['pods'][field] = value
            responses = [data[k] for k in ('server_version', 'node', 'pods', 'quotas', 'limitranges', 'runtimeclasses')]
            with patch.object(C, 'bounded_get', side_effect=responses):
                with self.assertRaises(C.Unable):
                    C.capture('test', 'worker-node', 'private-ci')
        for field in ('spec', 'status', 'metadata'):
            data = pod('bad'); data[field] = None
            with self.assertRaises(C.Unable): C.project_object(data)


if __name__ == '__main__':
    unittest.main()
