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
            '.github/containers/runner-go-tools.json', 'scripts/build_runner_go_tools.py',
            '.github/containers/runner-libbpf.json', 'scripts/build_runner_libbpf.py',
            '.github/containers/libbpf/0001-restrict-core-poisoning.patch',
            *{f'.github/containers/runner-go-tools/{component}/{name}'
              for component in ('containerd', 'buildx', 'compose')
              for name in ('go.mod', 'go.sum', 'inputs.json')},
            '.github/containers/runner-browsers.json', 'scripts/install_runner_browsers.py',
            'scripts/browser-toolchains.json', 'deploy/runner-reserve/qualify-browsers.cjs',
            *{'.github/containers/runner-browsers/' + name for name in (
                'google-Release', 'google-Release.gpg', 'google-key.gpg', 'google-amd64-Packages',
                'edge-Release', 'edge-Release.gpg', 'microsoft-key.gpg', 'edge-amd64-Packages')},
            '.github/containers/runner-npm.json', 'scripts/install_runner_npm.py',
            'scripts/prune_runner_npm.py', 'deploy/runner-reserve/qualify-npm.cjs',
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

    def test_go_source_builds_keep_the_same_nonroot_stage_context(self):
        source = (RESERVE / 'Dockerfile').read_text().replace('\\\n', ' ')
        expected_env = {'HOME': '/home/go-builder', 'GOPATH': '/home/go-builder/go',
                        'GOMODCACHE': '/home/go-builder/go/pkg/mod',
                        'GOCACHE': '/home/go-builder/.cache/go-build'}
        stage = user = workdir = None
        environment, calls, stage_runs = {}, [], []
        nonroot_selected = False
        for line in source.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            instruction, value = line.split(None, 1)
            instruction = instruction.upper()
            if instruction == 'FROM':
                words = shlex.split(value)
                stage = words[-1] if len(words) >= 3 and words[-2].upper() == 'AS' else None
                user = workdir = None
                environment = {}
                nonroot_selected = False
            elif instruction == 'USER':
                if stage == 'patched-go-tools':
                    if nonroot_selected:
                        self.assertEqual(value, '10001:10001', 'The source stage cannot return to root')
                    nonroot_selected = nonroot_selected or value == '10001:10001'
                user = value
            elif instruction == 'ENV' and stage == 'patched-go-tools':
                for assignment in shlex.split(value):
                    name, separator, setting = assignment.partition('=')
                    self.assertTrue(separator, 'Use explicit ENV name=value bindings')
                    environment[name] = setting
            elif instruction == 'WORKDIR':
                workdir = value
            elif instruction == 'COPY' and stage == 'patched-go-tools':
                self.assertFalse(any(word.startswith('--chown=') for word in shlex.split(value)),
                                 'Copied source inputs must retain their default root ownership')
            elif instruction == 'RUN':
                if stage == 'patched-go-tools':
                    stage_runs.append((user, dict(environment), ' '.join(value.split())))
                if 'scripts/build_runner_go_tools.py' not in value:
                    continue
                self.assertEqual(stage, 'patched-go-tools')
                self.assertEqual(user, '10001:10001')
                self.assertEqual(workdir, '/opt/victron-go-tools-inputs')
                self.assertEqual({name: environment.get(name) for name in expected_env}, expected_env)
                words = shlex.split(value)
                self.assertEqual(words[:-1], ['umask', '022', '&&', 'python3',
                                             'scripts/build_runner_go_tools.py'])
                calls.append(words[-1])
        self.assertEqual(calls, ['containerd', 'buildx', 'compose'])
        self.assertEqual(user, '1001:1001', 'The final runtime identity must not change')
        provision = [command for identity, _, command in stage_runs
                     if identity in (None, 'root', '0', '0:0') and 'useradd' in command]
        self.assertEqual(len(provision), 1, 'Provision the source account as root')
        for fragment in ('groupadd --gid 10001 go-builder',
                         'useradd --uid 10001 --gid 10001 --create-home',
                         'install -d -o 10001 -g 10001 -m 0755'):
            self.assertIn(fragment, provision[0])
        preflights = [(index, identity, bindings, command)
                      for index, (identity, bindings, command) in enumerate(stage_runs)
                      if command.startswith('test "$(id -u)" = 10001')]
        self.assertEqual(len(preflights), 1, 'Keep the image-stage permission preflight')
        index, identity, bindings, preflight = preflights[0]
        self.assertEqual(identity, '10001:10001')
        self.assertEqual({name: bindings.get(name) for name in expected_env}, expected_env)
        self.assertTrue(all(index < call_index for call_index, (_, _, command) in enumerate(stage_runs)
                            if 'scripts/build_runner_go_tools.py' in command))
        # Check the real preflight, not merely an unrelated USER/chown string.
        # Its execution in the image build remains a separate acceptance gate.
        for fragment in ('test "$(id -u -r)" = 10001', 'test "$(id -g)" = 10001',
                         'test "$(id -g -r)" = 10001',
                         'for path in "$HOME" "$GOPATH" "$GOMODCACHE" "$GOCACHE" '
                         '/opt/victron-go-tools-build /opt/victron-go-tools; do',
                         'test -d "$path" && test ! -L "$path" && test -w "$path" && test -x "$path"',
                         'test "$(stat -c \'%u:%g\' "$path")" = 10001:10001 || exit 1',
                         'test -w /tmp && test -x /tmp',
                         'find /opt/victron-go-tools-inputs -exec sh -ec',
                         'test ! -L "$path" && test -r "$path" && test ! -w "$path"',
                         'test "$(stat -c "%u:%g" "$path")" = 0:0 || exit 1',
                         'if test -d "$path"; then test -x "$path"; else test -f "$path"; fi || exit 1'):
            self.assertIn(fragment, preflight)
        self.assertNotIn('|| true', preflight)

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

    def test_runtime_probe_retains_and_runs_the_reviewed_npm_fixture(self):
        dockerfile = (RESERVE / 'Dockerfile').read_text().replace('\\\n', '')
        runtime_copy = next(line for line in dockerfile.splitlines()
                            if line.startswith('COPY deploy/runner-reserve/start-runner.sh'))
        self.assertIn('deploy/runner-reserve/qualify-npm.cjs', runtime_copy)
        self.assertIn('node /opt/victron-ci-reserve/qualify-npm.cjs',
                      (RESERVE / 'qualify.sh').read_text())

    def test_compose_uses_local_image_and_cleans_up_on_success_or_failure(self):
        source = (RESERVE / 'qualify.sh').read_text()
        section = source[source.index('work=$(mktemp -d)'):source.index('password=$(openssl')]
        self.assertIn('pull_policy: never', section)
        self.assertIn('network_mode: none', section)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable(root / 'docker', '#!' + sys.executable + '\n' + r'''
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['DOCKER_LOG'], 'a') as stream:
    stream.write(json.dumps(args) + '\n')
if args[0] == 'run':
    print('nested-build-ok')
if args[0] == 'compose':
    assert args[:4] == ['compose', '--project-name', 'reserve-qualification', '--file']
    config = pathlib.Path(args[4]).read_text()
    assert 'image: reserve-smoke:local' in config
    if args[5] == 'up':
        assert args[6:] == ['--pull', 'never', '--no-build', '--abort-on-container-exit', '--exit-code-from', 'smoke']
    if args[5] == os.environ['FAIL_STAGE']:
        sys.exit(7)
''')
            for stage in ('none', 'config', 'up'):
                with self.subTest(stage=stage):
                    log = root / (stage + '.jsonl')
                    result = subprocess.run(['bash', '-euo', 'pipefail', '-c', section],
                        env={**os.environ, 'PATH': directory + ':' + os.environ['PATH'],
                             'DOCKER_LOG': str(log), 'FAIL_STAGE': stage},
                        text=True, capture_output=True, timeout=10, check=False)
                    self.assertEqual(result.returncode, 0 if stage == 'none' else 7, result.stderr)
                    calls = [json.loads(line) for line in log.read_text().splitlines()]
                    compose = [call[5] for call in calls if call[0] == 'compose']
                    self.assertEqual(compose[0], 'config')
                    self.assertEqual(compose[-1], 'down')
                    self.assertEqual('up' in compose, stage != 'config')
                    work = Path(next(call[4] for call in calls if call[0] == 'compose')).parent
                    self.assertFalse(work.exists())

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
