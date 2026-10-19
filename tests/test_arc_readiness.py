"""ARC snapshot reporting must not confuse zero workers with missing scale sets."""

import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('arc_readiness', Path(__file__).resolve().parents[1] / 'scripts/arc_readiness.py')
arc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(arc)
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
STAMP = NOW.isoformat()


def fixtures():
    labels = {arc.PREFIX + 'scale-set-name': 'example-ci', arc.PREFIX + 'scale-set-namespace': 'arc-ci'}
    pool = {'metadata': {'name': 'example-ci', 'namespace': 'arc-ci', 'uid': 'pool-uid', 'generation': 2},
            'spec': {'githubConfigUrl': 'https://github.com/example/project', 'runnerScaleSetName': 'example-ci',
                     'minRunners': 0, 'maxRunners': 1}, 'status': {'observedGeneration': 2}}
    listener = {'metadata': {'name': 'example-listener', 'namespace': 'arc-system', 'uid': 'listener-uid', 'labels': labels},
                'spec': {'githubConfigUrl': pool['spec']['githubConfigUrl'], 'runnerScaleSetId': 7, 'maxRunners': 1}}
    pod = {'metadata': {'name': 'example-listener', 'namespace': 'arc-system', 'uid': 'pod-uid', 'labels': labels,
                        'ownerReferences': [{'kind': 'AutoscalingListener', 'uid': 'listener-uid', 'controller': True}]},
           'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}],
                      'containerStatuses': [{'name': 'listener', 'ready': True}]}}
    cluster = {'schema': 1, 'observed_at': STAMP, 'collections': {
        key: {'complete': True, 'scope': 'cluster-all', 'source_metadata': {}, 'items': items}
        for key, items in [('asrs', [pool]), ('listeners', [listener]), ('pods', [pod])]}}
    repos = {'schema': 1, 'observed_at': STAMP, 'complete': True, 'repositories': [
        {'repository': 'example/project', 'mode': 'self-hosted', 'profiles': {'ci': ['example-ci']}, 'github_runners': []}]}
    return repos, cluster


class ArcReadinessTests(unittest.TestCase):
    def setUp(self):
        self.repos, self.cluster = fixtures()
        self.pool = self.cluster['collections']['asrs']['items'][0]
        self.listener = self.cluster['collections']['listeners']['items'][0]
        self.pod = self.cluster['collections']['pods']['items'][0]

    def result(self):
        return arc.evaluate(self.repos, self.cluster, now=NOW)['results'][0]

    def test_empty_github_workers_does_not_mean_missing_registration(self):
        result = self.result()
        self.assertEqual(result['configuration'], 'matched')
        self.assertEqual(result['observed_state'], 'idle-scale-to-zero')
        self.assertEqual(result['pools'][0]['registration'], 'observed-scale-set-id')
        self.assertEqual(result['qualification'], 'not-assessed')

    def test_held_queued_demand_is_distinct_from_idle_or_missing(self):
        self.pool['spec']['maxRunners'] = 0
        self.repos['repositories'][0]['queued_jobs'] = {'ci': 3}
        result = self.result()
        self.assertEqual(result['observed_state'], 'held-with-queued-demand')
        self.assertEqual(result['pools'][0]['listener'], 'ready')
        self.assertEqual(result['configuration'], 'matched')

    def test_missing_pool_requires_complete_cluster_wide_inventory(self):
        self.cluster['collections']['asrs']['items'] = []
        self.assertEqual(self.result()['configuration'], 'absent')
        for change in [{'complete': False}, {'source_metadata': {'continue': 'next'}}, {'scope': 'some-namespace'}]:
            with self.subTest(change=change):
                old = copy.deepcopy(self.cluster['collections']['asrs'])
                self.cluster['collections']['asrs'].update(change)
                self.assertEqual(self.result()['configuration'], 'unknown')
                self.cluster['collections']['asrs'] = old

    def test_asrs_alone_is_not_registered_or_qualified(self):
        self.cluster['collections']['listeners']['items'] = []
        result = self.result()['pools'][0]
        self.assertEqual(result['listener'], 'absent')
        self.assertEqual(result['registration'], 'unknown')
        self.assertEqual(result['state'], 'unknown')

    def test_wrong_scope_and_multi_label_mismatch_are_not_matched(self):
        self.pool['spec']['githubConfigUrl'] = 'https://github.com/other/project'
        self.assertEqual(self.result()['configuration'], 'scope-mismatch')
        self.pool['spec']['githubConfigUrl'] = 'https://github.com/example/project'
        self.repos['repositories'][0]['profiles']['ci'] += ['self-hosted']
        self.assertEqual(self.result()['configuration'], 'absent')

    def test_custom_labels_and_org_scope_do_not_prove_group_access(self):
        self.pool['spec']['runnerScaleSetLabels'] = ['shared-ci']
        self.pool['spec']['githubConfigUrl'] = 'https://github.com/example'
        self.listener['spec']['githubConfigUrl'] = self.pool['spec']['githubConfigUrl']
        self.repos['repositories'][0]['profiles']['ci'] = ['shared-ci']
        result = self.result()['pools'][0]
        self.assertEqual(result['scope'], 'organization-access-unverified')
        self.assertEqual(result['state'], 'idle-scale-to-zero')

    def test_cross_namespace_listener_requires_both_labels_and_owner_uid(self):
        self.listener['metadata']['labels'] = {**self.listener['metadata']['labels'], arc.PREFIX + 'scale-set-namespace': 'foreign'}
        self.assertEqual(self.result()['pools'][0]['listener'], 'absent')
        self.listener['metadata']['labels'][arc.PREFIX + 'scale-set-namespace'] = 'arc-ci'
        self.pod['metadata']['ownerReferences'][0]['uid'] = 'old-listener'
        self.assertEqual(self.result()['pools'][0]['listener'], 'pod-absent')

    def test_deleting_or_unready_listener_cannot_be_idle(self):
        for change in ('delete', 'ready', 'condition'):
            with self.subTest(change=change):
                self.setUp()
                if change == 'delete':
                    self.listener['metadata']['deletionTimestamp'] = STAMP
                elif change == 'ready':
                    self.pod['status']['containerStatuses'][0]['ready'] = False
                else:
                    self.pod['status']['conditions'] = []
                self.assertEqual(self.result()['observed_state'], 'unknown')

    def test_partial_pod_inventory_does_not_fabricate_zero_workers(self):
        self.cluster['collections']['pods']['complete'] = False
        result = self.result()['pools'][0]
        self.assertEqual(result['listener'], 'observed-ready')
        self.assertIsNone(result['worker_pods'])
        self.assertEqual(result['state'], 'unknown')

    def test_namespace_prefix_scope_supports_zero_only_inside_coverage(self):
        pods = self.cluster['collections']['pods']
        pods.update(scope='selected', namespace_prefixes=['arc-'])
        self.assertEqual(self.result()['observed_state'], 'idle-scale-to-zero')
        pods['namespace_prefixes'] = ['arc-system']
        self.assertIsNone(self.result()['pools'][0]['worker_pods'])

    def test_unset_null_or_boolean_max_is_not_zero(self):
        for value in [None, False, -1]:
            with self.subTest(value=value):
                self.pool['spec']['maxRunners'] = value
                self.assertEqual(self.result()['pools'][0]['admission'], 'unknown')
        del self.pool['spec']['maxRunners']
        self.assertEqual(self.result()['pools'][0]['admission'], 'unspecified')

    def test_observed_worker_count_is_separate_from_listener(self):
        worker = copy.deepcopy(self.pod)
        worker['metadata'].update(name='example-worker', namespace='arc-ci', uid='worker-uid',
                                  ownerReferences=[{'kind': 'EphemeralRunner', 'uid': 'ephemeral-uid', 'controller': True}])
        self.cluster['collections']['pods']['items'].append(worker)
        result = self.result()['pools'][0]
        self.assertEqual(result['worker_pods'], 1)
        self.assertEqual(result['ready_worker_pods'], 1)
        self.assertEqual(result['state'], 'workers-observed')

    def test_selector_unknown_missing_invalid_and_duplicate_pools(self):
        for value, expected in [(None, 'unknown'), ([], 'missing'), ('example-ci', 'invalid'), (['x', 'x'], 'invalid')]:
            with self.subTest(value=value):
                self.repos['repositories'][0]['profiles']['ci'] = value
                self.assertEqual(self.result()['selector'], expected)
        self.repos['repositories'][0]['profiles']['ci'] = ['example-ci']
        self.cluster['collections']['asrs']['items'].append(copy.deepcopy(self.pool))
        self.assertEqual(self.result()['configuration'], 'ambiguous')

    def test_stale_or_future_evidence_is_marked_not_fresh(self):
        for stamp in ['2025-12-31T00:00:00Z', '2026-01-02T00:00:00Z']:
            self.cluster['observed_at'] = stamp
            self.assertFalse(self.result()['evidence_fresh'])
        self.assertEqual(self.result()['qualification'], 'not-assessed')

    def test_missing_controller_generation_is_not_idle(self):
        self.pool['status'] = {}
        self.assertEqual(self.result()['observed_state'], 'unknown')

    def test_missing_items_or_remaining_pages_cannot_prove_absence(self):
        pools = self.cluster['collections']['asrs']
        del pools['items']
        self.assertEqual(self.result()['configuration'], 'unknown')
        pools['items'] = []
        pools['source_metadata'] = {'remainingItemCount': 1}
        self.assertEqual(self.result()['configuration'], 'unknown')

    def test_partial_matching_census_does_not_prove_uniqueness(self):
        self.cluster['collections']['asrs']['complete'] = False
        self.assertEqual(self.result()['configuration'], 'unknown')
        self.assertEqual(self.result()['observed_state'], 'unknown')
        self.cluster['collections']['asrs']['complete'] = True
        self.cluster['collections']['listeners']['complete'] = False
        self.assertEqual(self.result()['pools'][0]['listener'], 'observed-ready')
        self.assertEqual(self.result()['observed_state'], 'unknown')

    def test_listener_limit_drift_cannot_prove_idle(self):
        self.listener['spec']['maxRunners'] = 0
        self.assertEqual(self.result()['pools'][0]['listener_limit_reconciliation'], 'pending')
        self.assertEqual(self.result()['observed_state'], 'unknown')

    def test_omitted_listener_minimum_does_not_match_nonzero_pool_minimum(self):
        self.pool['spec']['minRunners'] = 1
        result = self.result()['pools'][0]
        self.assertEqual(result['listener_limit_reconciliation'], 'pending')
        self.assertEqual(result['state'], 'unknown')
        self.listener['spec']['minRunners'] = 1
        self.assertEqual(self.result()['pools'][0]['listener_limit_reconciliation'], 'matched')

    def test_boolean_observed_generation_is_not_current(self):
        self.pool['metadata']['generation'] = 1
        self.pool['status']['observedGeneration'] = True
        self.assertFalse(self.result()['pools'][0]['controller_observed_generation'])

    def test_no_profiles_is_reported_without_inventing_requirements(self):
        self.repos['repositories'][0]['profiles'] = {}
        result = arc.evaluate(self.repos, self.cluster, now=NOW)
        self.assertEqual(result['profiles_not_declared'], ['example/project'])
        self.assertEqual(result['results'], [])

    def test_missing_listener_identity_does_not_establish_readiness(self):
        del self.listener['metadata']['uid']
        del self.pod['metadata']['ownerReferences'][0]['uid']
        result = self.result()['pools'][0]
        self.assertEqual(result['listener'], 'unknown')
        self.assertEqual(result['registration'], 'unknown')

    def test_duplicate_input_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'snapshot.json'
            path.write_text('{"schema":1,"schema":1}')
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                arc.load(path)
            path.write_text(json.dumps(self.repos))
            self.assertEqual(arc.load(path), self.repos)


if __name__ == '__main__':
    unittest.main()
