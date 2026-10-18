"""Public workers must be fenced before admission, without affecting private pools."""
import importlib.util
import json
import tempfile
import tomllib
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('node_guard', ROOT / 'deploy/runner-reserve/node_guard.py')
guard_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard_module)
WORKER = {'ip': '10.42.3.250', 'interface': 'vethabc123', 'uid': 'pod-1', 'mac': '02:42:0a:2a:03:fa'}


class NetworkAdmissionTests(unittest.TestCase):
    def test_guest_sudo_keeps_the_sandbox_runtime_boundary(self):
        config = tomllib.loads((ROOT / 'deploy/runner-reserve/runsc.toml').read_text())
        self.assertEqual(config['binary_name'], '/opt/victron-ci-reserve/gvisor-20260928/runsc')
        self.assertEqual(config['runsc_config'], {
            'platform': 'systrap', 'net-raw': 'true',
            'allow-packet-socket-write': 'true', 'allow-suid': 'true',
        })

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
                guard_module.Guard({})), tls_context=guard_module.ssl.SSLContext(
                    guard_module.ssl.PROTOCOL_TLS_SERVER)) as server:
            for _ in range(16):
                self.assertTrue(server.slots.acquire(blocking=False))
            with patch.object(server, 'shutdown_request') as shutdown:
                server.process_request(object(), ('127.0.0.1', 1000))
                shutdown.assert_called_once()
            server.slots.release()
            with patch.object(guard_module.ThreadingMixIn, 'process_request_thread',
                              side_effect=RuntimeError('handler failed')):
                server.slots.acquire()
                with self.assertRaises(RuntimeError):
                    server.process_request_thread(object(), ('127.0.0.1', 1000))
            self.assertTrue(server.slots.acquire(blocking=False))


class GuardReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.guard = guard_module.Guard({'node_ip': '10.66.10.2', 'dns_ip': '10.43.0.10'})
        self.objects = [{'table': {'family': 'bridge', 'name': guard_module.TABLE}},
                        {'chain': {'family': 'bridge', 'table': guard_module.TABLE,
                                   'name': 'ingress', 'type': 'filter', 'hook': 'prerouting',
                                   'prio': -350, 'policy': 'accept'}},
                        {'rule': {'family': 'bridge', 'table': guard_module.TABLE,
                                  'chain': 'ingress', 'expr': [{'counter': {}}, {'drop': None}]}}]
        self.echo = json.dumps({'nftables': [{'add': item} for item in self.objects]})
        self.clock = self.enterContext(patch.object(guard_module.time, 'monotonic', return_value=100))
        self.discovery = self.enterContext(patch.object(guard_module, 'discover', return_value=[WORKER]))
        self.validate = self.enterContext(patch.object(guard_module, 'validate_workers'))
        self.snapshot = self.enterContext(patch.object(guard_module, 'firewall_snapshot',
                                                      return_value=self.objects))
        self.command = self.enterContext(patch.object(guard_module, 'command', return_value=self.echo))

    def test_unchanged_workers_poll_cri_less_and_never_rewrite_firewall(self):
        self.guard.sync()
        for now in range(102, 162, 2):
            self.clock.return_value = now
            self.guard.sync()
        self.assertEqual(self.discovery.call_count, 4)
        self.assertEqual(self.command.call_count, 1)
        self.assertEqual(self.snapshot.call_count, 8)
        self.assertEqual(self.guard.admitted, {WORKER['ip']: WORKER['uid']})
        self.assertGreaterEqual(self.validate.call_count, 30)

    def test_removed_or_changed_table_is_repaired_without_cri_poll(self):
        self.guard.sync()
        for now, actual in [(110, None), (120, self.objects[:-1])]:
            self.clock.return_value = now
            self.snapshot.side_effect = [actual, self.objects]
            self.guard.sync()
            self.assertEqual(self.guard.admitted, {WORKER['ip']: WORKER['uid']})
        self.assertEqual(self.command.call_count, 3)
        self.assertEqual(self.discovery.call_count, 2)

    def test_failed_atomic_apply_revokes_admission(self):
        self.guard.sync()
        self.clock.return_value = 110
        self.snapshot.return_value = None
        self.command.side_effect = RuntimeError('nft failure')
        with self.assertRaises(RuntimeError):
            self.guard.sync()
        self.assertEqual(self.guard.admitted, {})
        self.assertEqual(self.guard.admitted_until, 0)

    def test_post_apply_drift_is_not_adopted_as_baseline(self):
        self.snapshot.side_effect = [None, self.objects[:-1]]
        with self.assertRaisesRegex(RuntimeError, 'changed during'):
            self.guard.sync()
        self.assertEqual(self.guard.admitted, {})
        self.assertIsNone(self.guard.firewall)

    def test_bad_echo_cannot_admit_workers(self):
        for echo in ['', '{}', '{"nftables": []}']:
            with self.subTest(echo=echo):
                self.command.return_value = echo
                with self.assertRaises((ValueError, KeyError)):
                    self.guard.sync()
                self.assertEqual(self.guard.admitted, {})

    def test_cri_failure_requires_fresh_discovery_before_readmission(self):
        self.guard.sync()
        self.clock.return_value = 116
        self.discovery.side_effect = RuntimeError('runtime unavailable')
        with self.assertRaises(RuntimeError):
            self.guard.sync()
        self.assertEqual(self.guard.admitted, {})
        self.discovery.side_effect = None
        self.clock.return_value = 118
        self.guard.sync()
        self.assertEqual(self.discovery.call_count, 3)
        self.assertEqual(self.guard.admitted, {WORKER['ip']: WORKER['uid']})

    def test_identity_loss_revokes_before_next_cri_or_firewall_check(self):
        self.guard.sync()
        self.clock.return_value = 102
        self.validate.side_effect = ValueError('veth reused')
        with self.assertRaises(ValueError):
            self.guard.sync()
        self.assertEqual(self.guard.admitted, {})
        self.assertEqual(self.command.call_count, 1)
        self.assertEqual(self.discovery.call_count, 1)
        self.assertEqual(self.guard.next_discovery, 0)

    def test_worker_replacement_and_removal_apply_before_admission(self):
        self.guard.sync()
        for now, workers in [(116, [{**WORKER, 'uid': 'pod-2'}]), (132, [])]:
            self.clock.return_value = now
            self.discovery.return_value = workers
            self.guard.sync()
            self.assertEqual(self.guard.admitted, {row['ip']: row['uid'] for row in workers})
        self.assertEqual(self.command.call_count, 3)

    def test_firewall_snapshot_failure_revokes_even_when_workers_unchanged(self):
        self.guard.sync()
        self.clock.return_value = 110
        self.snapshot.side_effect = ValueError('invalid JSON')
        with self.assertRaises(ValueError):
            self.guard.sync()
        self.assertEqual(self.guard.admitted, {})

    def test_fingerprints_ignore_only_volatile_fields(self):
        changed = json.loads(json.dumps(self.objects))
        changed[-1]['rule']['handle'] = 999
        changed[-1]['rule']['expr'][0]['counter'] = {'packets': 5, 'bytes': 300}
        self.assertEqual(guard_module.canonical_ruleset(json.dumps({'nftables': changed})), self.objects)
        changed[-1]['rule']['expr'][-1] = {'accept': None}
        self.assertNotEqual(guard_module.canonical_ruleset(json.dumps({'nftables': changed})), self.objects)
        self.assertNotEqual(guard_module.canonical_ruleset(
            json.dumps({'nftables': list(reversed(self.objects))})), self.objects)
        changed = json.loads(json.dumps(self.objects))
        changed[1]['chain']['prio'] = 0
        self.assertNotEqual(guard_module.canonical_ruleset(json.dumps({'nftables': changed})), self.objects)

    def test_nft_input_file_is_private_and_removed_even_on_failure(self):
        paths = []
        def inspect(*args):
            path = Path(args[-1])
            paths.append(path)
            self.assertEqual(path.read_text(), 'test rules')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            raise RuntimeError('nft failed')
        self.command.side_effect = inspect
        with self.assertRaises(RuntimeError):
            guard_module.apply_firewall('test rules')
        self.assertFalse(paths[0].exists())

    def test_invalid_intervals_are_rejected(self):
        for value in (True, 0, -1, float('nan'), float('inf'), '15', 301):
            with self.subTest(value=value), self.assertRaises(ValueError):
                guard_module.Guard({'discovery_interval_seconds': value})


class LocalIdentityTests(unittest.TestCase):
    def test_local_identity_rejects_reused_veth_and_changed_or_missing_cni(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cni, devices = root / 'cni', root / 'devices'
            cni.mkdir()
            device = devices / WORKER['interface']
            device.mkdir(parents=True)
            host_mac = '02:00:00:00:00:01'
            (device / 'address').write_text(host_mac)
            (device / 'ifindex').write_text('17')
            pod = {'id': 'a' * 64, 'metadata': {'uid': WORKER['uid']}}
            cache = {'kind': 'cniCacheV1', 'containerId': pod['id'], 'ifName': 'eth0', 'result': {
                'interfaces': [{'name': WORKER['interface'], 'mac': host_mac},
                               {'name': 'eth0', 'mac': WORKER['mac'], 'sandbox': '/run/netns/cni-123'}],
                'ips': [{'address': WORKER['ip'] + '/24', 'interface': 1}]}}
            path = cni / ('cbr0-' + pod['id'] + '-eth0')
            path.write_text(json.dumps(cache))
            with patch.object(guard_module, 'CNI_RESULTS', cni), \
                    patch.object(guard_module, 'NETWORK_DEVICES', devices), \
                    patch.object(guard_module, 'command') as command:
                worker = guard_module.cached_worker(pod)
                guard_module.validate_workers([worker])
                command.assert_not_called()
                (device / 'ifindex').write_text('18')
                with self.assertRaises(ValueError):
                    guard_module.validate_workers([worker])
                (device / 'ifindex').write_text('17')
                cache['containerId'] = 'b' * 64
                path.write_text(json.dumps(cache))
                with self.assertRaises(ValueError):
                    guard_module.validate_workers([worker])
                path.unlink()
                with self.assertRaises(OSError):
                    guard_module.validate_workers([worker])
                path.symlink_to(root / 'other')
                with self.assertRaises(ValueError):
                    guard_module.validate_workers([worker])

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
            context = spec['containers'][0]['securityContext']
            self.assertFalse(context['privileged'])
            capabilities = context['capabilities']
            self.assertFalse('ALL' in capabilities.get('add', []) and
                             'ALL' in capabilities.get('drop', []),
                             'containerd clears every bootstrap capability for add/drop ALL')

    def test_shared_routing_labels_do_not_collide_in_kubernetes(self):
        scopes = ['victron-venus'] + [r['name'] for r in self.inventory['repositories']
                                      if r['name'].startswith('4alvit/')]
        names = set()
        for scope in scopes:
            for profile, label in self.inventory['profiles'].items():
                values = self.values(scope, profile)
                self.assertNotIn(values['runnerScaleSetName'], names)
                self.assertLessEqual(len(values['runnerScaleSetName']), 45)
                names.add(values['runnerScaleSetName'])
                self.assertEqual(values['scaleSetLabels'], [label])
        self.assertEqual(len(names), 51)

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
        expressions = '\n'.join(v['expression'] for v in admission['spec']['validations'])
        self.assertIn('!has(e.valueFrom.secretKeyRef)', expressions)
        self.assertIn('!has(e.valueFrom.configMapKeyRef)', expressions)
        self.assertIn("e.name == 'ACTIONS_RUNNER_INPUT_JITCONFIG'", expressions)
        self.assertIn('e.valueFrom.secretKeyRef.name == object.metadata.name', expressions)
        self.assertIn("e.valueFrom.secretKeyRef.key == 'jitToken'", expressions)
        self.assertIn("o.kind == 'EphemeralRunner'", expressions)
        self.assertIn('!e.valueFrom.secretKeyRef.optional', expressions)
        self.assertIn("size(c.env.filter(e, e.name == 'ACTIONS_RUNNER_INPUT_JITCONFIG')) <= 1", expressions)
        binding = next(d for d in docs if d['kind'] == 'ValidatingAdmissionPolicyBinding')
        self.assertEqual(binding['spec']['validationActions'], ['Deny'])


class TLSAdmissionTests(unittest.TestCase):
    def test_real_https_requires_trusted_ca_matching_hostname_and_pod_identity(self):
        import ssl
        import socket
        import subprocess
        import tempfile
        import threading
        import urllib.error
        import urllib.request
        with tempfile.TemporaryDirectory() as directory:
            key, cert = [Path(directory) / name for name in ('key.pem', 'cert.pem')]
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-days', '1', '-keyout', str(key), '-out', str(cert),
                            '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                           capture_output=True, check=True)
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.minimum_version = ssl.TLSVersion.TLSv1_2
            server_context.load_cert_chain(cert, key)
            guard = guard_module.Guard({})
            guard.admitted = {'127.0.0.1': 'pod-1'}
            guard.admitted_until = float('inf')
            with guard_module.ReadinessServer(('127.0.0.1', 0), guard_module.handler(guard),
                                             tls_context=server_context) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = f'https://localhost:{server.server_address[1]}/ready/pod-1'
                    trusted = ssl.create_default_context(cafile=str(cert))
                    with urllib.request.urlopen(url, context=trusted, timeout=4) as result:
                        self.assertTrue(json.load(result)['admitted'])
                    for context, target in [(ssl.create_default_context(), url),
                                            (trusted, url.replace('localhost', '127.0.0.1'))]:
                        with self.assertRaises(urllib.error.URLError) as error:
                            urllib.request.urlopen(target, context=context, timeout=4)
                        self.assertIsInstance(error.exception.reason, ssl.SSLCertVerificationError)
                    guard.admitted_until = 0
                    with self.assertRaises(urllib.error.HTTPError) as expired:
                        urllib.request.urlopen(url, context=trusted, timeout=4)
                    self.assertEqual(expired.exception.code, 503)
                    expired.exception.close()
                    guard.admitted_until = float('inf')
                    wrong_url = url.replace('pod-1', 'wrong-pod')
                    with self.assertRaises(urllib.error.HTTPError) as denied:
                        urllib.request.urlopen(wrong_url, context=trusted, timeout=4)
                    self.assertEqual(denied.exception.code, 503)
                    denied.exception.close()
                    with socket.create_connection(server.server_address, timeout=4) as plain:
                        plain.sendall(b'GET /ready/pod-1 HTTP/1.1\r\nHost: localhost\r\n\r\n')
                        try:
                            reply = plain.recv(1024)
                        except ConnectionResetError:
                            reply = b''
                        self.assertNotIn(b'HTTP/', reply)
                        self.assertNotIn(b'admitted', reply)
                finally:
                    server.shutdown()
                    thread.join(timeout=4)
