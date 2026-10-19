"""Offline private image context checks; no image or runtime execution."""
from pathlib import Path
import shlex
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


if __name__ == '__main__':
    unittest.main()
