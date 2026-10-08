"""Exercise delivery signing with synthetic SSH keys and isolated Git config."""

import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def signing_script():
    workflow = yaml.safe_load((ROOT / '.github/workflows/vendor-update.yml').read_text())
    step = next(item for item in workflow['jobs']['update']['steps'] if item.get('id') == 'signing')
    return '\n'.join(step['run'].splitlines()[1:-1])


@unittest.skipUnless(shutil.which('ssh-keygen') and shutil.which('git'), 'OpenSSH and Git required')
class DeliverySigningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys_directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.keys_directory.cleanup)
        cls.keys = {}
        for kind, bits in [('ed25519', None), ('ecdsa', 256), ('ecdsa', 384),
                           ('ecdsa', 521), ('rsa', 2048), ('rsa', 1024), ('dsa', 1024)]:
            path = Path(cls.keys_directory.name) / f'{kind}-{bits}'
            command = ['ssh-keygen', '-q', '-t', kind, '-N', '', '-C', 'synthetic-signing-test', '-f', str(path)]
            if bits is not None:
                command.extend(['-b', str(bits)])
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            if result.returncode:
                if kind == 'dsa':
                    continue  # Current OpenSSH builds may have removed DSA entirely.
                raise AssertionError('Unable to generate a synthetic SSH key')
            cls.keys[(kind, bits)] = path

    def run_signing(self, key_text):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer = root / 'consumer'
            consumer.mkdir()
            home = root / 'home'
            home.mkdir()
            temporary = root / 'runner'
            temporary.mkdir()
            output = root / 'output'
            output.write_text('')
            env = {'PATH': os.environ['PATH'], 'HOME': str(home),
                   'GIT_CONFIG_NOSYSTEM': '1', 'RUNNER_TEMP': str(temporary),
                   'GITHUB_OUTPUT': str(output), 'DELIVERY_SIGNING_KEY': key_text,
                   'COMMITTER_NAME': 'Fixture', 'COMMITTER_EMAIL': 'fixture@example.invalid'}
            subprocess.run(['git', 'init', '-q', str(consumer)], env=env, check=True, capture_output=True)
            config_command = ['git', '-C', str(consumer), 'config', '--local', '--list']
            before = subprocess.check_output(config_command, env=env, text=True)
            result = subprocess.run([sys.executable, '-I', '-c', signing_script()], cwd=root, env=env,
                                    capture_output=True, text=True, check=False)
            after = subprocess.check_output(config_command, env=env, text=True)
            return result, before, after, output.read_text()

    def test_supported_generated_keys_configure_signing(self):
        for identity in [('ed25519', None), ('ecdsa', 256), ('ecdsa', 384), ('ecdsa', 521), ('rsa', 2048)]:
            with self.subTest(identity=identity):
                result, _, config, output = self.run_signing(self.keys[identity].read_text())
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('commit.gpgsign=true', config)
                self.assertIn('gpg.format=ssh', config)
                self.assertRegex(output, r'^fingerprint=SHA256:[A-Za-z0-9+/]{43}\n$')
                self.assertEqual(result.stdout, '')

    def test_weak_or_unsupported_generated_keys_fail_before_git_config(self):
        for identity in [('rsa', 1024), ('dsa', 1024)]:
            if identity not in self.keys:
                continue
            with self.subTest(identity=identity):
                secret = self.keys[identity].read_text()
                result, before, after, output = self.run_signing(secret)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(before, after)
                self.assertEqual(output, '')
                self.assertNotIn(secret, result.stdout + result.stderr)
                self.assertNotIn('PRIVATE KEY', result.stdout + result.stderr)

    def test_invalid_key_error_does_not_echo_input(self):
        secret = 'synthetic-private-invalid-marker'
        result, before, after, output = self.run_signing(secret)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, after)
        self.assertEqual(output, '')
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_unknown_type_and_mismatched_curve_strength_are_rejected(self):
        namespace = {}
        tree = ast.parse(signing_script())
        definitions = [node for node in tree.body if isinstance(node, (ast.Import, ast.FunctionDef))]
        exec(compile(ast.Module(body=definitions, type_ignores=[]), '<signing-policy>', 'exec'), namespace)
        validate = namespace['validate_public_key']
        key = self.keys[('ed25519', None)]
        public = subprocess.check_output(['ssh-keygen', '-y', '-P', '', '-f', str(key)], text=True).strip()
        metadata = subprocess.check_output(['ssh-keygen', '-lf', str(key), '-E', 'sha256'], text=True)
        self.assertTrue(validate(public, metadata).startswith('SHA256:'))
        for invalid in [public.replace('ssh-ed25519', 'unknown-key', 1),
                        public.replace('ssh-ed25519', 'ecdsa-sha2-nistp384', 1),
                        public + '\nInjected key']:
            with self.subTest(invalid=invalid), self.assertRaises(SystemExit):
                validate(invalid, metadata)
        with self.assertRaises(SystemExit):
            validate(public, 'invalid metadata')
