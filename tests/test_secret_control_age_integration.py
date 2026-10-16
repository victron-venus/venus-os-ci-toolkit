"""Real encryption and disaster-recovery checks, enabled by the POSIX CI job."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


AGE = os.environ.get('SECRET_CONTROL_AGE')
KEYGEN = os.environ.get('SECRET_CONTROL_AGE_KEYGEN')
ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(AGE and KEYGEN, 'set both SECRET_CONTROL_AGE and SECRET_CONTROL_AGE_KEYGEN')
class AgeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.identities = [self.root / f'identity-{i}' for i in range(3)]
        self.recipients = []
        for identity in self.identities:
            subprocess.run([KEYGEN, '-o', str(identity)], check=True, capture_output=True, timeout=10)
            identity.chmod(0o600)
            result = subprocess.run([KEYGEN, '-y', str(identity)], check=True, capture_output=True, timeout=10)
            self.recipients.append(result.stdout.decode().strip())
        self.catalog = {
            'schema_version': 1,
            'catalog_id': 'ba35a848-d775-4548-890f-deb3b94cbaab',
            'generation': 1,
            'entries': [{'id': 'synthetic', 'revision': 1, 'value': 'SYNTHETIC_ONLY_RECOVERY_TEST',
                         'targets': [{'type': 'hcp', 'key': 'SYNTHETIC_PROBE', 'category': 'env',
                                      'workspace_id': 'ws-1234567890abcdef'}]}],
        }
        self.encrypted = self.root / 'catalog-1.age'

    def cli(self, *args, data=None, success=True):
        result = subprocess.run([sys.executable, '-m', 'tools.secret_control', *map(str, args)],
                                cwd=ROOT, input=data, capture_output=True, timeout=20)
        self.assertEqual(result.returncode == 0, success, result.stderr.decode())
        for output in (result.stdout, result.stderr):
            self.assertNotIn(b'SYNTHETIC_ONLY_RECOVERY_TEST', output)
            self.assertNotIn(b'AGE-SECRET-KEY-', output)
        return result

    def encrypt(self):
        return self.cli('encrypt', '--age', AGE, '--output', self.encrypted,
                        '--recipient', self.recipients[0], '--recipient', self.recipients[1],
                        data=json.dumps(self.catalog).encode())

    def test_recovery_from_second_key_after_operator_key_loss(self):
        self.encrypt()
        self.identities[0].unlink()
        ciphertext = self.encrypted.read_bytes()
        self.assertNotIn(b'SYNTHETIC_ONLY_RECOVERY_TEST', ciphertext)
        self.assertNotIn(b'workspace_id', ciphertext)
        self.cli('validate', '--age', AGE, '--catalog', self.encrypted, '--identity', self.identities[1])
        recovered = subprocess.run([AGE, '-d', '-i', str(self.identities[1]), str(self.encrypted)],
                                   check=True, capture_output=True, timeout=10)
        self.assertEqual(json.loads(recovered.stdout), self.catalog)

    def test_wrong_key_and_tampering_refused(self):
        self.encrypt()
        self.cli('validate', '--age', AGE, '--catalog', self.encrypted,
                 '--identity', self.identities[2], success=False)
        ciphertext = bytearray(self.encrypted.read_bytes())
        ciphertext[-1] ^= 1
        self.encrypted.write_bytes(ciphertext)
        self.cli('validate', '--age', AGE, '--catalog', self.encrypted,
                 '--identity', self.identities[1], success=False)

    def test_immutable_generation_and_revision_lineage(self):
        self.encrypt()
        before = self.encrypted.read_bytes()
        self.cli('encrypt', '--age', AGE, '--output', self.encrypted,
                 '--recipient', self.recipients[0], data=json.dumps(self.catalog).encode(), success=False)
        self.assertEqual(self.encrypted.read_bytes(), before)
        self.catalog['generation'] = 2
        self.catalog['entries'][0]['revision'] = 2
        self.catalog['entries'][0]['value'] = 'ROTATED_SYNTHETIC_ONLY'
        second = self.root / 'catalog-2.age'
        self.cli('encrypt', '--age', AGE, '--output', second,
                 '--previous', self.encrypted, '--identity', self.identities[0],
                 '--recipient', self.recipients[1], data=json.dumps(self.catalog).encode())
        self.cli('plan', '--age', AGE, '--catalog', second, '--identity', self.identities[1])


if __name__ == '__main__':
    unittest.main()
