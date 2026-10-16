"""Approved backup checkpoints must fail closed before keys or tools are accessed."""

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from tools.secret_control import catalog, recovery
from tools.secret_control.__main__ import main
from tools.secret_control.private_io import new_private, process


FIXTURE = {
    "schema_version": 1,
    "catalog_id": "da33eecd-54d1-45a8-bfbc-efb59ae606e4",
    "generation": 2,
    "entries": [{
        "id": "synthetic",
        "revision": 2,
        "value": "synthetic-recovery-only-${literal}",
        "targets": [{
            "type": "hcp", "category": "env", "key": "SYNTHETIC_RECOVERY",
            "variable_set_id": "varset-AAAAAAAAAAAAAAAA",
        }],
    }],
}


class RecoveryCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.path = self.root / "catalog.age"
        self.encrypted = b"synthetic encrypted fixture"
        new_private(self.path, self.encrypted)
        self.digest = hashlib.sha256(self.encrypted).hexdigest()

    def check(self, **overrides):
        options = {"expected_sha256": self.digest, "generation": 2}
        options.update(overrides)
        return recovery.check(self.path, "/missing/identity", "/missing/age", **options)

    def test_rejects_invalid_checkpoint_before_reading_input(self):
        with patch.object(recovery, "read_private") as read, patch.object(recovery, "decrypt_bytes") as decrypt:
            for overrides in (
                {"expected_sha256": "bad"}, {"expected_sha256": "A" * 64},
                {"expected_sha256": None}, {"generation": True},
                {"generation": 0}, {"generation": 2**53},
            ):
                with self.subTest(overrides=overrides), self.assertRaises(catalog.ControlError):
                    self.check(**overrides)
            read.assert_not_called()
            decrypt.assert_not_called()

    def test_digest_mismatch_never_opens_identity_or_resolves_tool(self):
        with patch.object(recovery, "decrypt_bytes") as decrypt, patch.object(recovery.shutil, "which") as which:
            with self.assertRaisesRegex(catalog.ControlError, "ciphertext digest mismatch"):
                self.check(expected_sha256="0" * 64)
            decrypt.assert_not_called()
            which.assert_not_called()

    def test_checked_bytes_are_decrypted_without_reopening_input(self):
        raw = json.dumps(FIXTURE).encode()
        with patch.object(recovery, "read_private", return_value=self.encrypted) as read, patch.object(recovery, "decrypt_bytes", return_value=raw) as decrypt:
            result = self.check()
        read.assert_called_once_with(self.path)
        decrypt.assert_called_once_with(self.encrypted, "/missing/identity", "/missing/age")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["generation"], 2)
        self.assertFalse(result["cloud_verified"])
        self.assertFalse(result["key_custody_verified"])
        output = json.dumps(result)
        for private in (FIXTURE["catalog_id"], FIXTURE["entries"][0]["value"], "varset-AAAAAAAAAAAAAAAA"):
            self.assertNotIn(private, output)

    def test_digest_match_is_not_enough_for_wrong_generation_or_invalid_catalog(self):
        invalid = copy.deepcopy(FIXTURE)
        invalid["entries"][0]["revision"] = 0
        for raw, generation in ((json.dumps(FIXTURE).encode(), 3), (json.dumps(invalid).encode(), 2)):
            with self.subTest(generation=generation), patch.object(recovery, "decrypt_bytes", return_value=raw):
                with self.assertRaises(catalog.ControlError):
                    self.check(generation=generation)

    def test_cli_refusal_does_not_echo_arguments_or_exception_details(self):
        secret = "DO-NOT-PRINT-THIS-SYNTHETIC-VALUE"
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(["recovery-check", "--catalog", secret, "--identity", secret, "--expected-sha256", secret, "--generation", "2"])
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(secret, error.getvalue())
        self.assertEqual(json.loads(error.getvalue())["status"], "refused")


@unittest.skipUnless(os.environ.get("SECRET_CONTROL_AGE") and os.environ.get("SECRET_CONTROL_AGE_KEYGEN"), "set explicit age binaries for real recovery tests")
class RealRecoveryCheckpointTests(unittest.TestCase):
    def test_real_encrypted_checkpoint_and_safe_cli_report(self):
        age, keygen = os.environ["SECRET_CONTROL_AGE"], os.environ["SECRET_CONTROL_AGE_KEYGEN"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            key = root / "synthetic.agekey"
            new_private(key, process([keygen]))
            recipient = process([keygen, "-y", str(key)]).decode().strip()
            ciphertext = process([age, "--encrypt", "--recipient", recipient], json.dumps(FIXTURE).encode())
            path = root / "approved.age"
            new_private(path, ciphertext)
            digest = hashlib.sha256(ciphertext).hexdigest()
            args = ["recovery-check", "--catalog", str(path), "--identity", str(key), "--age", age, "--expected-sha256", digest, "--generation", "2"]
            output, error = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                code = main(args)
            self.assertEqual(code, 0, error.getvalue())
            self.assertEqual(json.loads(output.getvalue())["status"], "verified")
            self.assertNotIn(FIXTURE["entries"][0]["value"], output.getvalue())
            self.assertNotIn(str(key), output.getvalue())
            with self.assertRaisesRegex(catalog.ControlError, "generation mismatch"):
                recovery.check(path, key, age, expected_sha256=digest, generation=1)
            wrong = root / "wrong.agekey"
            new_private(wrong, process([keygen]))
            with self.assertRaises(catalog.ControlError):
                recovery.check(path, wrong, age, expected_sha256=digest, generation=2)


if __name__ == "__main__":
    unittest.main()
