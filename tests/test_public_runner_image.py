"""Offline image inputs, CLI capability and manual-probe process supervision."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RESERVE = ROOT / 'deploy/runner-reserve'


def launch_tail():
    """Exercise only final dispatch, never bootstrap, Docker, network or a runner."""
    source = (RESERVE / 'start-runner.sh').read_text()
    return source[source.index('if [[ "${RUNNER_RESERVE_QUALIFICATION:-}" == 1 ]]; then'):]


def executable(path, source):
    path.write_text(source)
    path.chmod(0o700)


class PublicRunnerImageTests(unittest.TestCase):
    def test_root_context_allows_only_exact_required_files(self):
        lines = [line for line in (RESERVE / 'Dockerfile.dockerignore').read_text().splitlines()
                 if line and not line.startswith('#')]
        self.assertEqual(lines[0], '**')
        allowed = {
            '.github/gh/release.json', 'scripts/install_release_gh.py',
            'deploy/runner-reserve/Dockerfile',
            'deploy/runner-reserve/Dockerfile.dockerignore',
            *{'deploy/runner-reserve/' + name for name in (
                'start-runner.sh', 'sandbox-docker.sh', 'qualify.sh', 'guard-ca.crt')},
        }
        # No wildcard or directory exception can reopen unrelated context files.
        self.assertEqual(set(lines[1:]), {'!' + name for name in allowed})
        source = (RESERVE / 'Dockerfile').read_text().replace('\\\n', '')
        copied = set()
        for line in source.splitlines():
            if line.startswith('COPY ') and '--from=' not in line:
                copied.update(shlex.split(line)[1:-1])
        self.assertEqual(copied, allowed - {
            'deploy/runner-reserve/Dockerfile',
            'deploy/runner-reserve/Dockerfile.dockerignore'})
        for path in allowed - {'deploy/runner-reserve/guard-ca.crt'}:
            self.assertTrue((ROOT / path).is_file(), path)

    def test_gh_uses_existing_manifest_and_verified_offline_installer(self):
        source = (RESERVE / 'Dockerfile').read_text()
        manifest = json.loads((ROOT / '.github/gh/release.json').read_text())
        self.assertNotRegex(source, r'(?m)^\s+gh\s+\\$')
        url = re.search(r'"(https://github.com/cli/cli/releases/download/[^" ]+)"', source)[1]
        for architecture in ('amd64', 'arm64'):
            self.assertEqual(url.replace('${arch}', architecture),
                             'https://github.com/cli/cli/releases/download/v' + manifest['version']
                             + '/' + manifest['artifacts']['linux_' + architecture]['asset'])
        self.assertIn('GODEBUG=fips140=on python3 /tmp/reserve-gh/scripts/install_release_gh.py', source)
        self.assertIn('--archive /tmp/reserve-gh/archive.tar.gz --output /usr/local/bin/gh', source)
        self.assertIn('test "$(command -v gh)" = /usr/local/bin/gh', source)
        for check in self.capability_checks():
            self.assertIn(check, source)

    @staticmethod
    def capability_checks():
        return [line for line in (RESERVE / 'qualify.sh').read_text().splitlines()
                if line.startswith('gh api ')]

    def test_capability_rejects_missing_parser_flag_and_missing_help_entry(self):
        checks = self.capability_checks()
        self.assertEqual(len(checks), 2)
        with tempfile.TemporaryDirectory() as directory:
            for accepts_flag, advertises_flag, expected in (
                    (True, True, 0), (False, True, 2), (True, False, 1)):
                with self.subTest(accepts=accepts_flag, advertises=advertises_flag):
                    executable(Path(directory) / 'gh', '#!/bin/sh\n'
                               'case "$*" in\n'
                               '"api --paginate --slurp --help") exit '
                               + ('0' if accepts_flag else '2') + ';;\n'
                               '"api --help") echo '
                               + ('--slurp' if advertises_flag else '--paginate') + ';;\n'
                               '*) exit 90;;\nesac\n')
                    result = subprocess.run(['bash', '-eo', 'pipefail', '-c', '\n'.join(checks)],
                                            env={**os.environ, 'PATH': directory + ':' + os.environ['PATH']},
                                            capture_output=True, text=True, timeout=5, check=False)
                    self.assertEqual(result.returncode, expected, result.stderr)

    def test_only_manual_probe_gets_deadline_and_normal_runner_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name in ('timeout', 'setpriv'):
                executable(path / name, '#!/bin/sh\nprintf "%s\\n" "' + name + '" "$@"\n')
            for flag, expected in (
                ('1', ['timeout', '--signal=TERM', '--kill-after=15s', '600s', 'setpriv',
                       '--reuid=runner', '--regid=runner', '--init-groups',
                       '/opt/victron-ci-reserve/qualify.sh']),
                ('0', ['setpriv', '--reuid=runner', '--regid=runner', '--init-groups', './run.sh']),
            ):
                with self.subTest(flag=flag):
                    result = subprocess.run(['bash', '-e', '-c', launch_tail()],
                                            env={**os.environ, 'PATH': directory + ':' + os.environ['PATH'],
                                                 'RUNNER_RESERVE_QUALIFICATION': flag},
                                            capture_output=True, text=True, timeout=5, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.splitlines(), expected)

    def test_timeout_kills_descendants_across_exec_boundary(self):
        timeout = shutil.which('gtimeout') or shutil.which('timeout')
        if not timeout:
            self.skipTest('GNU timeout is needed for the local process-group test')
        version = subprocess.run([timeout, '--version'], capture_output=True, text=True,
                                 timeout=5, check=False)
        if 'GNU coreutils' not in version.stdout:
            self.skipTest('The installed timeout is not GNU coreutils')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            report = path / 'report.jsonl'
            # The shim checks production arguments, records the pre-exec identity,
            # then replaces itself like setpriv. It does not change local UIDs.
            executable(path / 'setpriv', '#!' + sys.executable + '\n' + '''
import json, os, sys
assert sys.argv[1:4] == ['--reuid=runner', '--regid=runner', '--init-groups']
assert sys.argv[4] == '/opt/victron-ci-reserve/qualify.sh'
with open(os.environ['REPORT'], 'a') as out:
    out.write(json.dumps({'stage': 'before', 'pid': os.getpid(), 'pgid': os.getpgrp()}) + '\\n')
os.execv(sys.executable, [sys.executable, os.environ['PROBE']])
''')
            executable(path / 'timeout', '#!' + sys.executable + '\n' + '''
import os, sys
assert sys.argv[1:4] == ['--signal=TERM', '--kill-after=15s', '600s']
# Shorten only this offline fixture; production constants are asserted above.
os.execv(os.environ['GNU_TIMEOUT'], [os.environ['GNU_TIMEOUT'], '--signal=TERM',
          '--kill-after=0.2s', '1s', *sys.argv[4:]])
''')
            probe = path / 'probe.py'
            probe.write_text('''import json, os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c',
    'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
with open(os.environ['REPORT'], 'a') as out:
    out.write(json.dumps({'stage': 'after', 'pid': os.getpid(), 'pgid': os.getpgrp(),
                         'child': child.pid, 'child_pgid': os.getpgid(child.pid)}) + '\\n')
time.sleep(60)
''')
            process = subprocess.Popen(['bash', '-e', '-c', launch_tail()],
                                       env={**os.environ, 'PATH': directory + ':' + os.environ['PATH'],
                                            'REPORT': str(report), 'PROBE': str(probe),
                                            'GNU_TIMEOUT': timeout, 'RUNNER_RESERVE_QUALIFICATION': '1'},
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, start_new_session=True)
            try:
                _, stderr = process.communicate(timeout=8)
                self.assertIn(process.returncode, (-signal.SIGKILL, 137), stderr)
                before, after = [json.loads(line) for line in report.read_text().splitlines()]
                self.assertEqual(before['pid'], after['pid'])
                self.assertEqual(before['pgid'], after['pgid'])
                self.assertEqual(after['pgid'], after['child_pgid'])
                self.assertEqual(after['pgid'], process.pid)
                for pid in (after['pid'], after['child']):
                    # An orphan zombie has terminated; reaping is the host's duty.
                    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                                           capture_output=True, text=True, timeout=5, check=False)
                    self.assertTrue(not state.stdout.strip() or state.stdout.strip().startswith('Z'),
                                    f'owned process {pid} survived: {state.stdout}')
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
