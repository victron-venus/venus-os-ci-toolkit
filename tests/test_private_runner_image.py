"""Offline private image context checks; no image or runtime execution."""
from pathlib import Path
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / 'deploy/arc-private'
INPUTS = {
    '.github/gh/release.json',
    'scripts/install_release_gh.py',
    'deploy/arc-private/Dockerfile',
    'deploy/arc-private/Dockerfile.dockerignore',
    'deploy/arc-private/sandbox-docker.sh',
    'deploy/runner-reserve/start-runner.sh',
    'deploy/runner-reserve/sandbox-docker.sh',
    'deploy/runner-reserve/qualify.sh',
}


class PrivateRunnerImageTests(unittest.TestCase):
    def test_root_context_excludes_everything_except_exact_build_inputs(self):
        rules = [line.strip() for line in (PRIVATE / 'Dockerfile.dockerignore').read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        self.assertEqual(rules[0], '**')
        self.assertCountEqual(rules[1:], ['!' + path for path in INPUTS])
        # Literal file exceptions cannot include local credentials, inventory,
        # Git history or siblings beneath an otherwise permitted directory.
        for path in INPUTS:
            self.assertNotRegex(path, r'[!*?\[\]\\]')
            source = ROOT / path
            self.assertFalse(source.is_symlink(), path)
            self.assertTrue(source.is_file(), path)

    def test_every_local_copy_is_in_the_context_and_no_extra_payload_is_allowed(self):
        source = (PRIVATE / 'Dockerfile').read_text().replace('\\\n', '')
        copied = set()
        for line in source.splitlines():
            instruction = shlex.split(line, comments=True)
            if not instruction or instruction[0].upper() not in {'COPY', 'ADD'}:
                continue
            # New flags, remote ADDs or JSON COPY forms need explicit review.
            self.assertEqual(instruction[0], 'COPY')
            self.assertFalse(any(part.startswith('--') for part in instruction[1:]))
            copied.update(instruction[1:-1])
        self.assertEqual(copied, INPUTS - {
            'deploy/arc-private/Dockerfile', 'deploy/arc-private/Dockerfile.dockerignore'})
        self.assertIn('FROM ${BASE_IMAGE}', source)
        self.assertNotIn('deploy/runner-reserve/guard-ca.crt', copied)

    def test_only_readonly_inputs_are_mounted_and_base_runtime_is_inherited(self):
        source = (PRIVATE / 'Dockerfile').read_text().replace('\\\n', ' ')
        final = source.split('FROM ${BASE_IMAGE}\n', 1)[1]
        self.assertEqual(sum(line.startswith('RUN ') for line in final.splitlines()), 2)
        self.assertNotRegex(final, r'(?m)^(COPY|ADD|ENV|ENTRYPOINT|CMD|WORKDIR) ')
        self.assertEqual(re.findall(r'--mount=\S+', final),
                         ['--mount=type=bind,from=private-inputs,target=/tmp/private-inputs,ro'])
        self.assertTrue(final.endswith('USER 1001:1001\n'))
        self.assertIn('FROM scratch AS private-inputs', source)
        self.assertIn('test ! -e /home/runner/.runner', final)
        self.assertIn('rm -rf /tmp/private-gh', final)
        self.assertNotIn('--no-preserve=ownership', final)

    def test_actual_payload_copy_preserves_ca_and_wrapper_order(self):
        cp = shutil.which('gcp') or shutil.which('cp')
        if 'GNU coreutils' not in subprocess.run([cp, '--version'], capture_output=True,
                                                 text=True, check=False).stdout:
            self.skipTest('GNU cp is required for the Linux image copy semantics')
        source = (PRIVATE / 'Dockerfile').read_text().replace('\\\n', ' ')
        assembly = next(line for line in source.splitlines() if line.startswith('RUN --mount='))
        assembly = re.sub(r'^RUN --mount=\S+\s+', '', assembly)
        operations = [shlex.split(part.strip()) for part in assembly.split('&&')
                      if part.strip().startswith(('cp -a ', 'mv ', 'chmod ', 'mkdir -p '))]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def mapped(path):
                return root / path.lstrip('/')
            files = {
                '/tmp/private-inputs/tmp/private-gh/.github/gh/release.json': 'locked-gh',
                '/tmp/private-inputs/tmp/private-gh/scripts/install_release_gh.py': 'installer',
                '/tmp/private-inputs/opt/victron-ci-reserve/start-runner.sh': 'bootstrap',
                '/tmp/private-inputs/opt/victron-ci-reserve/sandbox-docker.sh': 'base-wrapper',
                '/tmp/private-inputs/opt/victron-ci-reserve/qualify.sh': 'probe',
                '/tmp/private-inputs/tmp/private-wrapper/sandbox-docker.sh': 'private-wrapper',
                '/opt/victron-ci-reserve/guard-ca.crt': 'unchanged-ca',
                '/opt/victron-ci-reserve/qualify-browsers.cjs': 'unchanged-browser-probe',
            }
            for name, content in files.items():
                path = mapped(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                path.chmod(0o640)
            for operation in operations:
                actual = [cp if operation[0] == 'cp' else operation[0]]
                actual.extend(str(mapped(arg)) + ('/' if arg.endswith('/') else '')
                              if arg.startswith('/') else arg for arg in operation[1:])
                subprocess.run(actual, check=True, capture_output=True, timeout=5)
            runtime = mapped('/opt/victron-ci-reserve')
            for name, content in [('start-runner.sh', 'bootstrap'), ('sandbox-docker-base.sh', 'base-wrapper'),
                                  ('sandbox-docker.sh', 'private-wrapper'), ('qualify.sh', 'probe')]:
                self.assertEqual((runtime / name).read_text(), content)
                self.assertEqual((runtime / name).stat().st_mode & 0o777, 0o755)
                self.assertEqual((runtime / name).stat().st_uid, os.getuid())
            for name in ('guard-ca.crt', 'qualify-browsers.cjs'):
                self.assertEqual((runtime / name).read_text(), files['/opt/victron-ci-reserve/' + name])
                self.assertEqual((runtime / name).stat().st_mode & 0o777, 0o640)
            self.assertEqual(mapped('/tmp/private-gh/.github/gh/release.json').read_text(), 'locked-gh')


if __name__ == '__main__':
    unittest.main()
