"""Exercise the private storage gate without mounting or starting a daemon."""
import importlib.util
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
RUNNER_DOCKER_STORAGE_DRIVER=overlay2
RUNNER_PRIVATE_NAMESPACE=arc-private-ci
RUNNER_PRIVATE_PROFILE=ci
RUNNER_PRIVATE_MEMORY_LIMIT=6442450944
RUNNER_PRIVATE_MEMORY_REQUEST=3221225472
id() { printf 0; }
uname() { printf 6.12-gvisor; }
findmnt() { if [[ -e "$state" ]]; then printf tmpfs; else printf 9p; fi; }
mount() { printf '%s\\n' "$*" > "$state"; }
stat() { printf '4096 524288 262144'; }
''' + setup + '\n' + invoke,
                'test', str(HELPER), str(data), str(Path(directory) / 'mounted'),
                str(ROOT / 'deploy/runner-reserve/sandbox-docker.sh')],
                text=True, capture_output=True, check=False, timeout=10)
            mounted = Path(directory) / 'mounted'
            return result, mounted.read_text() if mounted.exists() else None

    def test_exact_bounded_guest_mount_is_accepted(self):
        result, mounted = self.gate()
        self.assertEqual((result.returncode, result.stdout), (0, 'overlay2'), result.stderr)
        self.assertTrue(mounted.startswith('-t tmpfs -o size=2147483648,nr_inodes=262144,mode=0710 tmpfs '))
        self.assertIn('PRIVATE_DOCKER_TMPFS_READY filesystem=tmpfs bytes=2147483648 inodes=262144', result.stderr)

    def test_profile_resources_and_unknown_modes_fail_before_mount(self):
        for setup in [
                'RUNNER_PRIVATE_DOCKER_STORAGE=tmpfs', 'RUNNER_DOCKER_STORAGE_DRIVER=vfs',
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
                'findmnt() { printf 9p; }',
                'stat() { return 1; }', 'stat() { printf "4096 1048576 262144"; }',
                'stat() { printf "4096 524288 999999"; }', 'stat() { printf "bad output"; }',
                'stat() { printf "4096 524288 262144 extra"; }']:
            with self.subTest(setup=setup):
                result, mounted = self.gate(setup)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertIsNotNone(mounted)

    def test_failed_storage_gate_stops_shared_launcher_before_any_daemon(self):
        for failure in ['mount() { return 1; }', 'stat() { printf "4096 1 1"; }']:
            result, _ = self.gate('''
base=$4
source() {
  [[ "$1" == /opt/victron-ci-reserve/sandbox-docker-base.sh ]] || return 1
  builtin source "$base"
}
mkdir() { printf UNEXPECTED_PRE_DAEMON_WRITE >&2; return 1; }
containerd() { printf UNEXPECTED_DAEMON >&2; return 1; }
dockerd() { printf UNEXPECTED_DAEMON >&2; return 1; }
''' + failure, invoke='''
# Source the real shared helper: its main guard must not start either daemon.
source /opt/victron-ci-reserve/sandbox-docker-base.sh
docker_storage_driver() { private_tmpfs_storage "$data"; }
start_sandbox_docker 1500
''')
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('UNEXPECTED_', result.stderr)
            self.assertNotIn('PRIVATE_DOCKER_TMPFS_READY', result.stderr)

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
            self.assertEqual(runner['resources']['requests']['cpu'], '900m')
            env = {e['name']: e for e in runner['env']}
            self.assertEqual(env['RUNNER_PRIVATE_DOCKER_STORAGE']['value'], 'ci-tmpfs-2gi')
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

    def test_two_candidates_fit_existing_quota_with_overhead(self):
        def mib(value):
            return int(value[:-2]) * (1024 if value.endswith('Gi') else 1)
        pod = arc.values({**ROW, 'ci_storage': 'tmpfs-2gi'}, 'ci', IMAGE, IMAGE, '192.0.2.1')['template']['spec']
        overhead = yaml.safe_load((ROOT / 'deploy/runner-reserve/runtime-class.yaml').read_text())['overhead']['podFixed']
        quota = arc.sandbox_quota('ci')
        for kind in ('requests', 'limits'):
            amount = mib(pod['containers'][0]['resources'][kind]['memory']) + mib(overhead['memory'])
            self.assertLessEqual(2 * amount, mib(quota[kind + '.memory']))

    def test_private_admission_bounds_selector_count_and_actual_resources(self):
        items = arc.foundations([ROW], IMAGE, '192.0.2.1')['items']
        policy = next(x for x in items if x['kind'] == 'ValidatingAdmissionPolicy')
        rule = next(r['expression'] for r in policy['spec']['validations']
                    if r['message'].startswith('The tmpfs candidate'))
        for guard in ["size(c.env.filter(e, e.name == 'RUNNER_PRIVATE_DOCKER_STORAGE')) <= 1",
                      "e.value == 'ci-tmpfs-2gi'", "!has(e.valueFrom)",
                      "object.metadata.namespace == 'arc-private-ci'",
                      "c.resources.requests['memory'] == '3Gi'", "c.resources.limits['memory'] == '6Gi'"]:
            self.assertIn(guard, rule)

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


if __name__ == '__main__':
    unittest.main()
