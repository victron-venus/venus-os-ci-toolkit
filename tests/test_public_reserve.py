"""Public workers must be fenced before admission, without affecting private pools."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('node_guard', ROOT / 'deploy/runner-reserve/node_guard.py')
guard_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard_module)
WORKER = {'ip': '10.42.3.250', 'interface': 'vethabc123', 'uid': 'pod-1', 'mac': '02:42:0a:2a:03:fa'}


class NetworkAdmissionTests(unittest.TestCase):
    def test_fence_checks_l2_and_source_identity_before_service_exceptions(self):
        rules = guard_module.rules([WORKER], '10.66.10.2', '10.43.0.10', ('129.159.41.15/32',), exists=True)
        self.assertTrue(rules.startswith('delete table bridge victron_ci_reserve\n'))
        for prefix in ('ether saddr !=', 'ether type != ip', 'ip saddr !='):
            self.assertLess(rules.index(prefix), rules.index('tcp dport 19999'))
        self.assertLess(rules.index('arp saddr ip !='), rules.index('ether type arp counter accept'))
        self.assertLess(rules.index('udp dport 53'), rules.index('10.0.0.0/8'))
        self.assertIn('169.254.0.0/16', rules)
        self.assertIn('129.159.41.15/32', rules)
        self.assertTrue(rules.endswith('iifname "vethabc123" counter drop\n}\n}\n'))

    def test_malformed_metadata_cannot_inject_firewall_commands(self):
        for key, value in [('ip', '10.2.3.4; accept'), ('interface', 'vethx" accept'),
                           ('mac', 'aa:bb:cc:dd:ee:ff\naccept')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                guard_module.rules([{**WORKER, key: value}], '10.66.10.2', '10.43.0.10')

    def test_no_workers_leaves_private_traffic_untouched(self):
        self.assertNotIn('drop', guard_module.rules([], '10.66.10.2', '10.43.0.10'))

    def test_admission_is_revoked_when_atomic_firewall_update_fails(self):
        guard = guard_module.Guard({'node_ip': '10.66.10.2', 'dns_ip': '10.43.0.10'})
        with patch.object(guard_module, 'discover', return_value=[WORKER]), \
                patch.object(guard_module.subprocess, 'run') as run, \
                patch.object(guard_module, 'command') as command:
            run.return_value.returncode = 1
            guard.sync()
            self.assertEqual(guard.admitted, {'10.42.3.250': 'pod-1'})
            self.assertEqual(command.call_args.args, ('nft', '-f', '-'))
            command.side_effect = RuntimeError('nft failure')
            with self.assertRaises(RuntimeError):
                guard.sync()
            self.assertEqual(guard.admitted, {})

    def test_cni_cache_must_match_cri_identity_and_live_veth(self):
        pod = {'id': 'a' * 64, 'metadata': {'uid': 'pod-1'}}
        cache = {'kind': 'cniCacheV1', 'containerId': pod['id'], 'ifName': 'eth0', 'result': {
            'interfaces': [{'name': 'cni0'}, {'name': 'vethabc123', 'mac': '02:00:00:00:00:01'},
                           {'name': 'eth0', 'mac': WORKER['mac'], 'sandbox': '/var/run/netns/cni-123'}],
            'ips': [{'address': '10.42.3.250/24', 'interface': 2}]}}
        links = {'vethabc123': '02:00:00:00:00:01'}
        self.assertEqual(guard_module.worker_from_cache(pod, cache, links), WORKER)
        for changes in ({'containerId': 'b' * 64}, {'kind': 'unknown'}, {'ifName': 'lo'}):
            with self.assertRaises(ValueError):
                guard_module.worker_from_cache(pod, {**cache, **changes}, links)
        for wrong in ({}, {'vethabc123': '02:00:00:00:00:02'}):
            with self.assertRaises(ValueError):
                guard_module.worker_from_cache(pod, cache, wrong)
        wrong = json.loads(json.dumps(cache))
        wrong['result']['interfaces'][2]['sandbox'] = '/etc/private'
        with self.assertRaises(ValueError):
            guard_module.worker_from_cache(pod, wrong, links)
        for index in (-1, 99, True, '2'):
            wrong = json.loads(json.dumps(cache))
            wrong['result']['ips'][0]['interface'] = index
            with self.subTest(index=index), self.assertRaises(ValueError):
                guard_module.worker_from_cache(pod, wrong, links)

    def test_readiness_endpoint_bounds_connections_and_releases_failed_handlers(self):
        with guard_module.ReadinessServer(('127.0.0.1', 0), guard_module.handler(
                guard_module.Guard({}))) as server:
            for _ in range(16):
                self.assertTrue(server.slots.acquire(blocking=False))
            with patch.object(server, 'shutdown_request') as shutdown:
                server.process_request(object(), ('127.0.0.1', 1000))
                shutdown.assert_called_once()
            server.slots.release()
            with patch.object(guard_module.ThreadingHTTPServer, 'process_request_thread',
                              side_effect=RuntimeError('handler failed')):
                server.slots.acquire()
                with self.assertRaises(RuntimeError):
                    server.process_request_thread(object(), ('127.0.0.1', 1000))
            self.assertTrue(server.slots.acquire(blocking=False))

POOL_SPEC = importlib.util.spec_from_file_location('render_pool', ROOT / 'deploy/runner-reserve/render_pool.py')
pool_module = importlib.util.module_from_spec(POOL_SPEC)
POOL_SPEC.loader.exec_module(pool_module)


class StandbyPoolTests(unittest.TestCase):
    def setUp(self):
        self.inventory = json.loads((ROOT / 'deploy/runner-reserve/repositories.json').read_text())
        self.image = 'localhost/victron-reserve-worker@sha256:' + '1' * 64

    def values(self, scope='victron-venus', profile='ci', image=None):
        return pool_module.pool_values(scope, profile, image or self.image,
                                       'mp', '10.66.10.2', self.inventory)

    def test_standby_can_accept_jobs_without_keeping_idle_workers(self):
        values = self.values()
        self.assertEqual(values['minRunners'], 0)
        self.assertGreater(values['maxRunners'], 0)
        self.assertEqual(self.inventory['default_mode'], 'github')
        self.assertEqual(values['runnerGroup'], 'victron-public-reserve-ci')
        self.assertNotIn('runnerGroup', self.values('4alvit/mcp-venus-os'))
        self.assertNotEqual(values['githubConfigSecret'], self.values('4alvit/mcp-venus-os')['githubConfigSecret'])

    def test_reject_private_unknown_scopes_and_unpinned_worker_images(self):
        for scope, profile, image in [('open-ott-play/private', 'ci', self.image),
                                      ('4alvit', 'ci', self.image),
                                      ('victron-venus', 'unknown', self.image),
                                      ('victron-venus', 'ci', 'ubuntu:latest')]:
            with self.subTest(scope=scope, profile=profile), self.assertRaises(ValueError):
                self.values(scope, profile, image)

    def test_workers_have_no_host_mounts_or_api_credentials(self):
        for profile in self.inventory['profiles']:
            values = self.values(profile=profile)
            spec = values['template']['spec']
            self.assertEqual(spec['runtimeClassName'], 'victron-reserve')
            self.assertFalse(spec['automountServiceAccountToken'])
            self.assertNotIn('volumes', spec)
            self.assertNotIn('initContainers', spec)
            self.assertEqual(spec['containers'][0]['image'], self.image)
            self.assertEqual(spec['containers'][0]['command'], ['/opt/victron-ci-reserve/start-runner.sh'])

    def test_foundations_fail_closed_and_preserve_three_profile_boundaries(self):
        docs = pool_module.foundations(self.inventory['profiles'], self.image, '10.66.10.2')['items']
        policies = [d for d in docs if d['kind'] == 'NetworkPolicy']
        self.assertEqual(len(policies), 3)
        for policy in policies:
            self.assertEqual(policy['spec']['ingress'], [])
            public = policy['spec']['egress'][-1]
            self.assertIn('169.254.0.0/16', public['to'][0]['ipBlock']['except'])
            self.assertEqual([p['port'] for p in public['ports']], [80, 443])
        admission = next(d for d in docs if d['kind'] == 'ValidatingAdmissionPolicy')
        self.assertEqual(admission['spec']['failurePolicy'], 'Fail')
        self.assertIn(self.image, json.dumps(admission))
        binding = next(d for d in docs if d['kind'] == 'ValidatingAdmissionPolicyBinding')
        self.assertEqual(binding['spec']['validationActions'], ['Deny'])
