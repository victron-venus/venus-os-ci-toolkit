"""Exercise daemon readiness and startup cleanup without Docker or host changes."""
from pathlib import Path
import subprocess
import tempfile
import unittest


HELPER = Path(__file__).resolve().parents[1] / 'deploy/runner-reserve/sandbox-docker.sh'


class SandboxDockerStartupTests(unittest.TestCase):
    def run_shell(self, body):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'daemon.log'
            log.write_text('startup diagnostic\n', encoding='utf-8')
            return subprocess.run(
                ['bash', '-c', 'source "$1"; log=$2; ' + body,
                 'test', str(HELPER), str(log)],
                capture_output=True, text=True, timeout=10, check=False)

    def test_retries_real_readiness_before_success(self):
        result = self.run_shell('''
sleep 8 & pid=$!
trap 'kill "$pid" 2>/dev/null || true' EXIT
tries=0
ready() { tries=$((tries + 1)); [[ "$tries" -ge 3 ]]; }
wait_for_service "$pid" 5 "$log" ready
[[ "$tries" == 3 ]]
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_overlay_requires_exact_disk_mount_and_explicit_driver(self):
        default = self.run_shell('unset RUNNER_DOCKER_STORAGE_DRIVER; docker_storage_driver')
        self.assertEqual((default.returncode, default.stdout), (0, 'vfs'))
        for filesystem, status, expected in [('9p', 0, 0), ('overlay', 0, 1),
                                              ('tmpfs', 0, 1), ('9p', 1, 1)]:
            with self.subTest(filesystem=filesystem, status=status):
                result = self.run_shell('''
RUNNER_DOCKER_STORAGE_DRIVER=overlay2
findmnt() { [[ "$*" == '--noheadings --output FSTYPE --mountpoint /var/lib/docker' ]] || return 2; printf '%s' "''' + filesystem + '''"; return ''' + str(status) + '''; }
docker_storage_driver
''')
                self.assertEqual(result.returncode, expected, result.stderr)
        unknown = self.run_shell('RUNNER_DOCKER_STORAGE_DRIVER=unknown; docker_storage_driver')
        self.assertNotEqual(unknown.returncode, 0)

    def test_exited_daemon_fails_without_accepting_stale_socket(self):
        result = self.run_shell('''
true & pid=$!
wait "$pid"
ready() { echo unsafe; return 0; }
wait_for_service "$pid" 5 "$log" ready
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('unsafe', result.stdout)
        self.assertIn('startup diagnostic', result.stderr)

    def test_unready_live_daemon_hits_deadline(self):
        result = self.run_shell('''
sleep 8 & pid=$!
trap 'kill "$pid" 2>/dev/null || true' EXIT
wait_for_service "$pid" 0 "$log" false
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('startup diagnostic', result.stderr)

    def test_startup_cleanup_reaps_both_owned_children(self):
        result = self.run_shell('''
sleep 8 & sandbox_containerd_pid=$!
sleep 8 & sandbox_docker_pid=$!
cleanup_startup
! kill -0 "$sandbox_containerd_pid" 2>/dev/null
! kill -0 "$sandbox_docker_pid" 2>/dev/null
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cleanup_kills_a_daemon_which_ignores_term(self):
        result = self.run_shell('''
ready=$(dirname "$log")/ready
python3 -c 'import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open(sys.argv[1], "w").close(); time.sleep(60)' "$ready" &
sandbox_containerd_pid=$!
while [[ ! -e "$ready" ]]; do sleep 0.01; done
started=$SECONDS
cleanup_startup
((SECONDS - started <= 6))
! kill -0 "$sandbox_containerd_pid" 2>/dev/null
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ping_requires_both_success_and_exact_ready_body(self):
        for body, status, expected in [('OK', 0, 0), ('starting', 0, 1),
                                       ('', 0, 1), ('OK', 22, 1)]:
            with self.subTest(body=body, status=status):
                # Isolate response validation from the real network and clock.
                result = self.run_shell('''
timeout() { shift 2; "$@"; }
curl() { printf '%s' "''' + body + '''"; return ''' + str(status) + '''; }
docker_api_ready
''')
                self.assertEqual(result.returncode, expected, result.stderr)


if __name__ == '__main__':
    unittest.main()
