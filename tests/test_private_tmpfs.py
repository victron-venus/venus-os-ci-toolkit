"""Exercise the private storage gate without mounting or starting a daemon."""
import importlib.util
import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'deploy/arc-private/sandbox-docker.sh'
SPEC = importlib.util.spec_from_file_location('private_tmpfs_arc', ROOT / 'deploy/arc-private/render.py')
arc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(arc)
IMAGE = 'localhost/private@sha256:' + 'a' * 64
CANDIDATE = HELPER.read_text()
ARGV = ['--containerd=/var/run/docker/containerd/containerd.sock', '--iptables=false', '--ip6tables=false', '--feature', 'embedded-containerd=false', '--feature', 'containerd-snapshotter=false', '--storage-driver=overlayfs', '--mtu=1400', '--group=runner', '--log-driver', 'argument with spaces']
ROW = {'name': 'owner/private', 'visibility': 'private', 'profiles': ['ci', 'automation', 'release']}


class PrivateTmpfsTests(unittest.TestCase):
    def gate(self, setup='', invoke='driver=$(private_tmpfs_storage "$data"); printf \'%s\' "$driver"'):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / 'data'
            data.mkdir()
            result = subprocess.run(['bash', '-c', '''
source "$1"
data=$2
state=$3
RUNNER_PRIVATE_DOCKER_STORAGE=ci-tmpfs-2gi
RUNNER_DOCKER_STORAGE_DRIVER=overlayfs
RUNNER_PRIVATE_NAMESPACE=arc-private-ci
RUNNER_PRIVATE_PROFILE=ci
RUNNER_PRIVATE_MEMORY_LIMIT=6442450944
RUNNER_PRIVATE_MEMORY_REQUEST=3221225472
id() { printf 0; }
uname() { printf 6.12-gvisor; }
findmnt() { if [[ -e "$state" ]]; then printf tmpfs; else printf 9p; fi; }
mount() { printf '%s\\n' "$*" > "$state"; }
stat() { printf 'tmpfs 4096 524288 262144'; }
''' + setup + '\n' + invoke,
                'test', str(HELPER), str(data), str(Path(directory) / 'mounted'),
                str(ROOT / 'deploy/runner-reserve/sandbox-docker.sh')],
                text=True, capture_output=True, check=False, timeout=10)
            mounted = Path(directory) / 'mounted'
            return result, mounted.read_text() if mounted.exists() else None

    def test_exact_bounded_guest_mount_is_accepted(self):
        result, mounted = self.gate()
        self.assertEqual((result.returncode, result.stdout), (0, 'overlayfs'), result.stderr)
        self.assertTrue(mounted.startswith('-t tmpfs -o size=2147483648,nr_inodes=262144,mode=0710 tmpfs '))
        self.assertIn('PRIVATE_DOCKER_TMPFS_READY filesystem=tmpfs bytes=2147483648 inodes=262144', result.stderr)

    def test_covered_mount_table_entry_does_not_hide_visible_tmpfs(self):
        result, mounted = self.gate('''
findmnt() { if [[ -e "$state" ]]; then printf '9p\\ntmpfs'; else printf 9p; fi; }
''')
        self.assertEqual((result.returncode, result.stdout), (0, 'overlayfs'), result.stderr)
        self.assertIsNotNone(mounted)

    def test_profile_resources_and_unknown_modes_fail_before_mount(self):
        for setup in [
                'RUNNER_PRIVATE_DOCKER_STORAGE=tmpfs', 'RUNNER_DOCKER_STORAGE_DRIVER=vfs',
                'RUNNER_DOCKER_STORAGE_DRIVER=overlay2',
                'RUNNER_PRIVATE_NAMESPACE=runner-reserve-ci', 'RUNNER_PRIVATE_PROFILE=automation',
                'RUNNER_PRIVATE_PROFILE=release', 'RUNNER_PRIVATE_MEMORY_LIMIT=17179869184',
                'RUNNER_PRIVATE_MEMORY_LIMIT=2147483648', 'RUNNER_PRIVATE_MEMORY_REQUEST=1073741824',
                'RUNNER_PRIVATE_MEMORY_REQUEST=7000000000', 'RUNNER_PRIVATE_MEMORY_REQUEST=bogus',
                'RUNNER_PRIVATE_MEMORY_REQUEST=03221225472', 'unset RUNNER_PRIVATE_MEMORY_REQUEST',
                'id() { printf 1001; }', 'uname() { printf 6.12; }']:
            with self.subTest(setup=setup):
                result, mounted = self.gate(setup)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertIsNone(mounted)

    def test_mount_failure_never_falls_back_or_prints_driver(self):
        result, mounted = self.gate('mount() { return 1; }')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIsNone(mounted)

    def test_missing_nonempty_symlink_or_wrong_backing_mount_is_rejected(self):
        for setup in [
                'rmdir "$data"', 'touch "$data/preexisting"',
                'mv "$data" "$data-real"; ln -s "$data-real" "$data"',
                'findmnt() { printf overlay; }', 'findmnt() { printf tmpfs; }',
                'findmnt() { return 1; }', 'find() { return 1; }']:
            with self.subTest(setup=setup):
                result, mounted = self.gate(setup)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(mounted)

    def test_mount_readback_and_capacity_must_match(self):
        for setup in [
                'stat() { printf "v9fs 4096 524288 262144"; }',
                'stat() { printf "overlayfs 4096 524288 262144"; }',
                'stat() { return 1; }', 'stat() { printf "tmpfs 4096 1048576 262144"; }',
                'stat() { printf "tmpfs 4096 524288 999999"; }', 'stat() { printf "bad output"; }',
                'stat() { printf "tmpfs 4096 524288 262144 extra"; }',
                'stat() { printf "tmpfs 4096 524288 262144\\nv9fs 4096 524288 262144"; }']:
            with self.subTest(setup=setup):
                result, mounted = self.gate(setup)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertIsNotNone(mounted)

    def test_failed_storage_gate_stops_shared_launcher_before_any_daemon(self):
        for setup in ['', self.new_mode_setup('ci-heavy-tmpfs-6gi'),
                      self.new_mode_setup('automation-tmpfs-3gi')]:
            for failure in ['mount() { return 1; }',
                            'stat() { printf "v9fs 4096 524288 262144"; }',
                            'stat() { printf "tmpfs 4096 1 1"; }']:
                result, _ = self.gate('''
    base=$4
    source() {
      [[ "$1" == /opt/victron-ci-reserve/sandbox-docker-base.sh ]] || return 1
      builtin source "$base"
    }
    mkdir() { printf UNEXPECTED_PRE_DAEMON_WRITE >&2; return 1; }
    containerd() { printf UNEXPECTED_DAEMON >&2; return 1; }
    dockerd() { printf UNEXPECTED_DAEMON >&2; return 1; }
    ''' + setup + failure, invoke='''
    # Source the real shared helper: its main guard must not start either daemon.
    source /opt/victron-ci-reserve/sandbox-docker-base.sh
    docker_storage_driver() { private_tmpfs_storage "$data"; }
    start_sandbox_docker 1500
    ''')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('UNEXPECTED_', result.stderr)
                self.assertNotIn('PRIVATE_DOCKER_TMPFS_READY', result.stderr)

    def test_failed_statfs_diagnostics_are_limited_and_shell_quoted(self):
        result, _ = self.gate('stat() { printf "v9fs;bad 4096 524288 262144 ignored-extra"; }')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(r'filesystem=v9fs\;bad block_size=4096 blocks=524288 inodes=262144', result.stderr)
        self.assertNotIn('ignored-extra', result.stderr)
        self.assertNotIn('memory_request', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_opt_in_reserves_ci_memory_but_preserves_other_profiles(self):
        row = {**ROW, 'ci_storage': 'tmpfs-2gi'}
        for profile in ROW['profiles']:
            old = arc.values(ROW, profile, IMAGE, IMAGE, '192.0.2.1')
            new = arc.values(row, profile, IMAGE, IMAGE, '192.0.2.1')
            if profile != 'ci':
                self.assertEqual(old, new)
                continue
            pod = new['template']['spec']
            self.assertEqual(pod['runtimeClassName'], 'victron-reserve')
            runner = pod['containers'][0]
            self.assertEqual(runner['resources']['requests']['memory'], '3Gi')
            self.assertEqual(runner['resources']['limits']['memory'], '6Gi')
            self.assertEqual(runner['resources']['requests']['cpu'], '1900m')
            env = {e['name']: e for e in runner['env']}
            self.assertEqual(env['RUNNER_PRIVATE_DOCKER_STORAGE']['value'], 'ci-tmpfs-2gi')
            self.assertEqual(env['RUNNER_DOCKER_STORAGE_DRIVER']['value'], 'overlayfs')
            self.assertEqual(env['RUNNER_PRIVATE_MEMORY_REQUEST']['valueFrom']['resourceFieldRef'],
                             {'containerName': 'runner', 'resource': 'requests.memory', 'divisor': '1'})
            self.assertEqual(pod['volumes'], old['template']['spec']['volumes'])

    def test_unqualified_profile_and_runtime_combinations_are_refused(self):
        for row in [{**ROW, 'ci_storage': 'unbounded'},
                    {**ROW, 'ci_storage': 'tmpfs-2gi', 'profiles': ['automation']},
                    {**ROW, 'ci_storage': 'tmpfs-2gi', 'ci_resource_class': 'heavy'}]:
            with self.subTest(row=row), self.assertRaises(ValueError):
                arc.validate_inventory({'repositories': [row]})
        with self.assertRaises(ValueError):
            arc.values({**ROW, 'ci_storage': 'tmpfs-2gi'}, 'ci', IMAGE, IMAGE, '192.0.2.1',
                       sandbox_runtime=arc.EXCLUSIVE_RUNTIME)

    def test_two_candidates_fit_memory_but_cpu_quota_admits_only_one(self):
        def mib(value):
            return int(value[:-2]) * (1024 if value.endswith('Gi') else 1)
        pod = arc.values({**ROW, 'ci_storage': 'tmpfs-2gi'}, 'ci', IMAGE, IMAGE, '192.0.2.1')['template']['spec']
        overhead = yaml.safe_load((ROOT / 'deploy/runner-reserve/runtime-class.yaml').read_text())['overhead']['podFixed']
        quota = arc.sandbox_quota('ci')
        for kind in ('requests', 'limits'):
            amount = mib(pod['containers'][0]['resources'][kind]['memory']) + mib(overhead['memory'])
            self.assertLessEqual(2 * amount, mib(quota[kind + '.memory']))
        cpu = (int(pod['containers'][0]['resources']['requests']['cpu'][:-1])
               + int(overhead['cpu'][:-1]))
        self.assertEqual(cpu, int(quota['requests.cpu'][:-1]))
        self.assertGreater(2 * cpu, int(quota['requests.cpu'][:-1]))

    def test_private_admission_bounds_selector_count_and_actual_resources(self):
        items = arc.foundations([ROW], IMAGE, '192.0.2.1')['items']
        policy = next(x for x in items if x['kind'] == 'ValidatingAdmissionPolicy')
        rule = next(r['expression'] for r in policy['spec']['validations']
                    if r['message'].startswith('The tmpfs candidate'))
        for guard in ["size(c.env.filter(e, e.name == 'RUNNER_PRIVATE_DOCKER_STORAGE')) <= 1",
                      "e.value == 'ci-tmpfs-2gi'", "!has(e.valueFrom)",
                      "object.metadata.namespace == 'arc-private-ci'",
                      "has(c.resources)", "has(dyn(c.resources).requests)", "has(dyn(c.resources).limits)",
                      "type(dyn(c.resources).requests) == map", "type(dyn(c.resources).limits) == map",
                      "'memory' in dyn(c.resources).requests", "'memory' in dyn(c.resources).limits",
                      "type(dyn(c.resources).requests['memory']) == string",
                      "type(dyn(c.resources).limits['memory']) == string",
                      "dyn(c.resources).requests['memory'] == '3Gi'", "dyn(c.resources).limits['memory'] == '6Gi'"]:
            self.assertIn(guard, rule)
        self.assertNotIn('c.resources.requests', rule)
        self.assertNotIn('c.resources.limits', rule)

    def new_mode_setup(self, mode):
        if mode == 'ci-heavy-tmpfs-6gi':
            return """
RUNNER_PRIVATE_DOCKER_STORAGE=ci-heavy-tmpfs-6gi
RUNNER_PRIVATE_MEMORY_REQUEST=13958643712
RUNNER_PRIVATE_MEMORY_LIMIT=17179869184
stat() { printf 'tmpfs 4096 1572864 524288'; }
"""
        return """
RUNNER_PRIVATE_DOCKER_STORAGE=automation-tmpfs-3gi
RUNNER_PRIVATE_NAMESPACE=arc-private-automation
RUNNER_PRIVATE_PROFILE=automation
RUNNER_PRIVATE_MEMORY_REQUEST=5368709120
RUNNER_PRIVATE_MEMORY_LIMIT=6442450944
stat() { printf 'tmpfs 4096 786432 262144'; }
"""

    def test_fixed_new_modes_mount_only_their_exact_caps(self):
        for mode, capacity, inodes in [('ci-heavy-tmpfs-6gi', 6442450944, 524288),
                                      ('automation-tmpfs-3gi', 3221225472, 262144)]:
            with self.subTest(mode=mode):
                result, mounted = self.gate(self.new_mode_setup(mode))
                self.assertEqual((result.returncode, result.stdout), (0, 'overlayfs'), result.stderr)
                self.assertIn(f'size={capacity},nr_inodes={inodes},mode=0710', mounted)
                self.assertIn(f'bytes={capacity} inodes={inodes}', result.stderr)

    def test_new_modes_reject_cross_profile_and_resource_spoofing_before_mount(self):
        for mode in ['ci-heavy-tmpfs-6gi', 'automation-tmpfs-3gi']:
            for mutation in ['RUNNER_PRIVATE_NAMESPACE=arc-private-release',
                             'RUNNER_PRIVATE_NAMESPACE=runner-reserve-ci',
                             'RUNNER_PRIVATE_PROFILE=release',
                             'RUNNER_PRIVATE_MEMORY_REQUEST=3221225472',
                             'RUNNER_PRIVATE_MEMORY_REQUEST=17179869184',
                             'RUNNER_PRIVATE_MEMORY_REQUEST=05368709120',
                             'RUNNER_PRIVATE_MEMORY_LIMIT=2147483648',
                             'RUNNER_DOCKER_STORAGE_DRIVER=vfs', 'RUNNER_DOCKER_STORAGE_DRIVER=overlay2',
                             'uname() { printf 6.12; }', 'id() { printf 1001; }']:
                with self.subTest(mode=mode, mutation=mutation):
                    result, mounted = self.gate(self.new_mode_setup(mode) + mutation)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, '')
                    self.assertIsNone(mounted)

    def test_new_modes_never_accept_wrong_visible_capacity_or_inode_limit(self):
        for mode in ['ci-heavy-tmpfs-6gi', 'automation-tmpfs-3gi']:
            for mutation in ["stat() { printf 'tmpfs 4096 524288 262144'; }",
                             "stat() { printf 'tmpfs 4096 1572864 999999'; }",
                             "stat() { printf 'tmpfs 9999999999 9999999999 524288'; }",
                             'mount() { return 1; }']:
                with self.subTest(mode=mode, mutation=mutation):
                    result, _ = self.gate(self.new_mode_setup(mode) + mutation)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, '')
                    self.assertNotIn('PRIVATE_DOCKER_TMPFS_READY', result.stderr)

    def test_new_inventory_modes_require_private_exact_profiles_and_resource_class(self):
        invalid = [
            {**ROW, 'ci_storage': 'tmpfs-6gi-heavy'},
            {**ROW, 'ci_storage': 'tmpfs-6gi-heavy', 'ci_resource_class': 'heavy', 'profiles': ['automation']},
            {**ROW, 'automation_storage': 'tmpfs-3gi', 'profiles': ['ci']},
            {**ROW, 'automation_storage': 'tmpfs-2gi'},
            {**ROW, 'ci_storage': 'tmpfs-3gi'},
            {**ROW, 'ci_storage': 'tmpfs-6gi-heavy', 'ci_resource_class': 'heavy', 'visibility': 'public'},
            {**ROW, 'automation_storage': 'tmpfs-3gi', 'visibility': 'public'},
        ]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                arc.validate_inventory({'repositories': [row]})
        for row in [{**ROW, 'ci_storage': 'tmpfs-6gi-heavy', 'ci_resource_class': 'heavy'},
                    {**ROW, 'automation_storage': 'tmpfs-3gi'}]:
            with self.subTest(row=row), self.assertRaises(ValueError):
                arc.values(row, 'ci', IMAGE, IMAGE, '192.0.2.1', sandbox_runtime=arc.EXCLUSIVE_RUNTIME)

    def test_explicit_modes_change_only_selected_profile_memory_and_storage(self):
        for selected, row, expected, cpu in [
                ('ci', {**ROW, 'ci_resource_class': 'heavy', 'ci_storage': 'tmpfs-6gi-heavy'},
                 ('13Gi', '16Gi', 'ci-heavy-tmpfs-6gi'), ('1500m', '3')),
                ('automation', {**ROW, 'automation_storage': 'tmpfs-3gi'},
                 ('5Gi', '6Gi', 'automation-tmpfs-3gi'), ('50m', '1'))]:
            baseline = {k: v for k, v in row.items() if k not in ('ci_storage', 'automation_storage')}
            for profile in ROW['profiles']:
                old = arc.values(baseline, profile, IMAGE, IMAGE, '192.0.2.1')
                new = arc.values(row, profile, IMAGE, IMAGE, '192.0.2.1')
                if profile != selected:
                    self.assertEqual(old, new)
                    continue
                runner = new['template']['spec']['containers'][0]
                self.assertEqual((runner['resources']['requests']['memory'],
                                  runner['resources']['limits']['memory']), expected[:2])
                self.assertEqual((runner['resources']['requests']['cpu'],
                                  runner['resources']['limits']['cpu']), cpu)
                self.assertIn({'name': 'RUNNER_PRIVATE_DOCKER_STORAGE', 'value': expected[2]}, runner['env'])
                self.assertIn({'name': 'RUNNER_DOCKER_STORAGE_DRIVER', 'value': 'overlayfs'}, runner['env'])
                self.assertEqual(new['template']['spec']['volumes'], old['template']['spec']['volumes'])

    def test_automation_quota_is_opt_in_and_excludes_heavy_plus_standard(self):
        def quotas(row):
            return {r['metadata']['namespace']: r['spec']['hard']
                    for r in arc.foundations([row], IMAGE, '192.0.2.1')['items']
                    if r['kind'] == 'ResourceQuota'}
        base = quotas(ROW)
        self.assertEqual(base, quotas({**ROW, 'automation_storage': 'disk'}))
        self.assertEqual(base, quotas({**ROW, 'ci_resource_class': 'heavy', 'ci_storage': 'tmpfs-6gi-heavy'}))
        changed = quotas({**ROW, 'automation_storage': 'tmpfs-3gi'})
        expected = {k: dict(v) for k, v in base.items()}
        expected['arc-private-automation']['requests.memory'] = '6Gi'
        self.assertEqual(changed, expected)
        def mib(value):
            return int(value[:-2]) * (1024 if value.endswith('Gi') else 1)
        overhead = yaml.safe_load((ROOT / 'deploy/runner-reserve/runtime-class.yaml').read_text())['overhead']['podFixed']
        def memory(row, kind):
            pod = arc.values(row, 'automation', IMAGE, IMAGE, '192.0.2.1')['template']['spec']
            return mib(pod['containers'][0]['resources'][kind]['memory']) + mib(overhead['memory'])
        heavy = {**ROW, 'automation_storage': 'tmpfs-3gi'}
        quota = changed['arc-private-automation']
        self.assertEqual(quota['limits.memory'], '9Gi')
        for kind in ('requests', 'limits'):
            budget = mib(quota[kind + '.memory'])
            self.assertLessEqual(memory(heavy, kind), budget)
            self.assertGreater(memory(heavy, kind) + memory(ROW, kind), budget)
            self.assertLessEqual(2 * memory(ROW, kind), budget)
            self.assertGreater(2 * memory(heavy, kind), budget)

    def test_admission_contains_only_named_exact_new_tuples(self):
        expression = arc.private_tmpfs_validation()
        for mode, profile, request, limit, cpu_request, cpu_limit in [
                ('ci-tmpfs-2gi', 'ci', '3Gi', '6Gi', '1900m', '4'),
                ('ci-heavy-tmpfs-6gi', 'ci', '13Gi', '16Gi', '1500m', '3'),
                ('automation-tmpfs-3gi', 'automation', '5Gi', '6Gi', '50m', '1')]:
            branch = next(part for part in expression.split(' || ') if f"e.value == '{mode}'" in part)
            for value in [f"object.metadata.namespace == 'arc-private-{profile}'",
                          f"object.metadata.labels['private-runners.github.io/profile'] == '{profile}'",
                          f"dyn(c.resources).requests['memory'] == '{request}'",
                          f"dyn(c.resources).limits['memory'] == '{limit}'",
                          f"dyn(c.resources).requests['cpu'] == '{cpu_request}'",
                          f"dyn(c.resources).limits['cpu'] == '{cpu_limit}'",
                          "b.name == 'RUNNER_DOCKER_STORAGE_DRIVER'", "b.value == 'overlayfs'"]:
                self.assertIn(value, branch)
        self.assertIn("size(c.env.filter(e, e.name == 'RUNNER_PRIVATE_DOCKER_STORAGE')) <= 1", expression)
        self.assertIn("object.spec.runtimeClassName == 'victron-reserve'", expression)
        self.assertIn('!has(e.valueFrom)', expression)
        self.assertNotIn("dyn(c.resources).requests['cpu'] == '900m'", expression)


    def test_only_private_image_installs_wrapper_and_default_remains_disk(self):
        self.assertNotIn('arc-private/sandbox-docker.sh', (ROOT / 'deploy/runner-reserve/Dockerfile').read_text())
        self.assertIn('COPY deploy/arc-private/sandbox-docker.sh', (ROOT / 'deploy/arc-private/Dockerfile').read_text())
        for profile in ROW['profiles']:
            self.assertEqual(arc.values(ROW, profile, IMAGE, IMAGE, '192.0.2.1'),
                             arc.values({**ROW, 'ci_storage': 'disk'}, profile, IMAGE, IMAGE, '192.0.2.1'))
        for mode in ('unset RUNNER_PRIVATE_DOCKER_STORAGE', 'RUNNER_PRIVATE_DOCKER_STORAGE=disk'):
            result = subprocess.run(['bash', '-c', '''
source "$1"
exec() { printf '%s' "$*"; }
''' + mode + '\nprivate_start_docker 1500', 'test', str(HELPER)],
                text=True, capture_output=True, check=False, timeout=10)
            self.assertEqual((result.returncode, result.stdout),
                             (0, '/opt/victron-ci-reserve/sandbox-docker-base.sh 1500'))
        result = subprocess.run(['bash', '-c', 'source "$1"; RUNNER_PRIVATE_DOCKER_STORAGE=unknown; private_start_docker 1500',
                                 'test', str(HELPER)], text=True, capture_output=True, check=False, timeout=10)
        self.assertNotEqual(result.returncode, 0)


    def harness(self, argv=ARGV, *, ignore_term=False, expect_ok=True, missing=None, relative=None, mode='ci-tmpfs-2gi'):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binaries = root/'bin'
            binaries.mkdir()
            sleep_binary = shutil.which('sleep')
            self.assertIsNotNone(sleep_binary)
            (binaries/'sleep').symlink_to(Path(sleep_binary).resolve())
            fake_source = f'#!{sys.executable}\n' + '''import json, os, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN if os.environ.get('IGNORE_TERM') == '1' else lambda *_: sys.exit(0))
record = Path(os.environ['RECORD'])
mask = os.umask(0)
os.umask(mask)
directory = record.with_suffix('.directory')
os.mkdir(directory, 0o755)
file = directory / 'file'
file.write_text('ordinary daemon file')
record.write_text(json.dumps({'pid':os.getpid(), 'gomaxprocs':os.environ.get('GOMAXPROCS'),
    'argv':sys.argv[1:], 'umask':mask, 'directory_mode':directory.stat().st_mode & 0o777,
    'file_mode':file.stat().st_mode & 0o777}))
while True: time.sleep(1)
'''
            for name in ('containerd', 'dockerd'):
                if name != missing:
                    target = (root if name == relative else binaries) / name
                    target.write_text(fake_source)
                    target.chmod(0o755)
            public = (ROOT/'deploy/runner-reserve/sandbox-docker.sh').read_text()
            cleanup = 'cleanup_startup() {' + public.split('cleanup_startup() {', 1)[1].split('\ndocker_api_ready()', 1)[0]
            helper = root/'helper.sh'
            helper.write_text(cleanup + '''
start_sandbox_docker() {
  trap cleanup_startup EXIT
  RECORD="$OUT/containerd.json" containerd --config 'path with spaces' &
  sandbox_containerd_pid=$!
  RECORD="$OUT/dockerd.json" dockerd ''' + shlex.join(argv) + ''' &
  sandbox_docker_pid=$!
  for attempt in {1..200}; do
    [[ -s "$OUT/dockerd.json" && -s "$OUT/containerd.json" ]] && break
    kill -0 "$sandbox_docker_pid" 2>/dev/null || return 1
    sleep 0.01
  done
  [[ -s "$OUT/dockerd.json" && -s "$OUT/containerd.json" ]] || return 1
  printf '%s %s %s\\n' "$sandbox_docker_pid" "$sandbox_containerd_pid" "${GOMAXPROCS-unset}"
  cleanup_startup
  trap - EXIT
  ! kill -0 "$sandbox_docker_pid" 2>/dev/null
  ! kill -0 "$sandbox_containerd_pid" 2>/dev/null
}
''')
            script = root/'candidate.sh'
            script.write_text(CANDIDATE.replace('/opt/victron-ci-reserve/sandbox-docker-base.sh', str(helper)))
            env = dict(os.environ, OUT=str(root), RECORD=str(root/'unexpected.json'), RUNNER_PRIVATE_DOCKER_STORAGE=mode, GOMAXPROCS='7', IGNORE_TERM='1' if ignore_term else '0')
            # A missing fixture must never fall through to a real host daemon.
            env['PATH'] = ('.' + os.pathsep if relative else '') + str(binaries)
            started = time.monotonic()
            # Both actual adapters execute through the real background/PID cleanup path.
            # Only external daemon executables are replaced with harmless child processes.
            r = subprocess.run(['/bin/bash', '-c', '''
umask 077
source "$1"
private_start_docker 1400
umask > "$OUT/caller-mask"
''', 'test', str(script)], env=env, cwd=root, capture_output=True, text=True, timeout=9)
            if not expect_ok:
                self.assertNotEqual(r.returncode, 0)
                self.assertFalse((root/'dockerd.json').exists())
                if missing or relative:
                    self.assertFalse((root/'containerd.json').exists())
                return
            self.assertEqual(r.returncode, 0, r.stderr)
            docker_pid, containerd_pid, parent_value = r.stdout.strip().split()
            d=json.loads((root/'dockerd.json').read_text())
            c=json.loads((root/'containerd.json').read_text())
            self.assertEqual((d['pid'],c['pid']), (int(docker_pid),int(containerd_pid)))
            self.assertEqual((d['gomaxprocs'],c['gomaxprocs'],parent_value), ('7','7','7'))
            self.assertEqual(int((root/'caller-mask').read_text().strip(), 8), 0o077)
            for record in (d, c):
                self.assertEqual((record['umask'], record['directory_mode'], record['file_mode']),
                                 (0o022, 0o755, 0o644))
            self.assertEqual(c['argv'], ['--config', 'path with spaces'])
            expected = list(argv)
            expected[expected.index('containerd-snapshotter=false')] = 'containerd-snapshotter=true'
            self.assertEqual(d['argv'], expected)
            self.assertLess(time.monotonic()-started, 8)

    def test_argv_pid_and_original_cleanup(self):
        for mode in ['ci-tmpfs-2gi', 'ci-heavy-tmpfs-6gi', 'automation-tmpfs-3gi']:
            with self.subTest(mode=mode):
                self.harness(mode=mode)

    def test_term_ignoring_daemon_escalates_and_reaps(self):
        self.harness(ignore_term=True)

    def test_invalid_feature_and_driver_arguments_rejected(self):
        bad = [ARGV + ['--feature','containerd-snapshotter=false'], [v for v in ARGV if v != 'containerd-snapshotter=false'], [v.replace('containerd-snapshotter=false','containerd-snapshotter=true') for v in ARGV], ARGV + ['--feature=containerd-snapshotter=false'], [v.replace('--storage-driver=overlayfs','--storage-driver=overlay2') for v in ARGV], ARGV + ['--storage-driver=overlayfs']]
        for argv in bad:
            with self.subTest(argv=argv):
                self.harness(argv, expect_ok=False)

    def test_no_absolute_daemon_fails_before_any_launch(self):
        for name in ('containerd', 'dockerd'):
            with self.subTest(missing=name):
                self.harness(missing=name, expect_ok=False)
            with self.subTest(relative=name):
                self.harness(relative=name, expect_ok=False)

    def test_backend_admission_keeps_disk_on_classic_driver(self):
        policy=next(item for item in arc.foundations([ROW], IMAGE, '192.0.2.1')['items']
                    if item['kind']=='ValidatingAdmissionPolicy')
        expr=next(rule['expression'] for rule in policy['spec']['validations']
                  if rule['message']=='Private storage modes require their exact Docker backend')
        self.assertIn("size(c.env.filter(e, e.name == 'RUNNER_DOCKER_STORAGE_DRIVER')) == 1",expr)
        self.assertIn("c.env.exists(m, m.name == 'RUNNER_PRIVATE_DOCKER_STORAGE') ? 'overlayfs' : 'overlay2'",expr)
        self.assertIn('!has(e.valueFrom)',expr)
        for profile in ROW['profiles']:
            env=arc.values(ROW, profile, IMAGE, IMAGE, '192.0.2.1')['template']['spec']['containers'][0]['env']
            self.assertIn({'name':'RUNNER_DOCKER_STORAGE_DRIVER','value':'overlay2'},env)
            self.assertFalse(any(e['name']=='RUNNER_PRIVATE_DOCKER_STORAGE' for e in env))

if __name__ == '__main__':
    unittest.main()
