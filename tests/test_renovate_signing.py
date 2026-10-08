"""Real, ephemeral-key regressions for the Renovate signing preflight."""

import base64
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "renovate_credentials", ROOT / "scripts/check_renovate_credentials.py"
)
credentials = importlib.util.module_from_spec(spec)
spec.loader.exec_module(credentials)


class RenovateSigningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # GnuPG agent Unix sockets need a short path on macOS as well as Linux.
        cls.temporary = tempfile.TemporaryDirectory(prefix="renovate-test-", dir="/tmp")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.ssh = {}
        for label, algorithm, bits, passphrase in (
            ("ed25519", "ed25519", 256, ""),
            ("encrypted", "ed25519", 256, "synthetic fixture passphrase"),
            ("ecdsa256", "ecdsa", 256, ""),
            ("ecdsa384", "ecdsa", 384, ""),
            ("ecdsa521", "ecdsa", 521, ""),
            ("rsa2048", "rsa", 2048, ""),
            ("rsa1024", "rsa", 1024, ""),
        ):
            path = cls.root / label
            subprocess.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    algorithm,
                    "-b",
                    str(bits),
                    "-N",
                    passphrase,
                    "-C",
                    "synthetic-fixture-only",
                    "-f",
                    str(path),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            cls.ssh[label] = path.read_text()
        cls.pgp = {}
        for algorithm in ("rsa1024", "rsa2048", "ed25519", "nistp256"):
            home = cls.root / (algorithm + "-pgp")
            home.mkdir(mode=0o700)
            args = [
                "gpg",
                "--no-options",
                "--homedir",
                str(home),
                "--batch",
                "--no-tty",
            ]
            cls.addClassCleanup(
                subprocess.run,
                ["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
                capture_output=True,
                timeout=15,
            )
            subprocess.run(
                args
                + [
                    "--pinentry-mode",
                    "loopback",
                    "--passphrase",
                    "",
                    "--quick-generate-key",
                    "Synthetic Test <fixture@example.invalid>",
                    algorithm,
                    "sign",
                    "0",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            cls.pgp[algorithm] = subprocess.check_output(
                args + ["--armor", "--export-secret-keys"],
                stderr=subprocess.PIPE,
                timeout=15,
            ).decode()
            if algorithm == "rsa2048":
                metadata = subprocess.check_output(
                    args + ["--with-colons", "--list-keys"],
                    stderr=subprocess.PIPE,
                    timeout=15,
                ).decode()
                fingerprint = next(
                    line.split(":")[9]
                    for line in metadata.splitlines()
                    if line.startswith("fpr:")
                )
                subprocess.run(
                    args
                    + [
                        "--pinentry-mode",
                        "loopback",
                        "--passphrase",
                        "",
                        "--quick-add-key",
                        fingerprint,
                        "rsa1024",
                        "sign",
                        "0",
                    ],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                cls.pgp["weak-subkey"] = subprocess.check_output(
                    args + ["--armor", "--export-secret-keys"],
                    stderr=subprocess.PIPE,
                    timeout=15,
                ).decode()

    def test_accepts_supported_real_ssh_keys(self):
        for name, value in self.ssh.items():
            if name != "rsa1024":
                with self.subTest(name=name):
                    credentials.validate_signing_key(value)

    def test_pem_rsa_with_and_without_passphrase(self):
        for passphrase in ("", "synthetic-fixture-passphrase"):
            with (
                self.subTest(encrypted=bool(passphrase)),
                tempfile.TemporaryDirectory() as tmp,
            ):
                path = Path(tmp) / "key"
                subprocess.run(
                    [
                        "ssh-keygen",
                        "-q",
                        "-t",
                        "rsa",
                        "-b",
                        "2048",
                        "-m",
                        "PEM",
                        "-N",
                        passphrase,
                        "-f",
                        str(path),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                with patch.dict(
                    os.environ, {"RENOVATE_GIT_PRIVATE_KEY_PASSPHRASE": passphrase}
                ):
                    credentials.validate_signing_key(path.read_text())

    def test_rejects_real_weak_ssh_key(self):
        with self.assertRaisesRegex(ValueError, "RSA >=2048"):
            credentials.validate_signing_key(self.ssh["rsa1024"])

    def test_accepts_supported_real_pgp_keys(self):
        for name in ("rsa2048", "ed25519", "nistp256"):
            with self.subTest(name=name):
                credentials.validate_signing_key(self.pgp[name])

    def test_accepts_base64_pgp_as_pinned_renovate_does(self):
        value = base64.b64encode(self.pgp["ed25519"].encode()).decode()
        credentials.validate_signing_key(value)

    def test_rejects_weak_pgp_primary_and_subkeys(self):
        for name in ("rsa1024", "weak-subkey"):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "RSA >=2048"),
            ):
                credentials.validate_signing_key(self.pgp[name])

    def test_rejects_multiple_pgp_identities(self):
        with self.assertRaisesRegex(ValueError, "RSA >=2048"):
            credentials.validate_signing_key(self.pgp["ed25519"] + self.pgp["rsa2048"])

    def test_rejects_nonprivate_unsupported_and_malformed_values(self):
        for value in (
            "",
            "fixture-secret",
            "x" * (1024 * 1024 + 1),
            "-----BEGIN PGP PUBLIC KEY BLOCK-----\nnot-a-key",
            "-----BEGIN PGP PRIVATE KEY BLOCK-----\nfixture-secret",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nfixture-secret",
        ):
            with self.subTest(size=len(value)), self.assertRaises(ValueError) as caught:
                credentials.validate_signing_key(value)
            self.assertNotIn("fixture-secret", str(caught.exception))

    def test_temporary_material_is_removed_after_success_and_failure(self):
        with tempfile.TemporaryDirectory() as parent:
            factory = tempfile.TemporaryDirectory
            with patch.object(
                credentials.tempfile,
                "TemporaryDirectory",
                side_effect=lambda **kw: factory(dir=parent, **kw),
            ):
                credentials.validate_signing_key(self.ssh["ed25519"])
                self.assertEqual(list(Path(parent).iterdir()), [])
                with self.assertRaises(ValueError):
                    credentials.validate_signing_key(self.ssh["rsa1024"])
                self.assertEqual(list(Path(parent).iterdir()), [])

    def test_weak_key_is_rejected_before_github_request(self):
        with (
            patch.dict(
                os.environ,
                {
                    "RENOVATE_TOKEN": "test-token",
                    "RENOVATE_GIT_PRIVATE_KEY": self.ssh["rsa1024"],
                },
            ),
            patch.object(credentials.urllib.request, "urlopen") as request,
        ):
            with self.assertRaisesRegex(SystemExit, "RSA >=2048"):
                credentials.main()
            request.assert_not_called()

    def test_external_failure_is_bounded_and_does_not_echo_secrets(self):
        for error in (
            FileNotFoundError("fixture-secret"),
            subprocess.TimeoutExpired(["tool"], 15, output=b"fixture-secret"),
            subprocess.CalledProcessError(1, ["tool"], stderr=b"fixture-secret"),
        ):
            with (
                patch.dict(
                    os.environ,
                    {
                        "RENOVATE_TOKEN": "test-token",
                        "RENOVATE_GIT_PRIVATE_KEY": "fixture-secret",
                    },
                ),
                patch.object(credentials.subprocess, "run", side_effect=error) as run,
            ):
                with self.assertRaises(ValueError) as caught:
                    credentials._key_command(["tool"])
                self.assertNotIn("fixture-secret", str(caught.exception))
                self.assertEqual(run.call_args.kwargs["timeout"], 15)
                self.assertNotIn("RENOVATE_TOKEN", run.call_args.kwargs["env"])
                self.assertNotIn(
                    "RENOVATE_GIT_PRIVATE_KEY", run.call_args.kwargs["env"]
                )

    def test_invalid_metadata_fails_closed(self):
        for metadata in (
            "",
            "bad",
            "256 SHA256:" + "a" * 43 + " (DSA)",
            "128 SHA256:" + "a" * 43 + " (ECDSA)",
            "256 SHA256:" + "a" * 43 + " (ED25519)\nextra",
        ):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                credentials._check_ssh_metadata(metadata)
        for metadata in ("", "sec:bad", "sec:u:1024:17:key:::::::s:::::dsa:\n"):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                credentials._check_pgp_metadata(metadata)


if __name__ == "__main__":
    unittest.main()
